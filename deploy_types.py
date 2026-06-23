#!/usr/bin/env python3
"""
deploy_types.py — Classifica os Ingresses nginx por tipo de deploy.

Lê todos os JSONs em data/raw/ e produz:
  - output/deploy_types.csv — um ingress por linha com cluster, namespace, nome e tipo de deploy

Tipos de deploy identificados:
  - spinnaker  : tem annotation moniker.spinnaker.io/application ou artifact.spinnaker.io/name
  - helm       : label app.kubernetes.io/managed-by=Helm ou annotation meta.helm.sh/release-name
  - manual     : label k8slens-edit-resource-version ou annotation freelens.app/resource-version
  - outros     : nenhum dos critérios acima (kubectl apply direto, scripts, etc.)

Uso:
  python3 deploy_types.py [--raw-dir <path>] [--output <path>]
  python3 deploy_types.py --spinnaker-session <SESSION_COOKIE>
"""

import argparse
import csv
import json
import urllib.error
import urllib.request
from pathlib import Path

RAW_DIR = Path(__file__).parent / "data" / "raw"
OUTPUT_DIR = Path(__file__).parent / "output"

SPINNAKER_GATE = "https://spinnaker-api.rdops.systems"

RAILS_HELM_LABEL = "resource_kind"   # exclusivo do chart rails (resource_kind: ingress)
RDSM_HELM_LABEL  = "app_group"       # exclusivo do chart rdsm  (app_group: web/ingress/...)

# Pipelines de suporte que não gerenciam o ingress diretamente
SUPPORT_PIPELINE_KEYWORDS = {
    "external-secret", "externalsecret", "cloudsql", "backup",
    "cronjob", "rollback", "destroy", "teardown",
}


def classify(ann: dict, labels: dict) -> tuple[str, str]:
    """Retorna (tipo, evidência) para o ingress.

    Prioridade de classificação:
      1. Chart interno (rails/rdsm) — labels são o sinal mais específico e confiável,
         independente de como o ingress foi implantado (Spinnaker, manual, etc.)
      2. Spinnaker sem chart interno → spinnaker+manifesto ou spinnaker+helm-externo
      3. Helm direto (managed-by=Helm ou meta.helm.sh/release-name)
      4. Edição manual via Lens/Freelens
      5. Outros (kubectl apply, scripts, etc.)
    """
    spinnaker_app = ann.get("moniker.spinnaker.io/application", "")
    artifact_name = ann.get("artifact.spinnaker.io/name", "")
    helm_chart = labels.get("helm.sh/chart", "")

    # 1. Chart interno rails/rdsm — verificado antes do Spinnaker.
    # app_group com valor "ingress" é label customizada, não vem do chart rdsm
    # (no chart rdsm o valor é o componente: web, api, worker, etc.)
    if RAILS_HELM_LABEL in labels:
        return "spinnaker+helm-rails", helm_chart or "rails-style"
    if RDSM_HELM_LABEL in labels and labels[RDSM_HELM_LABEL] != "ingress":
        return "spinnaker+helm-rdsm", helm_chart or "rdsm-style"

    # 2. Spinnaker sem chart interno
    if spinnaker_app or artifact_name:
        if helm_chart:
            return "spinnaker+helm-externo", helm_chart
        return "spinnaker+manifesto", spinnaker_app or artifact_name

    # 3. Helm direto
    helm_managed = labels.get("app.kubernetes.io/managed-by", "") == "Helm"
    helm_release = ann.get("meta.helm.sh/release-name", "")
    if helm_managed or helm_release:
        return "helm-direto", helm_release or helm_chart or "Helm"

    # 4. Edição manual via Lens
    if labels.get("k8slens-edit-resource-version") or ann.get("freelens.app/resource-version"):
        return "manual", "lens/freelens"

    return "outros", ""


def load_ingresses(raw_dir: Path) -> list[dict]:
    ingresses = []
    for fpath in sorted(raw_dir.glob("*.json")):
        parts = fpath.stem.split("__")
        project_id = parts[0]
        cluster_name = parts[1] if len(parts) > 1 else parts[0]

        with open(fpath) as f:
            data = json.load(f)
        items = data if isinstance(data, list) else data.get("items", [])

        for item in items:
            meta = item.get("metadata", {})
            spec = item.get("spec", {})
            ann = meta.get("annotations", {})
            labels = meta.get("labels", {})

            ic_spec = spec.get("ingressClassName", "")
            ic_ann = ann.get("kubernetes.io/ingress.class", "")
            if ic_spec != "nginx" and ic_ann != "nginx":
                continue

            deploy_type, evidence = classify(ann, labels)

            hosts = [r.get("host", "") for r in spec.get("rules", [])]

            ingresses.append({
                "project_id": project_id,
                "cluster": cluster_name,
                "namespace": meta.get("namespace", ""),
                "ingress": meta.get("name", ""),
                "deploy_type": deploy_type,
                "spinnaker_app": ann.get("moniker.spinnaker.io/application", ""),
                "spinnaker_cluster": ann.get("moniker.spinnaker.io/cluster", ""),
                "helm_release": ann.get("meta.helm.sh/release-name", ""),
                "helm_chart": labels.get("helm.sh/chart", ""),
                "helm_chart_artifact": "",
                "evidence": evidence,
                "hosts": "|".join(hosts),
                "source_repo": "",
            })

    return ingresses


