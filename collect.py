#!/usr/bin/env python3
"""
collect.py — Coleta metadados completos dos Ingresses listados na planilha.

Fluxo:
  1. Lê a planilha e monta lista de (project_id, cluster_name, cluster_location, namespace, ingress_name)
  2. Para cada cluster único:
     a. Verifica se o contexto kubectl já existe
     b. Se não existir, executa gcloud container clusters get-credentials
  3. Para cada ingress: kubectl get ingress <name> -n <namespace> -o json
  4. Salva resultado em data/raw/<project_id>__<cluster_name>.json

Uso:
  python3 collect.py [--xlsx <path>] [--dry-run] [--cluster <cluster_name>]
"""

import argparse
import json
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

XLSX_DEFAULT = Path(__file__).parent / "ingresses.xlsx"
RAW_DIR = Path(__file__).parent / "data" / "raw"
NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


# ---------------------------------------------------------------------------
# Leitura da planilha
# ---------------------------------------------------------------------------

def load_xlsx(path: str) -> list[dict]:
    """Retorna lista de dicts com as colunas da planilha."""
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
# Contextos kubectl
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
    """
    Garante que o contexto GKE existe no kubeconfig.
    Retorna o nome do contexto se disponível, None se falhar.
    """
    ctx = gke_context_name(project_id, location, cluster_name)

    # Alguns clusters têm alias no kubeconfig (ex: cluster-staging)
    # Verifica também pelo cluster_name direto
    if ctx in existing or cluster_name in existing:
        actual = ctx if ctx in existing else cluster_name
        print(f"  [OK] Contexto já existe: {actual}")
        return actual

    cmd = [
        "gcloud", "container", "clusters", "get-credentials", cluster_name,
        "--project", project_id,
        "--region", location,
    ]
    print(f"  [SETUP] Configurando contexto: {' '.join(cmd)}")

    if dry_run:
        print("  [DRY-RUN] Pulando execução.")
        return None

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [ERRO] gcloud falhou:\n{result.stderr.strip()}")
        return None

    print(f"  [OK] Contexto configurado: {ctx}")
    return ctx


# ---------------------------------------------------------------------------
# Coleta via kubectl
# ---------------------------------------------------------------------------

def fetch_ingress(context: str, namespace: str, name: str) -> dict | None:
    result = subprocess.run(
        [
            "kubectl", "get", "ingress", name,
            "-n", namespace,
            "-o", "json",
            "--context", context,
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "not found" in stderr.lower():
            return None
        print(f"    [WARN] kubectl erro ({name}/{namespace}): {stderr}")
        return None
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Persistência
# ---------------------------------------------------------------------------

def cluster_filename(project_id: str, cluster_name: str) -> Path:
    safe = f"{project_id}__{cluster_name}".replace("/", "_")
    return RAW_DIR / f"{safe}.json"


def load_existing(path: Path) -> list[dict]:
    if path.exists():
        return json.loads(path.read_text())
    return []


def save_cluster_data(path: Path, ingresses: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ingresses, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Coleta metadados de Ingresses GKE")
    parser.add_argument("--xlsx", default=XLSX_DEFAULT, help="Caminho para a planilha")
    parser.add_argument("--dry-run", action="store_true",
                        help="Não executa gcloud/kubectl, só mostra o que faria")
    parser.add_argument("--cluster", default=None,
                        help="Processar somente este cluster (cluster_name)")
    parser.add_argument("--force", action="store_true",
                        help="Re-coleta ingresses mesmo que já existam no arquivo")
    args = parser.parse_args()

    print(f"Lendo planilha: {args.xlsx}")
    rows = load_xlsx(args.xlsx)
    print(f"Total de ingresses na planilha: {len(rows)}")

    # Agrupa por cluster
    by_cluster: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        key = (row["project_id"], row["cluster_name"], row["cluster_location"])
        by_cluster[key].append(row)

    print(f"Clusters únicos: {len(by_cluster)}")

    if args.cluster:
        by_cluster = {
            k: v for k, v in by_cluster.items() if k[1] == args.cluster
        }
        if not by_cluster:
            print(f"[ERRO] Cluster '{args.cluster}' não encontrado na planilha.")
            sys.exit(1)
        print(f"Filtrando para cluster: {args.cluster}")

    existing_contexts = get_existing_contexts()

    total_collected = 0
    total_not_found = 0
    total_errors = 0

    for (project_id, cluster_name, location), ingress_rows in sorted(by_cluster.items()):
        print(f"\n{'='*60}")
        print(f"Cluster: {cluster_name}  ({project_id} / {location})")
        print(f"Ingresses a coletar: {len(ingress_rows)}")

        context = ensure_context(project_id, location, cluster_name,
                                 existing_contexts, args.dry_run)
        if context is None:
            print("  [SKIP] Sem contexto disponível, pulando cluster.")
            continue

        out_path = cluster_filename(project_id, cluster_name)
        existing_data: list[dict] = [] if args.force else load_existing(out_path)
        existing_keys = {
            (i["metadata"]["namespace"], i["metadata"]["name"])
            for i in existing_data
        }

        cluster_ingresses = list(existing_data)

        for row in ingress_rows:
            ns = row["namespace"]
            name = row["ingress_name"]
            key = (ns, name)

            if key in existing_keys and not args.force:
                print(f"  [CACHE] {ns}/{name}")
                continue

            if args.dry_run:
                print(f"  [DRY-RUN] Coletaria: {ns}/{name}")
                continue

            print(f"  [GET] {ns}/{name} ... ", end="", flush=True)
            obj = fetch_ingress(context, ns, name)

            if obj is None:
                print("não encontrado")
                total_not_found += 1
                continue

            # Remove campos de status/managedFields para reduzir tamanho
            obj.get("metadata", {}).pop("managedFields", None)

            cluster_ingresses.append(obj)
            existing_keys.add(key)
            total_collected += 1
            print("OK")

        if not args.dry_run:
            save_cluster_data(out_path, cluster_ingresses)
            print(f"  Salvo: {out_path}  ({len(cluster_ingresses)} ingresses)")

    print(f"\n{'='*60}")
    print(f"Coleta concluída.")
    print(f"  Coletados:     {total_collected}")
    print(f"  Não encontrados: {total_not_found}")
    print(f"  Erros:         {total_errors}")
    print(f"Arquivos em: {RAW_DIR}")


if __name__ == "__main__":
    main()
