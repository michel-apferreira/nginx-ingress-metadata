#!/usr/bin/env python3
"""
analyze.py — Analisa os metadados coletados e agrega por annotation/configuração.

Lê todos os JSONs em data/raw/ e produz:
  - output/annotations_summary.csv  — frequência e valores de cada annotation
  - output/ingresses_full.json      — todos os ingresses consolidados
  - output/features_report.json     — visão por feature para o depara

Uso:
  python3 analyze.py [--raw-dir <path>] [--top <n>]
"""

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

RAW_DIR = Path(__file__).parent / "data" / "raw"
OUTPUT_DIR = Path(__file__).parent / "output"

# Prefixos de annotation por controller
NGINX_ANNOTATION_PREFIXES = [
    "nginx.ingress.kubernetes.io/",
    "nginx.org/",
]
KONG_ANNOTATION_PREFIXES = [
    "konghq.com/",
    "ingress.kubernetes.io/",
]


def is_nginx_annotation(key: str) -> bool:
    return any(key.startswith(p) for p in NGINX_ANNOTATION_PREFIXES)


def is_kong_annotation(key: str) -> bool:
    return any(key.startswith(p) for p in KONG_ANNOTATION_PREFIXES)


def annotation_controller(key: str) -> str:
    if is_nginx_annotation(key):
        return "nginx"
    if is_kong_annotation(key):
        return "kong"
    return "other"


def get_ingress_controller(ingress: dict) -> str:
    """Determina o controller pelo ingressClassName da spec."""
    class_name = ingress.get("spec", {}).get("ingressClassName", "")
    if class_name == "nginx":
        return "nginx"
    if class_name == "kong":
        return "kong"
    return class_name or "unknown"


def load_all_ingresses(raw_dir: Path) -> list[dict]:
    ingresses = []
    for path in sorted(raw_dir.glob("*.json")):
        data = json.loads(path.read_text())
        # Cada arquivo é uma lista de objetos Ingress
        if isinstance(data, list):
            ingresses.extend(data)
        elif isinstance(data, dict):
            # Formato kubectl get ... -o json (Items wrapper)
            ingresses.extend(data.get("items", [data]))
    return ingresses


def extract_annotations(ingress: dict) -> dict[str, str]:
    return ingress.get("metadata", {}).get("annotations", {}) or {}


def extract_spec_features(ingress: dict) -> dict:
    spec = ingress.get("spec", {})
    features = {}

    # ingressClassName
    features["spec.ingressClassName"] = spec.get("ingressClassName", "")

    # TLS
    tls = spec.get("tls", [])
    features["spec.tls.enabled"] = "true" if tls else "false"
    features["spec.tls.secretCount"] = str(len(tls))

    # Hosts e paths
    rules = spec.get("rules", [])
    features["spec.rules.hostCount"] = str(len(rules))

    path_types = Counter()
    backend_services = set()
    for rule in rules:
        http = rule.get("http", {})
        for path in http.get("paths", []):
            path_types[path.get("pathType", "unset")] += 1
            svc = path.get("backend", {}).get("service", {})
            if svc.get("name"):
                backend_services.add(svc["name"])

    features["spec.pathTypes"] = ";".join(
        f"{k}={v}" for k, v in sorted(path_types.items())
    ) if path_types else ""
    features["spec.backendServiceCount"] = str(len(backend_services))

    return features


def analyze(ingresses: list[dict]) -> dict:
    # annotation key -> list of (ingress_id, value, controller)
    annotation_usage: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    all_annotation_keys: Counter = Counter()
    controller_counts: Counter = Counter()

    def ingress_id(ing: dict) -> str:
        meta = ing.get("metadata", {})
        ns = meta.get("namespace", "?")
        name = meta.get("name", "?")
        return f"{ns}/{name}"

    spec_feature_counter: dict[str, Counter] = defaultdict(Counter)

    for ing in ingresses:
        annotations = extract_annotations(ing)
        iid = ingress_id(ing)
        controller = get_ingress_controller(ing)
        controller_counts[controller] += 1

        for key, value in annotations.items():
            annotation_usage[key].append((iid, value, controller))
            all_annotation_keys[key] += 1

        spec_features = extract_spec_features(ing)
        for feat, val in spec_features.items():
            if val:
                spec_feature_counter[feat][val] += 1

    return {
        "annotation_usage": annotation_usage,
        "all_annotation_keys": all_annotation_keys,
        "spec_feature_counter": spec_feature_counter,
        "controller_counts": controller_counts,
        "total_ingresses": len(ingresses),
    }


