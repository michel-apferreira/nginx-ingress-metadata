#!/usr/bin/env python3
"""
check_nginx_lb.py — Verifica o modelo de Load Balancer do NGINX IC em todos os clusters.

Para cada cluster:
  - Localiza o Service do NGINX IC (busca por labels/nomes comuns)
  - Extrai: tipo de Service, IP externo, annotations de NEG e LB
  - Verifica existência de BackendConfig

Saída: output/nginx_lb_check.csv e resumo no terminal.

Uso:
  python3 check_nginx_lb.py [--cluster <nome>] [--dry-run]
"""

import argparse
import csv
import json
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

XLSX_DEFAULT = Path(__file__).parent / "ingresses.xlsx"
OUTPUT_FILE  = Path(__file__).parent / "output" / "nginx_lb_check.csv"
NS           = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

# Namespaces onde o NGINX IC costuma estar
NGINX_NAMESPACES = ["ingress-nginx", "kube-system", "nginx-ingress"]

# Annotations relevantes para diagnóstico de LB
LB_ANNOTATIONS = [
    "cloud.google.com/neg",
    "cloud.google.com/neg-status",
    "cloud.google.com/load-balancer-type",
    "networking.gke.io/load-balancer-class",
    "controller.kubernetes.io/load-balancer-class",
    "cloud.google.com/backend-config",
]


# ---------------------------------------------------------------------------
# Leitura da planilha (mesmo padrão do collect.py)
# ---------------------------------------------------------------------------

def load_xlsx(path: str) -> list[dict]:
    with zipfile.ZipFile(path) as z:
        with z.open("xl/sharedStrings.xml") as f:
            ss_tree = ET.parse(f)
        strings = []
        for si in ss_tree.findall(".//main:si", NS):
            text = "".join(t.text or "" for t in si.findall(".//main:t", NS))
            strings.append(text)
        with z.open("xl/worksheets/sheet1.xml") as f:
            ws_tree = ET.parse(f)

    rows = ws_tree.findall(".//main:row", NS)

    def cell_val(cell):
        t = cell.get("t", "")
        v = cell.find("main:v", NS)
        if v is None:
            return ""
        if t == "s":
            return strings[int(v.text)]
        return v.text or ""

    header_row = rows[0]
    headers = [cell_val(c) for c in header_row.findall("main:c", NS)]
    data = []
    for row in rows[1:]:
        cells = row.findall("main:c", NS)
        row_data = {}
        for cell in cells:
            ref = cell.get("r", "")
            col_idx = 0
            for ch in ref:
                if ch.isalpha():
                    col_idx = col_idx * 26 + (ord(ch.upper()) - ord("A") + 1)
            col_idx -= 1
            if col_idx < len(headers) and headers[col_idx]:
                row_data[headers[col_idx]] = cell_val(cell)
        if row_data.get("project_id"):
            data.append(row_data)
    return data


# ---------------------------------------------------------------------------
# Contextos kubectl (mesmo padrão do collect.py)
# ---------------------------------------------------------------------------

def get_existing_contexts() -> set[str]:
    result = subprocess.run(
        ["kubectl", "config", "get-contexts", "--no-headers", "-o", "name"],
        capture_output=True, text=True,
    )
    return set(result.stdout.strip().splitlines())


def gke_context_name(project_id: str, location: str, cluster_name: str) -> str:
    return f"gke_{project_id}_{location}_{cluster_name}"


def ensure_context(project_id: str, location: str, cluster_name: str,
                   existing: set[str], dry_run: bool) -> str | None:
    ctx = gke_context_name(project_id, location, cluster_name)
    if ctx in existing or cluster_name in existing:
        return ctx if ctx in existing else cluster_name

    cmd = ["gcloud", "container", "clusters", "get-credentials", cluster_name,
           "--project", project_id, "--region", location]

    if dry_run:
        print(f"  [DRY-RUN] {' '.join(cmd)}")
        return None

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [ERRO] gcloud: {result.stderr.strip()}")
        return None
    return ctx


# ---------------------------------------------------------------------------
# Checagem do NGINX IC Service
# ---------------------------------------------------------------------------

