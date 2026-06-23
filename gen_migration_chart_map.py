#!/usr/bin/env python3
"""
gen_migration_chart_map.py

Gera output/skip_helm_chart_mapping.csv com todos os ingresses migrados
para APISIX no cluster staging-234557.

Fontes:
  - helm-apisix/k8s/staging-234557/config/manifests/**/*-gw.yaml  → quais foram migrados
  - output/deploy_types.csv (staging-234557)                       → deploy_type, helm_chart, spinnaker_app
  - output/skip_helm_chart_mapping.csv (existente)                 → dados já curados manualmente

Coluna ingress_class (adicionada):
  nginx-hardcoded  — template força ingressClassName: nginx sem variável Helm
  configurável     — template usa {{ .Values.xxx }} para ingressClassName
  misto            — template tem ocorrências hardcoded E configuráveis
  sem-ingress      — chart não tem template de Ingress
  sem-versão       — nome genérico (rails/rdsm), sem .tgz para baixar
  sem-repo         — sem URL de repo para download
  erro-download    — falha ao baixar via gh api
  erro             — falha ao extrair o .tgz
"""

import base64
import csv
import json
import re
import subprocess
import tarfile
import tempfile
from collections import Counter
from pathlib import Path

HELM_APISIX   = Path.home() / "workspace/helm-apisix"
RD_HELM       = Path.home() / "workspace/rd-helm"
CHART_CACHE   = Path.home() / ".chart-cache"
MANIFESTS_DIR = HELM_APISIX / "k8s/staging-234557/config/manifests"
OUTPUT_DIR = Path(__file__).parent / "output"
DEPLOY_TYPES_CSV = OUTPUT_DIR / "deploy_types.csv"
EXISTING_CSV = OUTPUT_DIR / "skip_helm_chart_mapping.csv"
OUT_CSV = OUTPUT_DIR / "skip_helm_chart_mapping.csv"

# chart name prefix → repo GitHub do chart
CHART_REPO_MAP = {
    "rails":     "https://github.com/ResultadosDigitais/helm",
    "go-app":    "https://github.com/ResultadosDigitais/helm",
    "front-hub": "https://github.com/ResultadosDigitais/helm",
    "inngest":   "https://github.com/ResultadosDigitais/ai-helm",
    "litellm":   "https://github.com/ResultadosDigitais/ai-helm",
}

DEPLOY_TYPE_DEFAULTS = {
    "spinnaker+helm-rails":   ("rails",  "https://github.com/ResultadosDigitais/helm"),
    "spinnaker+helm-rdsm":    ("rdsm",   "https://github.com/ResultadosDigitais/helm"),
}


# ---------------------------------------------------------------------------
# Inspeção de charts — verifica se ingressClassName está hardcoded no template
# ---------------------------------------------------------------------------

def _scan_templates(base: Path) -> str:
    """Varre *.yaml em templates/ procurando ingressClassName (spec ou annotation antiga).

    Detecta duas formas de definir a ingress class:
      spec.ingressClassName: nginx          (Kubernetes >= 1.18)
      kubernetes.io/ingress.class: nginx    (annotation antiga, deprecated)
    """
    hardcoded = False
    configurable = False
    found = False
    for tmpl in sorted(base.rglob("templates/*.yaml")):
        try:
            content = tmpl.read_text(errors="replace")
        except OSError:
            continue
        for line in content.splitlines():
            s = line.strip()
            if s.startswith("#"):
                continue
            is_class_line = s.startswith("ingressClassName") or \
                            "kubernetes.io/ingress.class" in s
            if not is_class_line:
                continue
            found = True
            if "{{" in s:
                configurable = True
            else:
                hardcoded = True
    if not found:
        return "sem-ingress"
    if hardcoded and configurable:
        return "misto"
    return "nginx-hardcoded" if hardcoded else "configurável"


