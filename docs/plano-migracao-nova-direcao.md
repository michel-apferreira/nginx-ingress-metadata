# Plano de migração NGINX → APISIX — nova direção (2026-05-22)

**Status:** em construção — atualizar conforme decisões forem tomadas

---

## Contexto da mudança

Ficou definido que o chart do APISIX gateway deve seguir o padrão do **rd-helm** (`https://github.com/ResultadosDigitais/rd-helm`), onde infra fica centralizada fora dos repos de aplicação.

O Spinnaker será descontinuado em favor do ArgoCD no futuro, mas **não faz parte do escopo atual**. A migração de ingresses continua no modelo atual.

---

## Decisões técnicas confirmadas

| Item | Decisão |
|---|---|
| Chart APISIX gateway | `rd-helm/charts/apisix-gateway/` |
| Onde vivem values | Pasta `values/` do **rd-helm** (local oficial para repos RD) |
| Coexistência dos charts | Charts atuais (`rails`, `rdsm`) adaptados para coexistir — NGINX continua funcionando enquanto APISIX é adicionado |
| ApisixPluginConfig | Deploy separado, referenciado no Ingress via annotation (`k8s.apisix.apache.org/plugin-config-name`) |
| Workflow do time de engenharia | Não deve ser impactado; mudanças de config continuam via `values` |
| Nome do Ingress duplicado | **`<app>-gw`** (ex: `account-api-gw`) — agnóstico de tecnologia, sem sufixo de stack |

---

## Alinhamento com Matheus Guizolfi — 2026-05-25

### Tarefa principal: unificar template de Ingress no rd-helm

Os charts `rails` e `rdsm` possuem hoje templates de ingress com conteúdo diferente (o `rails` inclusive tem algumas annotations chumbadas). A tarefa é:

- Criar um **único conteúdo de template** de ingress que funcione para os dois charts
- O template deve suportar os caminhos existentes (NGINX) **e** o novo (`ingressClassName: apisix`) via variável
- Os charts continuam separados — só o arquivo `templates/ingress.yaml` terá o mesmo conteúdo nos dois
- Isso facilita a futura unificação em um único chart, que usará exatamente esse template

### Fluxo por tipo de deploy (atualizado)

| Tipo | Abordagem |
|---|---|
| Spinnaker + Helm (`rails` / `rdsm`) | Atualizar o template de ingress unificado no chart; gerar nova versão; atualizar o step no Spinnaker apontando para a nova versão |
| Spinnaker + Manifesto direto | Continua como está — gerar manifesto via script e aplicar diretamente. Já bem adiantado |
| Manual | Continua como está — aplicar via `kubectl apply` |

### Investigar: Spinnaker API

Para os casos Spinnaker + Manifesto direto, vale investigar automação via API do Spinnaker.
- Matheus: "Tales pode saber gerar o token"
- Risco: steps podem não ter nomes padronizados entre pipelines

### Virada de tráfego no staging

Pode ser feita durante o dia:
1. Testar via `/etc/hosts` na máquina local (apontando host para o IP do APISIX)
2. Se funcionar → virar a chave no LB do staging

---

## ⚠️ Débito: manifestos gerados com sufixo `-apisix` no staging

Os ~40 ingresses já aplicados no staging-234557 (batches 1 e 2) foram gerados com sufixo `-apisix` no nome (ex: `account-api-apisix`). Quando o nome genérico for definido, será necessário:

1. Regerar os manifestos com o novo nome via script
2. Aplicar os novos recursos no cluster
3. Remover os recursos com sufixo `-apisix`

Isso pode ser feito em conjunto com a próxima rodada de geração — não é urgente, mas não deve ser esquecido.

---

## Onde estamos hoje (atualizado 2026-05-28)

