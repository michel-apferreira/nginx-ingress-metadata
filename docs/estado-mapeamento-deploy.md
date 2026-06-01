# Estado do mapeamento de tipos de deploy — nginx ingresses

**Última atualização:** 2026-05-14
**Contexto:** item 1.3 do plano de migração — levantar todos os ingresses por tipo de deploy (Spinnaker, manual, Helm, outros)

---

## Por que este mapeamento é crítico

Ao migrar um ingress nginx → APISIX, o risco é: se a pipeline que gerencia aquele ingress continuar ativa, ela vai re-aplicar o manifesto nginx sobre o ingress APISIX e desfazer a migração. Por isso precisamos saber exatamente como cada ingress é deployado antes de migrar.

A estratégia atual usa sufixo `-apisix` nos novos manifestos para coexistência (ex: `rdsm-web-api-apisix.yaml`), mas o ingress nginx original continua sendo gerenciado pela pipeline antiga.

---

## O que foi feito

1. **Script `deploy_types.py`** criado em `nginx-ingress-metadata/`
   - Lê JSONs coletados em `data/raw/` (ou `sre-foundation-scripts/nginx-apisix-migration/data/raw/`)
   - Classifica cada ingress nginx por tipo de deploy via annotations/labels
   - Enriquece com dados reais da API do Spinnaker (pipeline configs)
   - Gera `output/deploy_types.csv`

2. **API do Spinnaker acessada** via cookie de sessão
   - Gate URL: `https://spinnaker-api.rdops.systems`
   - Autenticação: cookie `SESSION` obtido via browser após login Google SSO
   - Consultados `pipelineConfigs` de 109 apps para identificar tipo de pipeline (Helm vs Manifesto)

3. **CSV gerado:** `output/deploy_types.csv`
   - Colunas: `project_id`, `cluster`, `namespace`, `ingress`, `deploy_type`, `spinnaker_app`, `spinnaker_cluster`, `spinnaker_pipeline_type`, `spinnaker_pipeline`, `helm_release`, `helm_chart`, `evidence`, `hosts`

---

## Resultado final

| Tipo | Qtd | % |
|---|---|---|
| Spinnaker + Helm (HELM3) | 147 | 35% |
| Spinnaker + Manifesto direto | 145 | 34% |
| Manual (Lens/Freelens) | 69 | 16% |
| Outros (kubectl apply, scripts) | 54 | 13% |
| Helm direto (sem Spinnaker) | 5 | 1% |
| Indeterminado | 3 | <1% |
| **Total nginx** | **424** | |

---

## Critérios de classificação

### Spinnaker
- Annotation `moniker.spinnaker.io/application` ou `artifact.spinnaker.io/name` presente

### Spinnaker + Helm vs Manifesto
- Consultado `GET /applications/{app}/pipelineConfigs` na API do Spinnaker
- Pipeline com `bakeManifest` (templateRenderer: HELM3) = **Helm**
- Pipeline com apenas `deployManifest` = **Manifesto direto**
- Pipelines de suporte (external secrets, cloudsql, backup, cronjob, etc.) ignorados

### Manual/Lens
- Label `k8slens-edit-resource-version` ou annotation `freelens.app/resource-version`

### Helm direto
- Label `app.kubernetes.io/managed-by: Helm` ou annotation `meta.helm.sh/release-name`

---

## Como retomar amanhã

### 1. Reautenticar no Spinnaker (cookie expira)
```
1. Abrir browser → https://spinnaker.rdops.systems
2. Fazer login com conta Google
3. DevTools (F12) → Application → Cookies → spinnaker-api.rdops.systems
4. Copiar valor do cookie SESSION
```

### 2. Rodar o script
```bash
cd ~/workspace/nginx-ingress-metadata
python3 deploy_types.py
# CSV em output/deploy_types.csv
```

### 3. Para re-enriquecer com Spinnaker (se necessário)
O enriquecimento via API está dentro do script inline que foi rodado manualmente.
Considerar mover a lógica Spinnaker para dentro do `deploy_types.py` com flag `--spinnaker-session <SESSION>`.

---

## Pendências

- [ ] **Validação dos 13 ingresses APISIX no staging-234557** — Fase 3 do plano (PLATENG-4507)
  - 12 de 13 aplicados no cluster (falta `kafka/dashboard-kafka-staging-apisix`)
  - Todos os 12 presentes respondendo como esperado (301/403/401)
  - `rdsm-email-validator`: testar no path correto `/web_api/v1/email_validator`
  - Aplicar `dashboard-kafka-staging-apisix` e re-testar

- [ ] **Mover lógica de enriquecimento Spinnaker para dentro do `deploy_types.py`**
  - Adicionar flag `--spinnaker-session <token>`

- [ ] **Investigar os 54 "outros"** — ingresses sem classificação conhecida
  - Candidatos: kubectl apply direto, scripts de automação, terraform

- [ ] **Apresentar números para liderança técnica** — reunião para decidir estratégia de deploy (centralização Argo CD vs descentralização)

---

## Arquivos relevantes

| Arquivo | Descrição |
|---|---|
| `deploy_types.py` | Script de classificação |
| `output/deploy_types.csv` | CSV com classificação de todos os 424 ingresses |
| `output/spinnaker_pipeline_types.json` | Tipos de stage por app Spinnaker (intermediário) |
| `data/raw/` | JSONs coletados via kubectl (ou `sre-foundation-scripts/`) |
| `CLAUDE.md` | Contexto completo do projeto + decisões arquiteturais |

---

## Contexto das reuniões (2026-05-12 e 2026-05-13)

- **Decisão tomada:** 1 APISIX por cluster em produção
- **CI/CD futuro:** Argo CD substituirá Spinnaker para orquestração de ingress
- **Estratégia em 3 etapas:** padronização → automação (Argo CD) → corte NGINX
- **Risco:** ingresses com `moniker.spinnaker.io/application` podem ser revertidos pelo Spinnaker após migração
- Reunião semanal de liderança de plataforma: quartas-feiras 15h