def _inspect_from_gh_charts_dir(chart_prefix: str, gh_repo: str) -> str:
    """Fallback para charts em formato de diretório (charts/<name>/templates/) no GitHub."""
    listing_proc = subprocess.run(
        ["gh", "api", f"repos/{gh_repo}/contents/charts/{chart_prefix}/templates/",
         "--jq", "[.[] | select(.type == \"file\" and (.name | endswith(\".yaml\"))) | .name]"],
        capture_output=True, text=True,
    )
    if listing_proc.returncode != 0:
        return "erro-download"
    try:
        template_files = json.loads(listing_proc.stdout)
    except json.JSONDecodeError:
        return "erro-download"

    aggregated = "sem-ingress"
    for fname in template_files:
        proc = subprocess.run(
            ["gh", "api",
             f"repos/{gh_repo}/contents/charts/{chart_prefix}/templates/{fname}"],
            capture_output=True,
        )
        if proc.returncode != 0:
            continue
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            continue
        if data.get("encoding") == "base64" and data.get("content"):
            content = base64.b64decode(
                data["content"].replace("\n", "")
            ).decode("utf-8", errors="replace")
            hardcoded = configurable = False
            for line in content.splitlines():
                s = line.strip()
                if s.startswith("#"):
                    continue
                if not (s.startswith("ingressClassName") or
                        "kubernetes.io/ingress.class" in s):
                    continue
                if "{{" in s:
                    configurable = True
                else:
                    hardcoded = True
            if hardcoded and configurable:
                return "misto"
            if hardcoded:
                return "nginx-hardcoded"
            if configurable:
                aggregated = "configurável"
    return aggregated


def _inspect_tgz(tgz: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            with tarfile.open(tgz) as tar:
                tar.extractall(tmp)
        except Exception:
            return "erro"
        result = _scan_templates(Path(tmp))
        if result != "sem-ingress":
            return result
        # chart pode ter um subdiretório wrapper (ex: rails-0.5.1/rails/)
        for sub in sorted(Path(tmp).iterdir()):
            if sub.is_dir():
                r = _scan_templates(sub)
                if r != "sem-ingress":
                    return r
        return "sem-ingress"


def inspect_ingress_class(chart: str, repo: str) -> str:
    """Retorna o tipo de ingressClassName encontrado no template do chart.

    Ordem de busca: diretório extraído em ~/Downloads → .tgz local → cache
    em ~/.chart-cache → download via gh api.
    """
    if not chart or chart in ("rails", "rdsm"):
        return "sem-versão"

    chart_file = chart if chart.endswith(".tgz") else f"{chart}.tgz"
    stem       = chart_file.removesuffix(".tgz")
    downloads  = Path.home() / "Downloads"

    # 1. Diretório já extraído em ~/Downloads (suporta cópias "stem (2)")
    for candidate in sorted(downloads.glob(f"{stem}*")):
        if not candidate.is_dir():
            continue
        result = _scan_templates(candidate)
        if result != "sem-ingress":
            return result
        for sub in sorted(candidate.iterdir()):
            if sub.is_dir():
                r = _scan_templates(sub)
                if r != "sem-ingress":
                    return r

    # 2. .tgz em ~/Downloads
    local_tgz = downloads / chart_file
    if local_tgz.exists():
        return _inspect_tgz(local_tgz)

    # Extrai prefixo sem versão: "eck-elasticsearch-0.3.0" → "eck-elasticsearch"
    parts = stem.split("-")
    chart_prefix = stem
    for i, part in enumerate(parts):
        if part and part[0].isdigit():
            chart_prefix = "-".join(parts[:i])
            break
    rd_helm_dir = RD_HELM / "packages" / chart_prefix

    # 3. rd-helm/packages/<chart-prefix>/<chart-file> — versão exata apenas
    if rd_helm_dir.is_dir():
        exact = rd_helm_dir / chart_file
        if exact.exists():
            return _inspect_tgz(exact)

    # 4. Cache em ~/.chart-cache (versão exata, baixada anteriormente)
    cached = CHART_CACHE / chart_file
    if cached.exists():
        return _inspect_tgz(cached)

    # 5. Download via gh api (versão exata)
    if repo:
        m = re.search(r"github\.com/([^/?#\s]+/[^/?#\s]+)", repo)
        if m:
            gh_repo = m.group(1)
            CHART_CACHE.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                ["gh", "api", f"repos/{gh_repo}/contents/{chart_file}"],
                capture_output=True,
            )
            if proc.returncode == 0:
                try:
                    data = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    data = {}
                if data.get("encoding") == "base64" and data.get("content"):
                    binary = base64.b64decode(data["content"].replace("\n", ""))
                    cached.write_bytes(binary)
                    return _inspect_tgz(cached)
                download_url = data.get("download_url", "")
                if download_url:
                    token_proc = subprocess.run(
                        ["gh", "auth", "token"], capture_output=True, text=True
                    )
                    token = token_proc.stdout.strip()
                    dl = subprocess.run(
                        ["curl", "-fsSL", "-H", f"Authorization: Bearer {token}",
                         "-o", str(cached), download_url],
                        capture_output=True,
                    )
                    if dl.returncode == 0 and cached.exists() and cached.stat().st_size > 200:
                        return _inspect_tgz(cached)
            if cached.exists():
                cached.unlink()

    # 6. Charts em formato de diretório no GitHub (ex: ai-helm/charts/<name>/templates/)
    if repo:
        m2 = re.search(r"github\.com/([^/?#\s]+/[^/?#\s]+)", repo)
        if m2:
            r = _inspect_from_gh_charts_dir(chart_prefix, m2.group(1))
            if r != "erro-download":
                return r

    # 7. Último recurso: versão mais recente no rd-helm (pode diferir da versão exata)
    if rd_helm_dir.is_dir():
        candidates = sorted(rd_helm_dir.glob(f"{chart_prefix}-*.tgz"))
        if candidates:
            return _inspect_tgz(candidates[-1])

    return "erro-download"