| Item | Estado |
|---|---|
| Chart APISIX gateway | `rd-helm/charts/apisix-gateway/` — PR #163 aberto, lint corrigido, aguardando aprovação |
| values/apisix-gateway/local-values.yaml | Adicionado ao PR #163 |
| Template ingress unificado (rails + rdsm) | PR #165 aberto, todos os cenários de teste executados e validados |
| Ingresses migrados staging-234557 | 40/178 (batches 1 e 2 — sufixo `-apisix`, **a regerar com nome genérico**) |
| Ingresses migrados outros clusters | 0 |
| JIRA | PLATENG-4771 (PR #163) e PLATENG-4772 (PR #165) criados como filhos de PLATENG-4497 |
| deploy_types.py | Corrigido: agora distingue `spinnaker+helm-rails` de `spinnaker+helm-rdsm` |

---

## Passo 1 — Mover chart APISIX para o `rd-helm` ✅ Concluído

PR aberto: https://github.com/ResultadosDigitais/rd-helm/pull/163

Somente o **chart** (templates + valores padrão) foi para o rd-helm. Os values por cluster e os manifestos de ingress ficam no `helm-apisix` onde estão hoje.

```
rd-helm/
└── charts/
    └── apisix-gateway/
        ├── Chart.yaml
        ├── values.yaml
        ├── README.md
        ├── Chart.lock
        ├── charts/           ← subcharts (apisix, fluent-bit)
        └── templates/

helm-apisix/                  ← permanece como está
├── k8s/
│   ├── rd-sre-stg-01/infra/values.yaml
│   └── staging-234557/infra/values.yaml
│       └── config/manifests/ ← manifestos de ingress APISIX
└── ...
```

### Pendente (pós-merge do PR)
- [ ] Confirmar processo de contribuição com time rd-helm (CODEOWNERS, pré-commit hook)
- [ ] Definir se subcharts (etcd, keycloak) ficam embutidos ou viram charts separados no rd-helm

---

## Passo 2 — Unificar template de ingress (`rails` + `rdsm`) ✅ PR aberto

PR #165: https://github.com/ResultadosDigitais/rd-helm/pull/165

Template único com dois blocos condicionais — rails ativa por `$.Values.applications`, rdsm por `$.Values.ingress` como slice. `ingressClassName` variável com default `nginx` em ambos.

### Checklist
- [x] Mapear diferenças entre `templates/ingress.yaml` do `rails` e do `rdsm`
- [x] Identificar annotations chumbadas no template do `rails` que precisam virar variáveis
- [x] Criar template unificado com suporte a `ingressClassName: apisix`
- [x] Fix: nil pointer em `$app.service.port` quando `service` não definido (corrigido com `dig`)
- [x] Executar e validar todos os cenários de teste (6 cenários — todos OK)
- [x] Abrir PR no rd-helm
- [ ] Aguardar aprovação e merge
- [ ] Atualizar step no Spinnaker para nova versão (`rails 0.1.10` / `rdsm 0.1.8`)

---

## Passo 3 — Continuar migração dos ingresses (staging-234557)

178 ingresses nginx no cluster staging-234557 (excluindo namespaces de review). Batches concluídos:

| Batch | Ingresses | Status |
|---|---|---|
| Batch 1 | 12 (Padrão B — com ApisixPluginConfig) | Validado ✅ (nome `-apisix` — a regerar) |
| Batch 2 | 28 (Padrão A — troca direta) | Validado ✅ (nome `-apisix` — a regerar) |
| Restante | ~138 | Pendente |

Nome definitivo: **`<app>-gw`** (ex: `account-api-gw`). Os batches anteriores foram gerados com sufixo `-apisix` e precisam ser regerados com o novo padrão.

### Fluxo por tipo de deploy

| Tipo | Abordagem |
|---|---|
| Spinnaker + Manifesto direto | Ver passo a passo detalhado abaixo ✅ Validado em 2026-05-31 |
| Spinnaker + Helm (`rails` / `rdsm`) | Usar novo template unificado; atualizar step no Spinnaker com nova versão do chart |
| Manual | Aplicar via `kubectl apply` |

### Passo a passo — Spinnaker + Manifesto direto ✅ Validado (2026-05-31)

**Piloto validado:** `api-predictive-lead-scoring` no namespace `contact-mgmt` (cluster staging-234557)

**Princípio:** o Ingress nginx original é mantido intacto. Um segundo Ingress com nome `<app>-gw` é criado com `ingressClassName: apisix`. A troca de tráfego acontece no Load Balancer, nunca nos manifestos.

**1. Verificar annotations do Ingress nginx original**
```bash
kubectl get ingress <app> -n <namespace> -o yaml
```
Consultar o depara (`output/depara_nginx_apisix.csv`) para cada annotation presente:
- **Padrão A**: annotation tem equivalente direto no APISIX IC → trocar o nome
- **Padrão B**: annotation sem equivalente (ex: `proxy-body-size`) → criar `ApisixPluginConfig` companion

**2. Gerar o manifesto APISIX**

O manifesto vai para `~/workspace/helm-apisix/k8s/<cluster>/config/manifests/<namespace>/`. Usar o script `migrate.py` ou criar manualmente:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: <app>-gw          # NUNCA sobrescrever o ingress nginx original
  namespace: <namespace>
  annotations:
    # annotations convertidas conforme depara (Padrão A)
    # se Padrão B: k8s.apisix.apache.org/plugin-config-name: <app>-plugins
spec:
  ingressClassName: apisix
  rules:
    - host: <mesmo host do nginx>
      http:
        paths: <mesmos paths do nginx>
  tls: <mesma config TLS do nginx>
```

Se Padrão B, gerar também o `<app>-plugins.yaml` (`ApisixPluginConfig`).

**3. Aplicar via kubectl (não executar a pipeline inteira)**
```bash
kubectl apply -f <app>-gw.yaml
# se Padrão B:
kubectl apply -f <app>-plugins.yaml
```

> ⚠️ **Não disparar a pipeline do Spinnaker** para aplicar o manifesto APISIX. O stage de Deploy da app (imagem Docker) pode falhar com ImagePullBackOff se o SHA não tiver imagem publicada, causando downtime.

**4. Confirmar que a rota foi sincronizada no APISIX**
```bash
kubectl exec -n ingress-apisix deploy/apisix -- \
  curl -s http://127.0.0.1:9180/apisix/admin/routes \
  -H "X-API-KEY: $(kubectl get secret -n ingress-apisix apisix-gateway-admin-key -o jsonpath='{.data.key}' | base64 -d)" \
  | python3 -c "import sys,json; routes=json.load(sys.stdin)['list']; [print(r['value']['id']) for r in routes]" \
  | grep <app>-gw
```
Esperar a rota `<namespace>_<app>-gw_0-0` aparecer (IC sincroniza em ~5s).

**5. Testar o backend via APISIX (de dentro do cluster)**
```bash
kubectl run test-curl --image=curlimages/curl --restart=Never --rm -it -- \
  curl -v -H "Host: <host>" http://<apisix-service-ip>:9080/
```
Verificar HTTP 200 e headers de resposta corretos.

**6. Confirmar coexistência**
```bash
kubectl get ingress -n <namespace>
# deve mostrar:
# <app>      nginx   <host>   35.184.12.116   80,443
# <app>-gw   apisix  <host>                   80,443
```

**7. Adicionar stage à pipeline do Spinnaker (para futuras execuções)**

Adicionar um stage `Deploy Manifest` ao final da pipeline existente com o manifesto APISIX. Isso garante que redeployments futuros via Spinnaker também atualizam o Ingress APISIX. Usar a API do Spinnaker:
```bash
# GET /applications/<app>/pipelineConfigs → localizar a pipeline
# Adicionar stage com o manifesto APISIX (refId novo, requisiteStageRefIds=[<último stage>])
# POST /pipelines com o JSON atualizado
```

---

## Passo 4 — Expansão para outros clusters

Após staging-234557 concluído:

1. Replicar estrutura de manifests para cada cluster no `helm-apisix`
2. Adicionar hosts ao url_map do ALB do projeto GCP correspondente
3. Validar coexistência (mesmo script de validação)
4. Virar DNS cluster a cluster

Prioridade de clusters a definir com Márcio/Matheus.

---

## Distribuição de tipos de deploy (dados de abr/2026 — re-coletar antes de continuar)

Script: `python3 deploy_types.py` → `output/deploy_types.csv`

| Tipo | Qtde | % | Pode migrar sem PR #165? |
|---|---|---|---|
| `spinnaker+helm-rails` | 133 | 31% | Não — precisa PR #165 + atualizar Spinnaker |
| `spinnaker+manifesto` | 132 | 31% | Sim — gerar manifesto + kubectl apply |
| `manual` | 69 | 16% | Sim — kubectl apply |
| `outros` | 54 | 13% | Investigar |
| `spinnaker+helm-externo` | 14 | 3% | Investigar |
| `spinnaker+helm-rdsm` | 17 | 4% | Não — precisa PR #165 + atualizar Spinnaker |
| `helm-direto` | 5 | 1% | Investigar |

**Atenção:** dados raw são de 08/04/2026. Rodar `python3 collect.py --force` antes de tomar decisões de migração.

## Perguntas em aberto

- [ ] Subcharts embutidos ou separados no rd-helm?
- [ ] Ordem de prioridade dos próximos clusters após staging-234557
- [x] Nome genérico definitivo para os Ingresses APISIX duplicados: **`<app>-gw`** (ex: `account-api-gw`) — agnóstico de tecnologia, não precisa renomear se o ingress controller mudar
- [ ] Ferramenta de orquestração futura (Argo CD + Helm direto?) — decisão pós-staging
- [ ] Re-coletar dados dos 36 clusters (`collect.py --force`) para ter números atualizados