# ---------------------------------------------------------------------------
# Enriquecimento via API do Spinnaker
# ---------------------------------------------------------------------------

def _parse_github_repo(reference: str) -> str:
    """Extrai a URL base do repo GitHub a partir de uma referência de artifact."""
    if not reference:
        return ""
    # https://api.github.com/repos/ORG/REPO/contents/...
    if "api.github.com/repos/" in reference:
        after = reference.split("api.github.com/repos/", 1)[1]
        parts = after.split("/")
        if len(parts) >= 2:
            return f"https://github.com/{parts[0]}/{parts[1]}"
    # https://github.com/ORG/REPO/blob/... ou https://github.com/ORG/REPO
    if "github.com/" in reference:
        after = reference.split("github.com/", 1)[1]
        parts = after.split("/")
        if len(parts) >= 2:
            return f"https://github.com/{parts[0]}/{parts[1]}"
    return ""


def _is_chart_repo(repo_url: str) -> bool:
    """Repos dedicados só a charts Helm — preferimos repos de app quando há os dois."""
    name = repo_url.rstrip("/").split("/")[-1].lower()
    return name.endswith("-helm") or name == "helm"


def _is_support_pipeline(name: str) -> bool:
    name_lower = name.lower()
    return any(kw in name_lower for kw in SUPPORT_PIPELINE_KEYWORDS)