# ---------------------------------------------------------------------------

def chart_to_repo(helm_chart: str) -> str:
    for prefix, repo in CHART_REPO_MAP.items():
        if helm_chart.startswith(prefix):
            return repo
    return ""


def deploy_type_status(deploy_type: str) -> str:
    mapping = {
        "spinnaker+helm-rails":   "Pronto — só trocar ingressClassName nos values da app",
        "spinnaker+helm-rdsm":    "Pronto — só trocar ingressClassName nos values da app",
        "spinnaker+helm-externo": "Pronto — só trocar ingressClassName nos values da app",
        "spinnaker+manifesto":    "Manifesto Spinnaker — adicionar stage APISIX na pipeline",
        "helm-direto":            "Helm direto — atualizar values",
        "manual":                 "Manual (Lens/Freelens) — kubectl apply direto",
        "outros":                 "kubectl apply direto",
    }
    return mapping.get(deploy_type, "")


def load_migrated_ingresses() -> list[tuple[str, str, str]]:
    """Retorna lista de (project_id, namespace, ingress_name) dos -gw.yaml gerados."""
    result = []
    k8s_dir = HELM_APISIX / "k8s"
    k8s_idx = None
    for yaml_file in sorted(k8s_dir.rglob("config/manifests/**/*-gw.yaml")):
        parts = yaml_file.parts
        if k8s_idx is None:
            k8s_idx = parts.index("k8s")
        project_id = parts[k8s_idx + 1]
        namespace = yaml_file.parent.name
        ingress = yaml_file.stem.removesuffix("-gw")
        # ignorar namespaces de teste/kong fora do escopo de migração nginx
        if namespace in ("kong", "default", "mirror-test", "moteam", "platagen2520"):
            continue
        result.append((project_id, namespace, ingress))
    return result


def load_deploy_types() -> dict[tuple[str, str, str], dict]:
    """Chaveado por (project_id, namespace, ingress) — project_id bate com dir do helm-apisix."""
    rows = {}
    with open(DEPLOY_TYPES_CSV, newline="") as f:
        for row in csv.DictReader(f):
            key = (row["project_id"], row["namespace"], row["ingress"])
            rows[key] = row
    return rows


def load_existing_csv() -> dict[tuple[str, str, str], dict]:
    rows = {}
    with open(EXISTING_CSV, newline="") as f:
        for row in csv.DictReader(f):
            cluster = row.get("cluster", "staging-234557")
            key = (cluster, row["namespace"], row["ingress"])
            rows[key] = row
    return rows


FRONTHUB_DONE = {"front-hub-internal-rdops-systems", "front-hub-rdos-systems", "front-hub-staging.rdstation.com.br"}