def write_annotations_csv(result: dict, top: int | None = None):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    usage = result["annotation_usage"]
    total = result["total_ingresses"]

    # --- CSV completo (todas as annotations) ---
    out_full = OUTPUT_DIR / "annotations_summary_full.csv"
    keys_all = sorted(usage.keys(), key=lambda k: -len(usage[k]))
    if top:
        keys_all = keys_all[:top]

    with open(out_full, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "annotation_key", "controller", "uso_total",
            "nginx_ingresses", "kong_ingresses", "valores_distintos",
            "exemplos_de_valores", "exemplos_de_ingresses",
        ])
        for key in keys_all:
            entries = usage[key]
            values = [v for _, v, _ in entries]
            controllers = [c for _, _, c in entries]
            distinct_vals = sorted(set(values))
            # Trunca valores longos (ex: kubectl last-applied-configuration)
            sample_vals = "; ".join(v[:80] for v in distinct_vals[:5])
            sample_ingresses = "; ".join(iid for iid, _, _ in entries[:3])
            writer.writerow([
                key,
                annotation_controller(key),
                len(entries),
                sum(1 for c in controllers if c == "nginx"),
                sum(1 for c in controllers if c == "kong"),
                len(distinct_vals),
                sample_vals,
                sample_ingresses,
            ])
    print(f"Salvo: {out_full}  ({len(keys_all)} annotations)")

    # --- CSV de migração (só nginx e kong, sem ruído) ---
    out_migration = OUTPUT_DIR / "annotations_summary.csv"
    keys_migration = [
        k for k in sorted(usage.keys(), key=lambda k: -len(usage[k]))
        if is_nginx_annotation(k) or is_kong_annotation(k)
    ]

    with open(out_migration, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "controller", "annotation", "qtd_ingresses", "pct_do_total",
            "valores_distintos", "valores_encontrados", "exemplos_de_ingresses",
        ])
        for key in keys_migration:
            entries = usage[key]
            values = [v for _, v, _ in entries]
            distinct_vals = sorted(set(values))
            pct = f"{len(entries)/total*100:.1f}%"
            # Valores curtos e legíveis
            sample_vals = " | ".join(v[:60] for v in distinct_vals[:8])
            sample_ingresses = " | ".join(iid for iid, _, _ in entries[:3])
            writer.writerow([
                annotation_controller(key),
                key,
                len(entries),
                pct,
                len(distinct_vals),
                sample_vals,
                sample_ingresses,
            ])
    print(f"Salvo: {out_migration}  ({len(keys_migration)} annotations — só nginx/kong)")
    return out_migration


def write_features_report(result: dict):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / "features_report.json"

    usage = result["annotation_usage"]

    nginx_features = {}
    kong_features = {}
    other_annotations = {}

    for key, entries in sorted(usage.items(), key=lambda x: -len(x[1])):
        values = [v for _, v, _ in entries]
        controllers = [c for _, _, c in entries]
        distinct_vals = sorted(set(values))
        record = {
            "usage_count": len(entries),
            "nginx_ingresses": sum(1 for c in controllers if c == "nginx"),
            "kong_ingresses": sum(1 for c in controllers if c == "kong"),
            "distinct_values": distinct_vals,
            "sample_ingresses": [iid for iid, _, _ in entries[:5]],
        }
        if is_nginx_annotation(key):
            nginx_features[key] = record
        elif is_kong_annotation(key):
            kong_features[key] = record
        else:
            other_annotations[key] = record

    report = {
        "total_ingresses": result["total_ingresses"],
        "controller_counts": dict(result["controller_counts"]),
        "total_annotation_keys": len(usage),
        "nginx_annotation_keys": len(nginx_features),
        "kong_annotation_keys": len(kong_features),
        "other_annotation_keys": len(other_annotations),
        "nginx_features": nginx_features,
        "kong_features": kong_features,
        "other_annotations": other_annotations,
        "spec_features": {
            k: dict(v) for k, v in result["spec_feature_counter"].items()
        },
    }

    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Salvo: {out}")
    return out


def print_summary(result: dict, top: int = 20):
    usage = result["annotation_usage"]
    total = result["total_ingresses"]

    print(f"\nTotal de ingresses analisados: {total}")
    print(f"Total de annotation keys distintas: {len(usage)}")

    nginx_keys = [k for k in usage if is_nginx_annotation(k)]
    kong_keys = [k for k in usage if is_kong_annotation(k)]
    print(f"  NGINX Ingress annotations: {len(nginx_keys)}")
    print(f"  Kong annotations:          {len(kong_keys)}")
    print(f"  Outras annotations:        {len(usage) - len(nginx_keys) - len(kong_keys)}")

    cc = result["controller_counts"]
    print(f"\nIngresses por controller:")
    for ctrl, count in sorted(cc.items(), key=lambda x: -x[1]):
        print(f"  {ctrl:10s}: {count} ({count/total*100:.1f}%)")

    print(f"\nTop {top} annotations por frequência de uso:")
    print(f"  {'Count':>6}  {'%':>5}  {'Ctrl':>5}  Annotation")
    print(f"  {'-'*6}  {'-'*5}  {'-'*5}  {'-'*60}")

    for key, entries in sorted(usage.items(), key=lambda x: -len(x[1]))[:top]:
        pct = len(entries) / total * 100
        ctrl = annotation_controller(key)
        marker = " [nginx]" if is_nginx_annotation(key) else (" [kong]" if is_kong_annotation(key) else "")
        print(f"  {len(entries):>6}  {pct:>4.1f}%  {ctrl:>5}  {key}{marker}")

    print("")

    print("\nSpec features:")
    for feat, counter in sorted(result["spec_feature_counter"].items()):
        top_vals = counter.most_common(5)
        vals_str = ", ".join(f"{v}({c})" for v, c in top_vals)
        print(f"  {feat}: {vals_str}")


def main():
    parser = argparse.ArgumentParser(description="Analisa metadados de Ingresses coletados")
    parser.add_argument("--raw-dir", default=str(RAW_DIR))
    parser.add_argument("--top", type=int, default=None,
                        help="Limitar CSV às top N annotations por frequência")
    args = parser.parse_args()

    raw_dir = Path(args.raw_dir)
    files = list(raw_dir.glob("*.json"))
    if not files:
        print(f"Nenhum arquivo JSON encontrado em {raw_dir}")
        print("Execute collect.py primeiro.")
        return

    print(f"Carregando dados de {len(files)} arquivo(s) em {raw_dir} ...")
    ingresses = load_all_ingresses(raw_dir)
    print(f"Total de ingresses carregados: {len(ingresses)}")

    result = analyze(ingresses)
    print_summary(result)

    write_annotations_csv(result, top=args.top)
    write_features_report(result)


if __name__ == "__main__":
    main()