def _fetch_pipeline_configs(app: str, session: str) -> list[dict]:
    url = f"{SPINNAKER_GATE}/applications/{app}/pipelineConfigs"
    req = urllib.request.Request(url, headers={"Cookie": f"SESSION={session}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError):
        return []


def _fetch_last_executions(app: str, session: str, limit: int = 5) -> list[dict]:
    """Busca últimas execuções para extrair artifacts resolvidos (pipeline templates)."""
    url = f"{SPINNAKER_GATE}/applications/{app}/pipelines?limit={limit}&expand=false"
    req = urllib.request.Request(url, headers={"Cookie": f"SESSION={session}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError):
        return []


def _chart_from_executions(executions: list[dict]) -> str:
    """Extrai nome do chart .tgz dos artifacts resolvidos nas últimas execuções."""
    for execution in executions:
        for artifact in execution.get("trigger", {}).get("artifacts", []):
            chart = _chart_name_from_reference(artifact.get("reference", ""))
            if chart:
                return chart
        for artifact in execution.get("artifacts", []):
            chart = _chart_name_from_reference(artifact.get("reference", ""))
            if chart:
                return chart
    return ""


def _resolve_spel(value: str, app: str) -> str:
    """Substitui expressões SpEL conhecidas pelo valor real em runtime."""
    return value.replace("${execution['application']}", app)


def _chart_name_from_reference(reference: str) -> str:
    """Extrai o nome do arquivo de chart (ex: rails-0.5.3.tgz) de uma referência de artifact."""
    if not reference:
        return ""
    # último segmento do path que termine em .tgz
    for segment in reversed(reference.split("/")):
        if segment.endswith(".tgz"):
            return segment
    return ""


def _extract_refs_from_any(obj, app: str, refs: list[str]) -> None:
    """Percorre recursivamente obj e coleta todas as referências GitHub e .tgz."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "reference" and isinstance(v, str):
                refs.append(_resolve_spel(v, app))
            else:
                _extract_refs_from_any(v, app, refs)
    elif isinstance(obj, list):
        for item in obj:
            _extract_refs_from_any(item, app, refs)


def _repo_and_chart_from_pipeline_configs(configs: list[dict], app: str) -> tuple[str, str]:
    """Extrai (source_repo, helm_chart_artifact) dos configs de pipeline.

    Busca em inputArtifacts dos stages E em qualquer campo 'reference' do JSON
    (cobre pipeline templates, expectedArtifacts e variáveis).
    """
    import re as _re

    app_repos: list[str] = []
    chart_repos: list[str] = []
    chart_artifacts: list[str] = []

    for config in configs:
        if _is_support_pipeline(config.get("name", "")):
            continue

        # busca específica em inputArtifacts de stages bakeManifest/deployManifest
        for stage in config.get("stages", []):
            if stage.get("type") not in ("bakeManifest", "deployManifest"):
                continue
            for ia in stage.get("inputArtifacts", []):
                raw_ref = ia.get("artifact", {}).get("reference", "")
                ref = _resolve_spel(raw_ref, app)
                repo = _parse_github_repo(ref)
                chart = _chart_name_from_reference(ref)
                if chart and chart not in chart_artifacts:
                    chart_artifacts.append(chart)
                if repo:
                    if _is_chart_repo(repo):
                        if repo not in chart_repos:
                            chart_repos.append(repo)
                    else:
                        if repo not in app_repos:
                            app_repos.append(repo)

        # fallback: varre todo o JSON do config procurando referências
        if not chart_artifacts:
            all_refs: list[str] = []
            _extract_refs_from_any(config, app, all_refs)
            for ref in all_refs:
                chart = _chart_name_from_reference(ref)
                if chart and chart not in chart_artifacts:
                    chart_artifacts.append(chart)
                repo = _parse_github_repo(ref)
                if repo:
                    if _is_chart_repo(repo):
                        if repo not in chart_repos:
                            chart_repos.append(repo)
                    else:
                        if repo not in app_repos:
                            app_repos.append(repo)

        # fallback regex: pega .tgz em qualquer string do config (variables, templates)
        if not chart_artifacts:
            raw_json = json.dumps(config)
            for match in _re.findall(r'[\w][\w.\-]*-[\d]+\.[\d]+\.[\d]+\.tgz|[\w][\w.\-]*-[\d]+\.[\d]+\.tgz', raw_json):
                if match not in chart_artifacts:
                    chart_artifacts.append(match)

    # prefere versão mais recente quando há múltiplas
    if len(chart_artifacts) > 1:
        try:
            from packaging.version import Version
            def _ver(s):
                m = __import__('re').search(r'([\d]+\.[\d]+(?:\.[\d]+)?)', s)
                return Version(m.group(1)) if m else Version("0")
            chart_artifacts.sort(key=_ver, reverse=True)
        except Exception:
            pass

    source_repo = app_repos[0] if app_repos else (chart_repos[0] if chart_repos else "")
    helm_chart_artifact = chart_artifacts[0] if chart_artifacts else ""
    return source_repo, helm_chart_artifact


def enrich_with_source_repos(ingresses: list[dict], session: str) -> None:
    """Consulta a API do Spinnaker e preenche source_repo e helm_chart_artifact em cada ingress."""
    spinnaker_apps = {
        i["spinnaker_app"]
        for i in ingresses
        if i["spinnaker_app"]
    }

    total = len(spinnaker_apps)
    print(f"\nConsultando API do Spinnaker para {total} apps...")

    cache: dict[str, tuple[str, str]] = {}
    for idx, app in enumerate(sorted(spinnaker_apps), 1):
        configs = _fetch_pipeline_configs(app, session)
        repo, chart = _repo_and_chart_from_pipeline_configs(configs, app)
        # fallback: busca chart nas últimas execuções (pipeline templates não têm stages)
        if not chart:
            executions = _fetch_last_executions(app, session)
            chart = _chart_from_executions(executions)
        cache[app] = (repo, chart)
        status = repo if repo else "(não encontrado)"
        chart_info = f" [{chart}]" if chart else ""
        print(f"  [{idx}/{total}] {app} → {status}{chart_info}")

    for ingress in ingresses:
        app = ingress["spinnaker_app"]
        if app:
            repo, chart = cache.get(app, ("", ""))
            ingress["source_repo"] = repo
            if chart and not ingress.get("helm_chart"):
                ingress["helm_chart_artifact"] = chart


# ---------------------------------------------------------------------------
# Saída
# ---------------------------------------------------------------------------

def write_csv(ingresses: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "project_id", "cluster", "namespace", "ingress",
        "deploy_type", "spinnaker_app", "spinnaker_cluster",
        "helm_release", "helm_chart", "helm_chart_artifact", "evidence", "hosts", "source_repo",
    ]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(ingresses)


def print_summary(ingresses: list[dict]) -> None:
    from collections import Counter
    counts = Counter(i["deploy_type"] for i in ingresses)
    total = len(ingresses)
    with_repo = sum(1 for i in ingresses if i.get("source_repo"))
    print(f"\nTotal nginx ingresses: {total}")
    for t in ("spinnaker+manifesto", "spinnaker+helm-rails", "spinnaker+helm-rdsm", "spinnaker+helm-externo", "helm-direto", "manual", "outros"):
        n = counts[t]
        pct = n / total * 100 if total else 0
        print(f"  {t:<25} {n:>4}  ({pct:.0f}%)")
    if with_repo:
        pct_repo = with_repo / total * 100
        print(f"\n  source_repo preenchido: {with_repo}/{total} ({pct_repo:.0f}%)")


def main():
    parser = argparse.ArgumentParser(description="Classifica ingresses nginx por tipo de deploy")
    parser.add_argument("--raw-dir", default=str(RAW_DIR), help="Diretório com os JSONs coletados")
    parser.add_argument("--output", default=str(OUTPUT_DIR / "deploy_types.csv"), help="Caminho do CSV de saída")
    parser.add_argument(
        "--spinnaker-session",
        metavar="SESSION",
        help="Cookie SESSION do Spinnaker para enriquecer source_repo via API",
    )
    args = parser.parse_args()

    raw_dir = Path(args.raw_dir)
    output_path = Path(args.output)

    ingresses = load_ingresses(raw_dir)

    if args.spinnaker_session:
        enrich_with_source_repos(ingresses, args.spinnaker_session)

    write_csv(ingresses, output_path)
    print_summary(ingresses)
    print(f"\nCSV gerado: {output_path}")


if __name__ == "__main__":
    main()