def build_row(cluster: str, ns: str, name: str, dt_row: dict | None, existing: dict | None) -> dict:
    # PR #730 foi mergeado: fronthub não precisa mais de observação especial
    if existing:
        row = dict(existing)
        row["cluster"] = cluster
        if ns == "fronthub" and name in FRONTHUB_DONE:
            row["observacao"] = ""
            row["status_migracao"] = "Pronto — só trocar ingressClassName nos values da app"
        # se a classificação mudou de rdsm/rails para manifesto, descarta dados inferidos
        if dt_row and dt_row.get("deploy_type") == "spinnaker+manifesto":
            inferred = row.get("chart", "") in ("rdsm", "rails") and \
                       row.get("repo_chart") in (v[1] for v in DEPLOY_TYPE_DEFAULTS.values())
            if inferred:
                return build_row(cluster, ns, name, dt_row, None)
        # enriquece source_repo de manifesto+Spinnaker se disponível agora
        if not row.get("repo_chart") and dt_row and dt_row.get("source_repo"):
            row["repo_chart"] = dt_row["source_repo"]
            row["observacao"] = ""
        # deploy_types.helm_chart_artifact é a fonte mais confiável (vem de execuções reais).
        # Sempre sobrescreve o chart quando disponível — evita pegar versão de pipeline WIP/canary.
        if dt_row and dt_row.get("helm_chart_artifact"):
            row["chart"] = dt_row["helm_chart_artifact"]
        return row

    if not dt_row:
        return {
            "cluster":       cluster,
            "namespace":     ns,
            "ingress":       name,
            "spinnaker_app": "",
            "chart":         "",
            "repo_chart":    "",
            "observacao":    "sem dados no deploy_types",
            "status_migracao": "",
        }

    deploy_type  = dt_row.get("deploy_type", "")
    helm_chart   = dt_row.get("helm_chart", "")
    spinnaker    = dt_row.get("spinnaker_app", "")
    source_repo  = dt_row.get("source_repo", "")

    chart    = ""
    repo     = ""
    obs      = ""

    if deploy_type in DEPLOY_TYPE_DEFAULTS:
        chart, repo = DEPLOY_TYPE_DEFAULTS[deploy_type]
        # usa versão exata do chart se o Spinnaker informou
        chart = dt_row.get("helm_chart_artifact") or chart
    elif deploy_type == "spinnaker+helm-externo" and helm_chart:
        chart = helm_chart
        repo  = chart_to_repo(helm_chart)
    elif deploy_type == "spinnaker+manifesto":
        repo = source_repo
        obs  = "" if source_repo else "manifesto Spinnaker — repo não encontrado na pipeline"
    elif deploy_type == "manual":
        obs = "manual (Lens/Freelens)"
    elif deploy_type == "outros":
        obs = "kubectl apply direto"

    return {
        "cluster":        cluster,
        "namespace":      ns,
        "ingress":        name,
        "spinnaker_app":  spinnaker,
        "chart":          chart,
        "repo_chart":     repo,
        "observacao":     obs,
        "status_migracao": deploy_type_status(deploy_type),
    }


def main():
    migrated  = load_migrated_ingresses()
    dt_index  = load_deploy_types()
    ex_index  = load_existing_csv()

    rows = []
    for project_id, ns, name in migrated:
        key = (project_id, ns, name)
        row = build_row(project_id, ns, name, dt_index.get(key), ex_index.get(key))
        rows.append(row)

    # Inspeciona charts — uma vez por par (chart, repo) para evitar downloads repetidos
    print("Inspecionando charts...")
    inspection_cache: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (row["chart"], row["repo_chart"])
        if key not in inspection_cache:
            label = row["chart"] or "(sem chart)"
            print(f"  {label:42s}", end=" ", flush=True)
            result = inspect_ingress_class(row["chart"], row["repo_chart"])
            print(result)
            inspection_cache[key] = result

    for row in rows:
        row["ingress_class"] = inspection_cache[(row["chart"], row["repo_chart"])]
        app = row.get("spinnaker_app", "")
        row["spinnaker_url"] = f"https://spinnaker.rdops.systems/#/applications/{app}/executions" if app else ""

    fieldnames = [
        "cluster", "namespace", "ingress", "spinnaker_app",
        "chart", "repo_chart", "ingress_class",
        "spinnaker_url", "observacao", "status_migracao",
    ]
    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    # Resumo
    ic_counts = Counter(r["ingress_class"] for r in rows)
    print(f"\nTotal migrados : {len(rows)}")
    print("ingress_class  :")
    for label, n in ic_counts.most_common():
        print(f"  {n:3d}  {label}")
    print(f"\nCSV gerado: {OUT_CSV}")


if __name__ == "__main__":
    main()
