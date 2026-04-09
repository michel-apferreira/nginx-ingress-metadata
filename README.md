# nginx-ingress-metadata

Scripts para coleta e análise de metadados de Ingresses Kubernetes, com geração de mapeamento (depara) de features NGINX/Kong → APISIX.

## Contexto

A RD Station está migrando o NGINX Ingress Controller para o APISIX. Este repositório automatiza:

1. **Coleta** — lê a planilha de ingresses a migrar e coleta o JSON completo de cada ingress via `kubectl`
2. **Análise** — agrega as annotations por frequência, separando por controller (nginx/kong)
3. **Depara** — mapeia cada annotation NGINX/Kong para o plugin APISIX equivalente

## Pré-requisitos

- Python 3.12+
- `kubectl` configurado
- `gcloud` autenticado (para clusters sem contexto configurado)

## Estrutura

```
nginx-ingress-metadata/
├── ingresses.xlsx        # Planilha com os ingresses a migrar
├── collect.py            # Coleta metadados dos clusters
├── analyze.py            # Agrega annotations por frequência
├── depara.py             # Gera mapeamento NGINX/Kong → APISIX
├── data/
│   └── raw/              # JSONs coletados por cluster (gerado pelo collect.py)
└── output/               # CSVs e JSON de resultado (gerado pelos scripts)
```

## Uso

### 1. Coleta

Lê a planilha `ingresses.xlsx`, configura os contextos kubectl ausentes via `gcloud` e coleta o JSON completo de cada ingress:

```bash
python3 collect.py
```

Opções disponíveis:

```bash
# Testar sem executar nada (mostra o que faria)
python3 collect.py --dry-run

# Processar somente um cluster específico
python3 collect.py --cluster services-gke-usc1-crm-prd-01

# Usar uma planilha diferente
python3 collect.py --xlsx /caminho/para/planilha.xlsx

# Re-coletar mesmo ingresses já salvos
python3 collect.py --force
```

Os dados são salvos em `data/raw/<project_id>__<cluster_name>.json`. A coleta é incremental — ingresses já coletados são pulados (use `--force` para re-coletar).

### 2. Análise

Lê os JSONs coletados e agrega as annotations por frequência:

```bash
python3 analyze.py
```

Gera:
- `output/annotations_summary.csv` — annotations nginx/kong encontradas nos clusters, ordenadas por frequência
- `output/features_report.json` — dados intermediários usados pelo depara.py

### 3. Depara

Gera o mapeamento de cada annotation para o plugin APISIX equivalente:

```bash
python3 depara.py
```

Gera:
- `output/depara_nginx_apisix.csv` — mapeamento completo para revisão no Google Sheets
- `output/depara_nginx_apisix.json` — mesmo conteúdo em JSON

## Entendendo os outputs

### `annotations_summary.csv`

Inventário das annotations encontradas nos clusters. Cada linha é um tipo de annotation:

| Coluna | Descrição |
|---|---|
| `controller` | `nginx` ou `kong` |
| `annotation` | Nome da annotation |
| `qtd_ingresses` | Quantos ingresses usam essa annotation |
| `pct_do_total` | % do total de ingresses coletados |
| `valores_distintos` | Quantos valores diferentes existem |
| `valores_encontrados` | Exemplos de valores reais nos clusters |
| `exemplos_de_ingresses` | Quais ingresses usam |

### `depara_nginx_apisix.csv`

O arquivo principal da migração. Cada linha é uma annotation mapeada para o APISIX:

| Coluna | Descrição |
|---|---|
| `source_controller` | `nginx` ou `kong` |
| `annotation_original` | Annotation como está hoje no ingress |
| `uso_nos_clusters` | Quantos ingresses usam |
| `funcionalidade` | O que essa annotation faz |
| `apisix_plugin` | Qual plugin APISIX substitui |
| `apisix_config` | Como configurar no APISIX |
| `suporte` | `Completo` / `Parcial` / `Manual` / `Sem suporte` / `Não aplicável` |
| `observacoes` | Notas importantes para a migração |
| `valores_encontrados` | Valores reais encontrados nos clusters |

#### Níveis de suporte

| Nível | Significado |
|---|---|
| `Completo` | Migração direta, APISIX tem suporte nativo equivalente |
| `Parcial - requer ajuste` | Funciona mas precisa de adaptação |
| `Manual - análise caso a caso` | Cada ingress precisa ser analisado individualmente |
| `Sem suporte nativo` | APISIX não tem equivalente direto |
| `Não aplicável` | Annotation de infraestrutura (ex: GKE LB) — não precisa migrar |

## Resultado da coleta (2026-04-08)

| | |
|---|---|
| Ingresses na planilha | 709 |
| Ingresses coletados | 609 |
| Não encontrados (namespaces efêmeros) | 100 |
| Clusters | 36 |
| Controller nginx | 395 (64.9%) |
| Controller kong | 163 (26.8%) |
| Annotations únicas mapeadas | 86 (58 nginx + 28 kong) |
