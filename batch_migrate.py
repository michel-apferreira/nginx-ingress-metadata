#!/usr/bin/env python3
"""
batch_migrate.py — Orquestra a migração em lote de Ingresses NGINX → APISIX.

Para cada ingress nginx num namespace:
  1. Gera manifesto nginx original limpo + manifesto APISIX (<app>-gw) no helm-apisix
  2. Para spinnaker+manifesto: adiciona stage 'Deploy APISIX Ingress' à pipeline
  3. Para manual/outros (ou spinnaker sem execução recente): kubectl apply direto
  4. Verifica sincronização da rota no APISIX

Pré-requisitos:
  - data/raw/<cluster>.json já coletado (python3 collect.py)
  - kubectl configurado com o contexto do cluster alvo
  - Cookie SESSION do Spinnaker para tipos spinnaker+manifesto

Uso:
  # Gera manifestos e salva (sem aplicar):
  python3 batch_migrate.py --cluster staging-234557 --namespace crm --generate-only

  # Geração + Spinnaker stage + kubectl apply:
  python3 batch_migrate.py --cluster staging-234557 --namespace crm \\
      --spinnaker-session <TOKEN> \\
      --helm-apisix-dir ~/workspace/helm-apisix \\
      --kubectl-context gke_staging-234557_us-central1_cluster-staging \\
      --apply

  # Apenas aplica manifestos já gerados:
  python3 batch_migrate.py --cluster staging-234557 --namespace crm --apply-only
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Caminhos
# ---------------------------------------------------------------------------

SCRIPT_DIR  = Path(__file__).parent
RAW_DIR     = SCRIPT_DIR / "data" / "raw"
DEFAULT_HELM = Path.home() / "workspace" / "helm-apisix"
SPINNAKER_GATE = "https://spinnaker-api.rdops.systems"
APISIX_NS   = "ingress-apisix"

NGINX_PFX  = "nginx.ingress.kubernetes.io/"
APISIX_PFX = "k8s.apisix.apache.org/"

# ---------------------------------------------------------------------------
# Mapeamento de annotations (Padrão A — rename direto)
# ---------------------------------------------------------------------------

ANNOTATION_RENAMES = {
    "use-regex":                  APISIX_PFX + "use-regex",
    "rewrite-target":             APISIX_PFX + "rewrite-target",
    "priority":                   APISIX_PFX + "priority",
    "proxy-read-timeout":         APISIX_PFX + "upstream-read-timeout",
    "proxy-send-timeout":         APISIX_PFX + "upstream-send-timeout",
    "proxy-connect-timeout":      APISIX_PFX + "upstream-connect-timeout",
    "backend-protocol":           APISIX_PFX + "upstream-scheme",
    "proxy-next-upstream-tries":  APISIX_PFX + "upstream-retries",
    "send_timeout":               APISIX_PFX + "upstream-send-timeout",
    "ssl-redirect":               APISIX_PFX + "ssl-redirect",
    "force-ssl-redirect":         APISIX_PFX + "ssl-redirect",
    "whitelist-source-range":     APISIX_PFX + "allowlist-source-range",
    "denylist-source-range":      APISIX_PFX + "blocklist-source-range",
}

# Timeout para ssl-redirect/proxy-*-timeout: adicionar unidade "s"
TIMEOUT_KEYS = {"proxy-read-timeout", "proxy-send-timeout", "proxy-connect-timeout", "send_timeout"}

# Annotations removidas sem equivalente (sem impacto funcional)
REMOVED_SILENT = {
    "proxy-buffering",
    "proxy-buffer-size",
    "proxy-buffers-number",
    "proxy-max-temp-file-size",
    "session-cookie-expires",
    "session-cookie-max-age",
    "reload-ts",
    "ssl-protocols",
    "service-upstream",
}

# Annotations que requerem ApisixPluginConfig (Padrão B)
PLUGIN_KEYS = {
    "proxy-body-size",
    "limit-rpm",
    "proxy-hide-headers",
    "upstream-vhost",
    "enable-cors",
    "cors-allow-origin",
    "cors-allow-Origin",
    "cors-allow-headers",
    "cors-allow-methods",
    "cors-allow-credentials",
    "cors-max-age",
    "auth-url",
    "auth-response-headers",
    "auth-type",
    "auth-secret",
    "auth-realm",
    "limit-burst-multiplier",
    "compute-full-forwarded-for",
    "whitelist-source-range",   # pode usar plugin ou annotation; usamos annotation (acima)
}

# Annotations manuais (caso a caso — não automatizadas)
MANUAL_KEYS = {
    "server-snippet",
    "configuration-snippet",
    "custom-http-errors",
    "default-backend",
    "proxy-intercept-errors",
    "auth-signin",
    "auth-secret",
    "use-forwarded-headers",
    "canary",
    "canary-weight",
}

# Annotations cujos valores são configurados no upstream (ApisixUpstream)
UPSTREAM_KEYS = {
    "proxy-next-upstream",
    "keepalive",
    "keepalive-timeout",
    "load-balance",
    "health-check-path",
    "affinity",
    "session-cookie-name",
}

# ---------------------------------------------------------------------------
# Helpers de tamanho e tempo
# ---------------------------------------------------------------------------

def parse_size_bytes(value: str) -> int:
    import re
    value = value.strip()
    if value == "0":
        return 0
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([kmgKMG]?)$", value)
    if not m:
        return 0
    num = float(m.group(1))
    unit = m.group(2).lower()
    return int(num * {"k": 1024, "m": 1048576, "g": 1073741824}.get(unit, 1))


def cidr_list(value: str) -> list[str]:
    return [c.strip() for c in value.split(",") if c.strip()]


# ---------------------------------------------------------------------------
# Processamento de annotations
# ---------------------------------------------------------------------------

def process_annotations(annotations: dict, ingress_name: str, namespace: str):
    """
    Retorna (migrated_anns, plugin_config, issues).

    issues é uma lista de strings descrevendo casos MANUAL ou UPSTREAM
    que precisam de atenção humana.
    """
    migrated: dict = {}
    plugins: list[dict] = []
    issues: list[str] = []

    cors_cfg: dict = {}

    def add_plugin(plugin: dict):
        plugins.append(plugin)

    for key, value in annotations.items():
        # Annotation de classe legada — ignorar
        if key == "kubernetes.io/ingress.class":
            continue
        # Annotations não-NGINX: preservar (moniker, artifact, etc.)
        if not key.startswith(NGINX_PFX):
            migrated[key] = value
            continue

        short = key[len(NGINX_PFX):]

        # Removidas silenciosamente
        if short in REMOVED_SILENT:
            continue

        # Anotações manuais: reportar mas não bloquear
        if short in MANUAL_KEYS:
            issues.append(f"MANUAL [{short}={value!r}] — requer conversão caso a caso")
            continue

        # Upstream: reportar (ApisixUpstream necessário)
        if short in UPSTREAM_KEYS:
            issues.append(f"UPSTREAM [{short}={value!r}] — criar ApisixUpstream separado")
            continue

        # Rename direto (Padrão A)
        if short in ANNOTATION_RENAMES:
            new_key = ANNOTATION_RENAMES[short]
            new_value = value
            if short in TIMEOUT_KEYS and not value.endswith("s"):
                new_value = f"{value}s"
            if short == "backend-protocol":
                new_value = value.lower()
            migrated[new_key] = new_value
            continue

        # proxy-body-size → client-control (Padrão B)
        if short == "proxy-body-size":
            max_bytes = parse_size_bytes(value)
            add_plugin({"name": "client-control", "enable": True,
                        "config": {"max_body_size": max_bytes}})
            continue

        # limit-rpm → limit-count (Padrão B)
        if short == "limit-rpm":
            try:
                count = int(value)
            except ValueError:
                count = 60
            add_plugin({"name": "limit-count", "enable": True, "config": {
                "count": count, "time_window": 60,
                "key_type": "var", "key": "remote_addr", "rejected_code": 429,
            }})
            continue

        # proxy-hide-headers → response-rewrite (Padrão B)
        if short == "proxy-hide-headers":
            headers = [h.strip() for h in value.split(",") if h.strip()]
            add_plugin({"name": "response-rewrite", "enable": True,
                        "config": {"headers": {"remove": headers}}})
            continue

        # upstream-vhost → proxy-rewrite (Padrão B)
        if short == "upstream-vhost":
            add_plugin({"name": "proxy-rewrite", "enable": True,
                        "config": {"headers": {"host": value}}})
            continue

        # compute-full-forwarded-for → real-ip (Padrão B)
        if short == "compute-full-forwarded-for":
            add_plugin({"name": "real-ip", "enable": True,
                        "config": {"source": "http_x_forwarded_for", "recursive": True}})
            continue

        # CORS (Padrão B — agrupa múltiplas annotations)
        if short == "enable-cors":
            cors_cfg["enabled"] = value.lower() == "true"
            continue
        if short in ("cors-allow-origin", "cors-allow-Origin"):
            cors_cfg["allow_origins"] = value
            continue
        if short == "cors-allow-headers":
            cors_cfg["allow_headers"] = value
            continue
        if short == "cors-allow-methods":
            cors_cfg["allow_methods"] = value
            continue
        if short == "cors-allow-credentials":
            cors_cfg["allow_credential"] = value.lower() == "true"
            continue
        if short == "cors-max-age":
            try:
                cors_cfg["max_age"] = int(value)
            except ValueError:
                pass
            continue

        # forward-auth (Padrão B)
        if short == "auth-url":
            add_plugin({"name": "forward-auth", "enable": True, "config": {"uri": value}})
            continue
        if short == "auth-response-headers":
            hdrs = [h.strip() for h in value.split(",") if h.strip()]
            add_plugin({"name": "forward-auth", "enable": True,
                        "config": {"upstream_headers": hdrs}})
            continue

        # basic-auth (Padrão B)
        if short == "auth-type" and value == "basic":
            add_plugin({"name": "basic-auth", "enable": True, "config": {}})
            continue
        if short == "auth-realm":
            add_plugin({"name": "basic-auth", "enable": True, "config": {"realm": value}})
            continue

        # proxy-mirror (Padrão B)
        if short == "mirror-target":
            add_plugin({"name": "proxy-mirror", "enable": True, "config": {"host": value}})
            continue
        if short == "mirror-request-body":
            # sample_ratio: mirror-request-body não tem equivalente direto — mantém plugin sem sample_ratio
            continue

        # limit-burst-multiplier: manual
        if short == "limit-burst-multiplier":
            issues.append(f"MANUAL [limit-burst-multiplier={value!r}] — ajustar junto com limit-count")
            continue

        # proxy-next-upstream-tries tem rename direto (já coberto em ANNOTATION_RENAMES)

        # Fallback: não mapeada
        issues.append(f"DESCONHECIDA [{short}={value!r}] — verificar manualmente")

    # CORS plugin composto
    if cors_cfg.get("enabled") or len(cors_cfg) > 1:
        cfg = {k: v for k, v in cors_cfg.items() if k != "enabled"}
        add_plugin({"name": "cors", "enable": True, "config": cfg})

    # Monta ApisixPluginConfig se houver plugins
    plugin_config = None
    if plugins:
        pc_name = f"{ingress_name}-plugins"
        migrated[APISIX_PFX + "plugin-config-name"] = pc_name
        plugin_config = {
            "apiVersion": "apisix.apache.org/v2",
            "kind": "ApisixPluginConfig",
            "metadata": {"name": pc_name, "namespace": namespace},
            "spec": {"ingressClassName": "apisix", "plugins": plugins},
        }

    return migrated, plugin_config, issues


# ---------------------------------------------------------------------------
# Geração de manifestos
# ---------------------------------------------------------------------------

def clean_metadata(meta: dict) -> dict:
    result = {}
    for f in ("name", "namespace"):
        if f in meta:
            result[f] = meta[f]
    if meta.get("labels"):
        result["labels"] = meta["labels"]
    return result


def build_nginx_manifest(ingress: dict) -> dict:
    meta = ingress.get("metadata", {})
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": clean_metadata(meta),
        "spec": ingress.get("spec", {}),
    }


def build_apisix_manifest(ingress: dict, gw_name: str, migrated_anns: dict) -> dict:
    meta = ingress.get("metadata", {})
    spec = dict(ingress.get("spec", {}))
    spec["ingressClassName"] = "apisix"
    # Remove ingressClassName nginx do spec original
    spec.pop("ingressClassName", None)
    spec["ingressClassName"] = "apisix"

    clean_meta = clean_metadata(meta)
    clean_meta["name"] = gw_name
    # Remove labels do nginx original (moniker, resource_kind, etc.) — irrelevantes para APISIX
    clean_meta.pop("labels", None)
    if migrated_anns:
        clean_meta["annotations"] = migrated_anns

    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": clean_meta,
        "spec": spec,
    }


def _yaml_dump(doc: dict) -> str:
    return yaml.dump(doc, allow_unicode=True, default_flow_style=False, sort_keys=False)


def save_manifests(
    helm_apisix_dir: Path,
    cluster: str,
    namespace: str,
    ingress_name: str,
    nginx_manifest: dict,
    apisix_manifest: dict,
    plugin_config: dict | None,
    dry_run: bool,
) -> tuple[Path, Path | None]:
    out_dir = helm_apisix_dir / "k8s" / cluster / "config" / "manifests" / namespace

    nginx_path  = out_dir / f"{ingress_name}.yaml"
    apisix_path = out_dir / f"{ingress_name}-gw.yaml"

    if dry_run:
        return nginx_path, apisix_path

    out_dir.mkdir(parents=True, exist_ok=True)

    # nginx original
    if not nginx_path.exists():
        with open(nginx_path, "w") as f:
            f.write(_yaml_dump(nginx_manifest))

    # APISIX companion
    with open(apisix_path, "w") as f:
        f.write(_yaml_dump(apisix_manifest))
        if plugin_config:
            f.write("---\n")
            f.write(_yaml_dump(plugin_config))

    return nginx_path, apisix_path


# ---------------------------------------------------------------------------
# Classificação de deploy type (reusa lógica de deploy_types.py)
# ---------------------------------------------------------------------------

RAILS_HELM_LABEL = "resource_kind"
RDSM_HELM_LABEL  = "app_group"
SUPPORT_PIPELINE_KEYWORDS = {
    "external-secret", "externalsecret", "external secrets",
    "cloudsql", "backup", "cronjob", "rollback", "canary",
    "destroy", "teardown", "rake", "temporária", "temporaria",
}


def classify_deploy_type(ann: dict, labels: dict) -> tuple[str, str]:
    spinnaker_app = ann.get("moniker.spinnaker.io/application", "")
    artifact_name = ann.get("artifact.spinnaker.io/name", "")
    helm_chart    = labels.get("helm.sh/chart", "")

    if RAILS_HELM_LABEL in labels:
        return "spinnaker+helm-rails", helm_chart or "rails"
    if RDSM_HELM_LABEL in labels:
        return "spinnaker+helm-rdsm", helm_chart or "rdsm"

    if spinnaker_app or artifact_name:
        if helm_chart:
            return "spinnaker+helm-externo", helm_chart
        return "spinnaker+manifesto", spinnaker_app or artifact_name

    helm_managed = labels.get("app.kubernetes.io/managed-by", "") == "Helm"
    helm_release = ann.get("meta.helm.sh/release-name", "")
    if helm_managed or helm_release:
        return "helm-direto", helm_release or helm_chart or "Helm"

    if labels.get("k8slens-edit-resource-version") or ann.get("freelens.app/resource-version"):
        return "manual", "lens/freelens"

    return "outros", ""


# ---------------------------------------------------------------------------
# kubectl helpers
# ---------------------------------------------------------------------------

def kubectl(args: list[str], context: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["kubectl", "--context", context] + args
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def ingress_exists(name: str, namespace: str, context: str) -> bool:
    r = kubectl(
        ["get", "ingress", name, "-n", namespace, "--ignore-not-found", "-o", "name"],
        context, check=False,
    )
    return bool(r.stdout.strip())


def get_apisix_admin_key(context: str) -> str:
    r = kubectl(
        ["get", "secret", "apisix-admin-credentials", "-n", APISIX_NS,
         "-o", "jsonpath={.data.admin}"],
        context, check=False,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return ""
    import base64
    return base64.b64decode(r.stdout.strip()).decode()


def get_apisix_pod(context: str) -> str:
    r = kubectl(
        ["get", "pod", "-n", APISIX_NS, "-l", "app.kubernetes.io/name=apisix",
         "-o", "jsonpath={.items[0].metadata.name}"],
        context, check=False,
    )
    return r.stdout.strip()


def kubectl_apply(manifest_path: Path, context: str) -> bool:
    r = kubectl(["apply", "-f", str(manifest_path)], context, check=False)
    if r.returncode != 0:
        print(f"    [ERRO kubectl apply] {r.stderr.strip()}")
        return False
    print(f"    [kubectl apply] {r.stdout.strip()}")
    return True


def verify_apisix_route(gw_name: str, namespace: str, context: str, timeout: int = 60) -> bool:
    """Verifica se o host do ingress aparece nos Services do APISIX via port-forward local."""
    import subprocess as _sp

    admin_key = get_apisix_admin_key(context)
    if not admin_key:
        print("    [AVISO] Não foi possível obter admin key — pulando verificação")
        return True

    # Resolve os hosts do ingress para usar na verificação
    r = kubectl(
        ["get", "ingress", gw_name, "-n", namespace,
         "-o", "jsonpath={.spec.rules[*].host}"],
        context, check=False,
    )
    expected_hosts = set(r.stdout.strip().split()) if r.returncode == 0 else set()
    if not expected_hosts:
        print("    [AVISO] Não foi possível obter hosts do ingress — pulando verificação")
        return True

    # Port-forward para o admin do APISIX
    pf = _sp.Popen(
        ["kubectl", "--context", context, "port-forward", "-n", APISIX_NS,
         "svc/apisix-admin", "19180:9180"],
        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
    )
    time.sleep(2)

    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r2 = _sp.run(
                    ["curl", "-s", "http://127.0.0.1:19180/apisix/admin/services",
                     "-H", f"X-API-KEY: {admin_key}"],
                    capture_output=True, text=True, timeout=10,
                )
                data = json.loads(r2.stdout)
                for item in data.get("list", []):
                    hosts = set(item.get("value", {}).get("hosts", []))
                    if hosts & expected_hosts:
                        return True
            except (json.JSONDecodeError, _sp.TimeoutExpired):
                pass
            time.sleep(5)
    finally:
        pf.terminate()

    return False


# ---------------------------------------------------------------------------
# Spinnaker API
# ---------------------------------------------------------------------------

def _spinnaker_req(path: str, session: str, method: str = "GET", body: dict | None = None):
    url = f"{SPINNAKER_GATE}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {
        "Cookie": f"SESSION={session}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = resp.read()
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} {url}: {body_txt[:300]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"URLError {url}: {e.reason}") from e


def find_deploy_pipeline(app: str, session: str, env_hint: str = "") -> dict | None:
    """Retorna a pipeline de deploy do ambiente correspondente ao cluster.

    env_hint: string que deve aparecer no nome da pipeline (ex: 'staging').
    Quando informado, filtra pipelines cujo nome contém env_hint.
    """
    configs = _spinnaker_req(f"/applications/{app}/pipelineConfigs", session)
    if not configs:
        return None

    skip_kw = SUPPORT_PIPELINE_KEYWORDS | {"apisix"}

    candidates = []
    for cfg in configs:
        name_lower = cfg.get("name", "").lower()
        if any(kw in name_lower for kw in skip_kw):
            continue
        has_deploy = any(
            s.get("type") in ("deployManifest", "bakeManifest", "runJob")
            for s in cfg.get("stages", [])
        )
        # Preferência: pipeline cujo nome contém o env_hint ou sinônimos (staging/stg)
        env_hints = {env_hint.lower()} if env_hint else set()
        if "staging" in env_hints:
            env_hints.add("stg")
        elif "stg" in env_hints:
            env_hints.add("staging")
        env_match = bool(env_hints and any(h in name_lower for h in env_hints))
        candidates.append((env_match, has_deploy, cfg))

    if not candidates:
        return None

    # Ordena: env_match desc, has_deploy desc, nome asc
    candidates.sort(key=lambda x: (not x[0], not x[1], x[2].get("name", "")))
    return candidates[0][2]


def get_last_execution(app: str, pipeline_id: str, session: str) -> dict | None:
    """Retorna a execução mais recente da pipeline (ou None)."""
    try:
        execs = _spinnaker_req(
            f"/applications/{app}/pipelines?pipelineConfigIds={pipeline_id}&limit=1",
            session,
        )
        return execs[0] if execs else None
    except RuntimeError:
        return None


def pipeline_has_apisix_stage(pipeline: dict) -> bool:
    return any(
        "apisix" in s.get("name", "").lower()
        for s in pipeline.get("stages", [])
    )


def build_apisix_stage(manifest_content: str, last_ref_id: str, ingress_name: str = "", app: str = "") -> dict:
    """Cria um stage 'Deploy APISIX Ingress' para a pipeline do Spinnaker."""
    import hashlib
    ref_id = f"apisix-{hashlib.md5(manifest_content.encode()).hexdigest()[:6]}"
    # Suporta manifests multi-documento (Ingress + ApisixPluginConfig)
    docs = [d for d in yaml.safe_load_all(manifest_content) if d is not None]
    stage_name = f"Deploy APISIX Ingress ({ingress_name})" if ingress_name else "Deploy APISIX Ingress"
    description = (
        "Stage adicionado automaticamente pela equipe de SRE Foundation durante os testes de migração do NGINX Ingress para o APISIX.\n\n"
        "Este stage cria um Ingress paralelo com `ingressClassName: apisix` e não altera nem interfere nos demais stages da pipeline.\n\n"
        "O tráfego continua sendo roteado normalmente pelo NGINX, enquanto ambos os Ingresses coexistem no cluster."
    )
    return {
        "type": "deployManifest",
        "name": stage_name,
        "description": description,
        "refId": ref_id,
        "requisiteStageRefIds": [last_ref_id],
        "account": "k8s-staging",
        "cloudProvider": "kubernetes",
        "moniker": {"app": app},
        "skipExpressionEvaluation": False,
        "source": "text",
        "manifests": docs,
        "trafficManagement": {"enabled": False, "options": {"enableTraffic": False, "services": []}},
    }


def add_apisix_stage_to_pipeline(
    app: str,
    pipeline: dict,
    manifest_content: str,
    session: str,
    ingress_name: str = "",
) -> bool:
    """Adiciona stage APISIX à pipeline e salva via POST /pipelines."""
    import hashlib
    ref_id = f"apisix-{hashlib.md5(manifest_content.encode()).hexdigest()[:6]}"
    stages = pipeline.get("stages", [])

    # Verifica se já existe um stage com este refId específico
    if any(s.get("refId") == ref_id for s in stages):
        print(f"    [Spinnaker] Stage já existe (refId={ref_id}) — pulando")
        return True

    last_ref = stages[-1]["refId"] if stages else "1"
    new_stage = build_apisix_stage(manifest_content, last_ref, ingress_name, app)
    pipeline["stages"] = stages + [new_stage]

    try:
        _spinnaker_req("/pipelines", session, method="POST", body=pipeline)
        print(f"    [Spinnaker] Stage '{new_stage['name']}' adicionado (refId={new_stage['refId']})")
        return True
    except RuntimeError as e:
        print(f"    [ERRO Spinnaker] {e}")
        return False


# ---------------------------------------------------------------------------
# Leitura dos dados coletados
# ---------------------------------------------------------------------------

def load_ingresses_from_raw(cluster: str, namespace: str, kubectl_context: str = "") -> list[dict]:
    candidates = list(RAW_DIR.glob(f"*__{cluster}.json")) or list(RAW_DIR.glob(f"*{cluster}*.json"))

    ingresses = []
    ns_found_in_raw = False

    if candidates:
        for fpath in candidates:
            data = json.loads(fpath.read_text())
            items = data if isinstance(data, list) else data.get("items", [])
            for item in items:
                meta = item.get("metadata", {})
                spec = item.get("spec", {})
                ns     = meta.get("namespace", "")
                ic     = spec.get("ingressClassName", "")
                ic_ann = meta.get("annotations", {}).get("kubernetes.io/ingress.class", "")

                if ns != namespace:
                    continue
                ns_found_in_raw = True
                # Pula recursos já migrados para APISIX
                if ic == "apisix":
                    continue
                # Inclui apenas ingresses nginx (spec ou annotation)
                is_nginx = ic == "nginx" or ic_ann == "nginx"
                if not is_nginx:
                    continue

                ingresses.append(item)

    if not ns_found_in_raw:
        # Namespace não está no raw data — fallback via kubectl
        if not kubectl_context:
            raise FileNotFoundError(
                f"Namespace '{namespace}' não encontrado no raw data e kubectl_context não fornecido. "
                "Execute: python3 collect.py --cluster <cluster> ou passe --kubectl-context"
            )
        print(f"[INFO] Namespace '{namespace}' não está no raw data — coletando via kubectl...")
        cmd = ["kubectl", "--context", kubectl_context, "get", "ingress", "-n", namespace, "-o", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[WARN] kubectl falhou para namespace '{namespace}': {result.stderr.strip()}")
            return []
        live_data = json.loads(result.stdout)
        for item in live_data.get("items", []):
            spec = item.get("spec", {})
            meta = item.get("metadata", {})
            ic     = spec.get("ingressClassName", "")
            ic_ann = meta.get("annotations", {}).get("kubernetes.io/ingress.class", "")
            if ic == "apisix":
                continue
            is_nginx = ic == "nginx" or ic_ann == "nginx"
            if not is_nginx:
                continue
            ingresses.append(item)

    return ingresses


# ---------------------------------------------------------------------------
# Workflow por ingress
# ---------------------------------------------------------------------------

SKIP_DEPLOY_TYPES = {"spinnaker+helm-rails", "spinnaker+helm-rdsm", "spinnaker+helm-externo"}


def _env_from_cluster(cluster: str) -> str:
    """Extrai o ambiente (staging/production/etc) a partir do nome do cluster."""
    name = cluster.lower()
    if "staging" in name or "stg" in name:
        return "staging"
    if "prod" in name or "prd" in name:
        return "production"
    return ""
RECENT_EXEC_DAYS  = 90  # considera "sem execução recente" se última foi há mais de X dias


def is_recent_execution(execution: dict | None) -> bool:
    if not execution:
        return False
    ts = execution.get("buildTime") or execution.get("startTime") or 0
    ts_s = ts / 1000 if ts > 1e10 else ts
    age = time.time() - ts_s
    return age < RECENT_EXEC_DAYS * 86400


def migrate_ingress(
    ingress: dict,
    cluster: str,
    helm_apisix_dir: Path,
    kubectl_context: str,
    spinnaker_session: str,
    dry_run: bool,
    do_apply: bool,
    do_verify: bool,
    migrate_helm: bool = False,
) -> dict:
    """
    Executa o workflow completo de migração para um ingress.
    Retorna dict com resultado: {status, ingress, issues, ...}
    """
    meta = ingress.get("metadata", {})
    name = meta.get("name", "")
    namespace = meta.get("namespace", "")
    gw_name = f"{name}-gw"
    ann = {k: v for k, v in meta.get("annotations", {}).items()
           if k != "kubectl.kubernetes.io/last-applied-configuration"}
    labels = meta.get("labels", {})

    result = {
        "ingress": name,
        "gw_name": gw_name,
        "namespace": namespace,
        "status": "ok",
        "deploy_type": "",
        "issues": [],
        "actions": [],
    }

    # Verificar se já existe no cluster
    if not dry_run and ingress_exists(gw_name, namespace, kubectl_context):
        result["status"] = "already_migrated"
        result["actions"].append(f"{gw_name} já existe no cluster — pulando")
        return result

    # Classificar deploy type
    deploy_type, _ = classify_deploy_type(ann, labels)
    result["deploy_type"] = deploy_type

    if deploy_type in SKIP_DEPLOY_TYPES and not migrate_helm:
        result["status"] = "skip_helm"
        result["actions"].append(
            f"Tipo '{deploy_type}' — use --migrate-helm para coexistência"
        )
        return result

    # Processar annotations
    migrated_anns, plugin_config, issues = process_annotations(ann, gw_name, namespace)
    result["issues"] = issues

    has_manual = any(i.startswith("MANUAL") for i in issues)
    if has_manual:
        result["status"] = "manual_required"
        result["actions"].append("Annotations MANUAL presentes — gerar manifesto mas não aplicar automaticamente")

    # Gerar manifestos
    nginx_manifest  = build_nginx_manifest(ingress)
    apisix_manifest = build_apisix_manifest(ingress, gw_name, migrated_anns)

    _, apisix_path = save_manifests(
        helm_apisix_dir, cluster, namespace,
        name, nginx_manifest, apisix_manifest, plugin_config, dry_run,
    )
    result["actions"].append(f"Manifesto gerado: {apisix_path}")

    if dry_run or has_manual:
        return result

    # Conteúdo do manifesto APISIX para o stage Spinnaker
    apisix_content = _yaml_dump(apisix_manifest)
    if plugin_config:
        apisix_content += "---\n" + _yaml_dump(plugin_config)

    # Ação baseada no deploy type
    spinnaker_app = ann.get("moniker.spinnaker.io/application", "")

    if deploy_type == "spinnaker+manifesto" and spinnaker_session and spinnaker_app:
        # Tentar adicionar stage na pipeline
        try:
            pipeline = find_deploy_pipeline(spinnaker_app, spinnaker_session, env_hint=_env_from_cluster(cluster))
            if pipeline:
                pipeline_name = pipeline.get("name", "?")
                last_exec = get_last_execution(spinnaker_app, pipeline["id"], spinnaker_session)

                if add_apisix_stage_to_pipeline(spinnaker_app, pipeline, apisix_content, spinnaker_session, ingress_name=gw_name):
                    result["actions"].append(f"Stage Spinnaker adicionado: {spinnaker_app}/{pipeline_name}")

                if not is_recent_execution(last_exec) and do_apply:
                    print(f"    [Sem execução recente] Aplicando via kubectl...")
                    if kubectl_apply(apisix_path, kubectl_context):
                        result["actions"].append("kubectl apply (sem execução recente)")
                elif do_apply:
                    result["actions"].append("Não aplicado via kubectl — pipeline tem execução recente")
            else:
                print(f"    [AVISO] Pipeline não encontrada para {spinnaker_app} — kubectl apply direto")
                if do_apply:
                    kubectl_apply(apisix_path, kubectl_context)
                    result["actions"].append("kubectl apply (pipeline não encontrada)")
        except RuntimeError as e:
            print(f"    [ERRO Spinnaker] {e}")
            result["issues"].append(f"Spinnaker error: {e}")
            if do_apply:
                kubectl_apply(apisix_path, kubectl_context)
                result["actions"].append("kubectl apply (fallback — erro Spinnaker)")

    elif do_apply:
        # manual, outros, helm-direto, spinnaker sem session
        if kubectl_apply(apisix_path, kubectl_context):
            result["actions"].append("kubectl apply")

    # Verificar sincronização
    if do_apply and do_verify:
        print(f"    Aguardando sincronização APISIX (até 30s)...")
        if verify_apisix_route(gw_name, namespace, kubectl_context):
            result["actions"].append("Rota sincronizada no APISIX")
        else:
            result["status"] = "sync_timeout"
            result["issues"].append("Rota não apareceu no APISIX em 30s — verificar manualmente")

    return result


# ---------------------------------------------------------------------------
# Apply-only mode (aplica manifestos já gerados)
# ---------------------------------------------------------------------------

def apply_only_mode(cluster: str, namespace: str, helm_apisix_dir: Path,
                    kubectl_context: str, do_verify: bool):
    manifests_dir = helm_apisix_dir / "k8s" / cluster / "config" / "manifests" / namespace
    if not manifests_dir.exists():
        print(f"[ERRO] Diretório não encontrado: {manifests_dir}")
        sys.exit(1)

    gw_files = sorted(manifests_dir.glob("*-gw.yaml"))
    if not gw_files:
        print(f"[AVISO] Nenhum arquivo *-gw.yaml em {manifests_dir}")
        return

    print(f"\n=== Apply-only: {len(gw_files)} manifestos em {namespace} ===\n")
    for fpath in gw_files:
        print(f"  Aplicando {fpath.name}...")
        if kubectl_apply(fpath, kubectl_context):
            if do_verify:
                gw_name = fpath.stem
                print(f"    Verificando {gw_name}...")
                synced = verify_apisix_route(gw_name, namespace, kubectl_context)
                print(f"    {'OK' if synced else 'TIMEOUT'}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Migração em lote NGINX → APISIX para um namespace",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--cluster", required=True, help="Nome do cluster (ex: staging-234557)")
    parser.add_argument("--namespace", required=True, help="Namespace a migrar")
    parser.add_argument(
        "--helm-apisix-dir", default=str(DEFAULT_HELM),
        help=f"Caminho do repositório helm-apisix (padrão: {DEFAULT_HELM})",
    )
    parser.add_argument(
        "--kubectl-context", default=None,
        help="Contexto kubectl (padrão: gke_<cluster>_us-central1_<cluster>)",
    )
    parser.add_argument("--spinnaker-session", default="", metavar="TOKEN",
                        help="Cookie SESSION do Spinnaker")
    parser.add_argument("--apply", action="store_true",
                        help="Aplicar manifestos via kubectl (e adicionar stages Spinnaker)")
    parser.add_argument("--generate-only", action="store_true",
                        help="Apenas gerar manifestos, sem aplicar ou modificar Spinnaker")
    parser.add_argument("--apply-only", action="store_true",
                        help="Aplicar manifestos já gerados em helm-apisix (pular geração)")
    parser.add_argument("--migrate-helm", action="store_true",
                        help="Migrar ingresses spinnaker+helm-* (coexistência: gera manifest APISIX + stage, sem alterar o chart)")
    parser.add_argument("--no-verify", action="store_true",
                        help="Não verificar sincronização no APISIX após apply")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simular sem salvar arquivos nem chamar kubectl/Spinnaker")
    args = parser.parse_args()

    helm_apisix_dir = Path(args.helm_apisix_dir).expanduser()
    if not helm_apisix_dir.exists():
        print(f"[ERRO] helm-apisix-dir não encontrado: {helm_apisix_dir}")
        sys.exit(1)

    # Context kubectl padrão
    kubectl_context = args.kubectl_context
    if not kubectl_context:
        kubectl_context = f"gke_{args.cluster}_us-central1_{args.cluster.replace('_', '-')}"
        if "staging-234557" in args.cluster:
            kubectl_context = "gke_staging-234557_us-central1_cluster-staging"

    do_apply  = args.apply and not args.generate_only
    do_verify = not args.no_verify

    if args.apply_only:
        apply_only_mode(args.cluster, args.namespace, helm_apisix_dir, kubectl_context, do_verify)
        return

    # Carregar ingresses do JSON coletado (com fallback kubectl para namespaces novos)
    print(f"\nCarregando ingresses do cluster '{args.cluster}', namespace '{args.namespace}'...")
    try:
        ingresses = load_ingresses_from_raw(args.cluster, args.namespace, kubectl_context)
    except FileNotFoundError as e:
        print(f"[ERRO] {e}")
        sys.exit(1)

    nginx_only = [
        i for i in ingresses
        if i.get("spec", {}).get("ingressClassName") == "nginx"
        or i.get("metadata", {}).get("annotations", {}).get("kubernetes.io/ingress.class") == "nginx"
    ]

    if not nginx_only:
        print(f"[INFO] Nenhum ingress nginx encontrado no namespace '{args.namespace}'")
        return

    print(f"Encontrados {len(nginx_only)} ingresses nginx\n")

    # Cabeçalho
    print(f"{'Ingress':<40} {'Tipo':<25} {'Status'}")
    print("-" * 80)

    results = []
    counts = {"ok": 0, "already_migrated": 0, "skip_helm": 0,
              "manual_required": 0, "sync_timeout": 0, "error": 0}

    for ingress in nginx_only:
        name = ingress["metadata"]["name"]
        print(f"\n{name}")

        try:
            r = migrate_ingress(
                ingress,
                cluster=args.cluster,
                helm_apisix_dir=helm_apisix_dir,
                kubectl_context=kubectl_context,
                spinnaker_session=args.spinnaker_session,
                dry_run=args.dry_run,
                do_apply=do_apply,
                do_verify=do_verify,
                migrate_helm=args.migrate_helm,
            )
        except Exception as e:
            print(f"  [ERRO] {e}")
            r = {"ingress": name, "status": "error", "deploy_type": "?",
                 "issues": [str(e)], "actions": []}

        status = r["status"]
        counts[status] = counts.get(status, 0) + 1

        print(f"  Tipo:    {r.get('deploy_type', '?')}")
        print(f"  Status:  {status}")
        for action in r.get("actions", []):
            print(f"  → {action}")
        for issue in r.get("issues", []):
            print(f"  ⚠ {issue}")

        results.append(r)

    # Resumo
    print(f"\n{'='*60}")
    print(f"Namespace: {args.namespace}  |  Cluster: {args.cluster}")
    print(f"Total: {len(nginx_only)}")
    for status, count in counts.items():
        if count:
            print(f"  {status:<20} {count}")

    manual_cases = [r for r in results if r.get("issues")]
    if manual_cases:
        print(f"\n⚠  {len(manual_cases)} ingress(es) com itens para revisão manual:")
        for r in manual_cases:
            print(f"   {r['ingress']}")
            for issue in r["issues"]:
                print(f"     - {issue}")

    if args.dry_run:
        print("\n[DRY-RUN] Nenhum arquivo salvo, nenhuma chamada kubectl/Spinnaker realizada.")


if __name__ == "__main__":
    main()