def get_services(context: str, namespace: str) -> list[dict]:
    result = subprocess.run(
        ["kubectl", "get", "svc", "-n", namespace, "-o", "json", "--context", context],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return json.loads(result.stdout).get("items", [])


def is_nginx_service(svc: dict) -> bool:
    name    = svc["metadata"]["name"].lower()
    labels  = svc["metadata"].get("labels", {})
    label_v = " ".join(str(v).lower() for v in labels.values())
    label_k = " ".join(str(k).lower() for k in labels.keys())
    return (
        "nginx" in name or
        "ingress" in name and "nginx" in label_v or
        "nginx" in label_k or
        "nginx" in label_v
    )


def get_backend_configs(context: str, namespace: str) -> list[str]:
    result = subprocess.run(
        ["kubectl", "get", "backendconfig", "-n", namespace,
         "--no-headers", "-o", "name", "--context", context],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return [l.strip() for l in result.stdout.splitlines() if l.strip()]


def classify_lb(svc: dict) -> str:
    """Tenta classificar o tipo de LB com base no Service."""
    svc_type    = svc["spec"].get("type", "")
    annotations = svc["metadata"].get("annotations", {})

    if svc_type != "LoadBalancer":
        return svc_type

    lb_class   = (annotations.get("networking.gke.io/load-balancer-class", "") or
                  annotations.get("controller.kubernetes.io/load-balancer-class", ""))
    lb_type    = annotations.get("cloud.google.com/load-balancer-type", "")
    has_neg    = "cloud.google.com/neg" in annotations
    has_bcfg   = "cloud.google.com/backend-config" in annotations

    if has_bcfg or has_neg:
        return "LoadBalancer L7 (HTTP(S) LB com NEG)"
    if "internal" in lb_type.lower():
        return "LoadBalancer Internal (L4)"
    if lb_class:
        return f"LoadBalancer ({lb_class})"
    return "LoadBalancer TCP passthrough L4 (NLB clássico)"


def check_cluster(context: str, cluster_name: str) -> list[dict]:
    results = []

    for ns in NGINX_NAMESPACES:
        services = get_services(context, ns)
        nginx_svcs = [s for s in services if is_nginx_service(s)]

        for svc in nginx_svcs:
            annotations = svc["metadata"].get("annotations", {})
            ext_ips     = [i.get("ip", "") for i in svc["status"].get("loadBalancer", {}).get("ingress", [])]
            backend_cfgs = get_backend_configs(context, ns)

            lb_annotations = {k: v for k, v in annotations.items() if k in LB_ANNOTATIONS}

            results.append({
                "cluster":         cluster_name,
                "namespace":       ns,
                "service":         svc["metadata"]["name"],
                "type":            svc["spec"].get("type", ""),
                "classificacao":   classify_lb(svc),
                "external_ip":     ", ".join(ext_ips) if ext_ips else "nenhum",
                "backend_configs": ", ".join(backend_cfgs) if backend_cfgs else "nenhum",
                "annotations_lb":  json.dumps(lb_annotations, ensure_ascii=False) if lb_annotations else "{}",
            })

    if not results:
        results.append({
            "cluster":         cluster_name,
            "namespace":       "N/A",
            "service":         "NÃO ENCONTRADO",
            "type":            "",
            "classificacao":   "nginx IC não localizado nos namespaces verificados",
            "external_ip":     "",
            "backend_configs": "",
            "annotations_lb":  "{}",
        })

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Verifica modelo de LB do NGINX IC nos clusters")
    parser.add_argument("--xlsx",    default=XLSX_DEFAULT)
    parser.add_argument("--cluster", default=None, help="Processar somente este cluster")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = load_xlsx(args.xlsx)
    by_cluster: dict[tuple, list] = defaultdict(list)
    for row in rows:
        key = (row["project_id"], row["cluster_name"], row["cluster_location"])
        by_cluster[key].append(row)

    if args.cluster:
        by_cluster = {k: v for k, v in by_cluster.items() if k[1] == args.cluster}
        if not by_cluster:
            print(f"[ERRO] Cluster '{args.cluster}' não encontrado.")
            sys.exit(1)

    existing_contexts = get_existing_contexts()
    all_results = []

    for (project_id, cluster_name, location), _ in sorted(by_cluster.items()):
        print(f"\n{'='*55}")
        print(f"Cluster: {cluster_name}  ({project_id})")

        context = ensure_context(project_id, location, cluster_name,
                                 existing_contexts, args.dry_run)
        if context is None:
            print("  [SKIP] Sem contexto disponível.")
            continue

        if args.dry_run:
            print("  [DRY-RUN] Pulando checagem.")
            continue

        results = check_cluster(context, cluster_name)
        for r in results:
            print(f"  {r['namespace']}/{r['service']}")
            print(f"    Tipo:          {r['type']}")
            print(f"    Classificação: {r['classificacao']}")
            print(f"    IP externo:    {r['external_ip']}")
            print(f"    BackendConfig: {r['backend_configs']}")
            if r["annotations_lb"] != "{}":
                print(f"    Annotations:   {r['annotations_lb']}")
        all_results.extend(results)

    if all_results and not args.dry_run:
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = ["cluster", "namespace", "service", "type",
                      "classificacao", "external_ip", "backend_configs", "annotations_lb"]
        with open(OUTPUT_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_results)
        print(f"\nSalvo: {OUTPUT_FILE}")

    # Resumo
    if all_results:
        print(f"\n{'='*55}")
        print("RESUMO")
        by_class: dict[str, list[str]] = defaultdict(list)
        for r in all_results:
            by_class[r["classificacao"]].append(r["cluster"])
        for classificacao, clusters in sorted(by_class.items()):
            print(f"  [{len(clusters)} clusters] {classificacao}")


if __name__ == "__main__":
    main()
