# Contexto do projeto — nginx-ingress-metadata

## Instrução obrigatória

**Antes de qualquer ação relacionada à migração, leia o plano completo em `https://github.com/ResultadosDigitais/helm-apisix/issues/23` e siga-o à risca.**

Princípios que devem guiar toda decisão:
- Um Ingress só entra em PR quando **todas** as suas annotations estiverem resolvidas (Padrão A, B ou remoção explícita)
- O double_check deve ser rodado apenas sobre os ingresses do PR específico, não sobre todos os manifestos
- Coexistência nginx/APISIX é feita com recursos de nomes diferentes (sufixo `-apisix`), não substituindo o Ingress original
- A troca de tráfego acontece na camada de Load Balancer, nunca nos manifestos das aplicações

## Objetivo
Migração dos Ingresses **NGINX** distribuídos em 36 clusters GKE para APISIX na RD Station.
**Kong está fora do escopo de migração** — os ingresses rotulados como Kong usam na maioria/todos os casos o ingress da GCP (GKE Load Balancer nativo), não o Kong Ingress Controller. Não há o que migrar para APISIX nesses casos.
O repositório contém scripts para coleta, análise e geração de depara (mapeamento de annotations).
Primeiro cluster da validação: **rd-crm-stg-01** (Ingress mais complexo).

Participantes: Michel Ferreira, Marcio Cavalante, Matheus Guizolfi, Rodolfo Tavares, Everton Rocha.

## Acesso aos clusters

- **kubectl**: acesso a todos os 36 clusters — basta trocar o contexto (`kubectl config use-context <context>`)
- **gcloud**: acesso completo via CLI (`gcloud` disponível localmente)
- **Terraform**: acesso via repositório `ResultadosDigitais/tf-projects` no GitHub

## Ambiente — clusters configurados

| Cluster | Values | APISIX | Particularidades |
|---|---|---|---|
| rd-crm-stg-01 | `k8s/rd-sre-stg-01/infra/values.yaml` | 3.14.1 | NEG: `neg-demo-svc`, etcd: `apisix-gateway-etcd` |
| staging-234557 | `k8s/staging-234557/infra/values.yaml` | **3.16.0** | NEG: `neg-api-gateway-svc`, DNS discovery, URI routing com parâmetros |

Os caminhos de values acima são relativos ao repositório do chart do APISIX: **`~/workspace/rd-helm/values/apisix-gateway/`** (não o helm-apisix). O `helm-apisix` contém apenas manifestos de ingress e ApisixPluginConfig, não o chart em si.

- APISIX Ingress Controller 2.0.1 — IngressClass: `apisix` (coexiste com nginx: `defaultIngressClass: false`)
- etcd 3.6.0 (3 nós, anti-afinidade por zona) — StatefulSet próprio, Bitnami desabilitado
- Keycloak 26.5.4 — credenciais via Vault (`existingSecret`), PostgreSQL local só para dev
- Helm chart interno: `https://github.com/ResultadosDigitais/helm-apisix` (cópia local: `~/workspace/helm-apisix`)
- CODEOWNERS: time Pope — @ResultadosDigitais/pope

## Arquitetura de load balancing
ALB → **Standalone NEG** → NodePort/ClusterIP → APISIX (em staging: ClusterIP + annotation NEG)

Alguns clusters possuem dois caminhos de entrada no NGINX:
- **Externo** (NLB público): `nginx-ingress-nginx-controller` → ex: `35.184.12.116`
- **Interno** (ILB VPC-only): `nginx-ingress-nginx-controller-internal` → ex: `10.3.96.94` (staging-234557)

O APISIX tem ILB equivalente desde 2026-06-12 (PR ResultadosDigitais/tf-projects#11224 merged). IP: `10.3.96.100`, DNS: `k8s-apisix-internal.staging.rdops.systems`. Cert TLS: SELF_MANAGED regional `wildcard-staging-rdops-systems-regional-20260612` (expira Sep 10 2026, cert-manager renova automaticamente o secret K8s — mas o cert GCP regional é SELF_MANAGED e **deve ser recriado manualmente** a cada renovação do cert-manager).

**Próxima renovação do cert regional:** por volta de **2026-08-31** (cert-manager renova o secret devexp/wildcard-staging-rdops-systems). Após a renovação, recriar o cert GCP regional: exportar cert/key do secret `ingress-apisix/wildcard-staging-rdops-systems`, criar novo `gcloud compute ssl-certificates create wildcard-staging-rdops-systems-regional-YYYYMMDD --region=us-central1 --project=staging-234557 --certificate=... --private-key=...`, atualizar `api-gateway-staging-internal-https-proxy`, deletar o antigo.

## Certificados TLS do ALB externo (rd-hostproject-stg-01) — PLATENG-4843

**Concluído em 2026-06-17** — migração dos certs do ALB externo de SELF_MANAGED para GCP Certificate Manager MANAGED (renovação automática pelo Google).

**PRs aplicados:**
- `ResultadosDigitais/tf-projects#11483` — cria recursos do Certificate Manager em `rd-hostproject-stg-01`: dns_authorization, certificates, certificate_map, certificate_map_entries; substitui `ssl_certificates` no `gke-ingress-https-proxy-certmap` por `certificate_map`
- `ResultadosDigitais/tf-projects#11485` — adiciona CNAMEs de validação DNS: `_acme-challenge.staging.rdops.systems` no Cloud DNS (staging-234557) e `_acme-challenge.rd.services` no Route53 (Z3XETKI52G6M8)

**Estado atual:**
- `wildcard-staging-rdops-systems` — ACTIVE, expira 2026-09-15, renovação automática
- `wildcard-rd-services` — ACTIVE, expira 2026-09-15, renovação automática
- Target HTTPS proxy: `gke-ingress-https-proxy-certmap` (novo nome após replace com create_before_destroy)
- Certs SELF_MANAGED antigos (`wildcard-staging-rdops-systems`, `wildcard-rd-services`, `wildcard-staging-rdops-systems-2`) ainda existem no projeto mas não estão mais no proxy — podem ser deletados

**Aprendizado técnico:** Google provider 4.63.0 não suporta a transição de `ssl_certificates` para `certificate_map` via `setSslCertificates` API. Solução: rename do proxy + `create_before_destroy` cria um novo proxy com `certificate_map` do zero, evitando a chamada problemática.

## Estratégia de flip de tráfego NGINX → APISIX (2026-06-17)

### Dois caminhos de flip — externo vs. interno

O caminho depende de qual NGINX controller serve o host atualmente:

| Controller | IP | Caminho de flip |
|---|---|---|
| NGINX externo (`nginx-ingress-nginx-controller`) | `35.184.12.116` | DNS → IP do ALB externo → URL Map → APISIX |
| NGINX interno (`nginx-ingress-nginx-controller-internal`) | `10.3.96.94` | DNS → `k8s-apisix-internal.staging.rdops.systems` (`10.3.96.100`) |

**Atenção:** o sufixo "-internal" nos nomes de host (ex: `hydra-api-internal`, `accounts-internal`) é **convenção de nomenclatura da API**, não indica que o host seja VPC-only. Verificado via `kubectl get ingress -n accounts-auth`: todos os ingresses do accounts-auth têm ADDRESS `35.184.12.116` — o NGINX externo. O flip deles é pelo ALB.

Hosts genuinamente internos (VPC-only, ADDRESS `10.3.96.94`) precisam de tratamento diferente: mudar DNS de `k8s-nginx-ingress-internal.staging.rdops.systems` para `k8s-apisix-internal.staging.rdops.systems`. Nenhum host do accounts-auth é desse tipo.

### Arquitetura do flip externo (via ALB)

O flip acontece no **URL Map do ALB** (não no DNS). O DNS muda apenas uma vez por host para apontar ao IP do ALB; a partir daí, os flips NGINX↔APISIX são feitos trocando o `default_service` no URL Map namespace por namespace, sem TTL, reversível em segundos.

O PR #11508 aplicou as mudanças necessárias no URL Map:
- `default_service` do URL Map → `api-gateway-staging-bes` (APISIX)
- path_matcher `rd-gke-staging` → `api-gateway-staging-bes` (APISIX) para todos os hosts listados
- Novo path_matcher `rd-gke-staging-old-nginx-fallback` → `nginx-ingress-staging-bes` para o hostname interno do NGINX

Para viabilizar o rollback pelo ALB, o NGINX também precisa ser um backend registrado no ALB via NEG. O APISIX NEG já existe (`neg-api-gateway-svc`, criado via annotation no Service). O NGINX NEG precisa ser criado.

### Componentes e onde ficam

| Componente | Onde | Status |
|---|---|---|
| URL Map ALB com hosts accounts-auth e path_matcher APISIX | `tf-projects` `rd-hostproject-stg-01/global-lb.tf` | PR #11508 mergeado |
| NGINX NEG Service (annotation `cloud.google.com/neg`) | `helm-apisix` — manifesto avulso `k8s/staging-234557/config/manifests/ingress-nginx/nginx-neg-service.yaml` | Criado — aplicar com `kubectl apply` |
| Backend Service NGINX no ALB | `tf-projects` — a criar após NEG existir | Pendente |
| Flip por namespace (DNS → ALB) | Cloud DNS / Route53 | Pendente |

### DNS atual dos hosts accounts-auth (staging-234557)

Todos os hosts do accounts-auth são servidos pelo **NGINX externo** (`35.184.12.116`), não pelo ILB. O flip DNS é de `35.184.12.116` para o IP do ALB externo. O `accounts-staging.rdstation.com.br` vai por CDN externo → `35.190.52.66` (já roteia pelo ALB).

### NGINX NEG Service — detalhes

**Arquivo:** `~/workspace/helm-apisix/k8s/staging-234557/config/manifests/ingress-nginx/nginx-neg-service.yaml`

Manifesto avulso (fora do chart) — decisão intencional pois é temporário e será removido após a migração completa. Cria um Service ClusterIP no namespace `ingress-nginx` com annotation `cloud.google.com/neg` que instrui o GKE a criar o NEG `neg-nginx-controller-internal` nas zonas do cluster.

Para aplicar:
```bash
kubectl config use-context gke_staging-234557_us-central1_cluster-staging
kubectl apply -f ~/workspace/helm-apisix/k8s/staging-234557/config/manifests/ingress-nginx/nginx-neg-service.yaml
```

Para confirmar NEG criado nas zonas:
```bash
gcloud compute network-endpoint-groups list --filter="name=neg-nginx-controller-internal" --project=staging-234557
```

**REMOVER após migração completa e NGINX descomissionado:**
```bash
kubectl delete -f ~/workspace/helm-apisix/k8s/staging-234557/config/manifests/ingress-nginx/nginx-neg-service.yaml
```

### Próximos passos

1. Aplicar o manifesto avulso: `kubectl apply -f ~/workspace/helm-apisix/k8s/staging-234557/config/manifests/ingress-nginx/nginx-neg-service.yaml`
2. Confirmar NEG `neg-nginx-controller-internal` criado nas 4 zonas (a/b/c/f)
3. Criar backend service NGINX no ALB via tf-projects referenciando o NEG
4. **Flip do accounts-auth:** mudar DNS dos 7 hosts de `35.184.12.116` (NGINX externo) para IP do ALB — o URL Map (PR #11508) já está configurado para rotear esses hosts ao APISIX
5. Validar tráfego e monitorar por período
6. Rollback se necessário: mudar `default_service` do path_matcher `rd-gke-staging` de `api-gateway-staging-bes` para `nginx-ingress-staging-bes` no tf-projects
7. Repetir para demais namespaces

### Observação sobre sync do ingress controller (2026-06-17)

O ingress controller APISIX está com erro `HTTP 500: socket hang up` ao tentar sincronizar via admin API (ADC sync). Os GETs funcionam normalmente — o problema é no write do sync completo (provavelmente payload grande de SSL certs: 1.4MB). As 246 rotas já sincronizadas continuam servindo tráfego normalmente via etcd. Investigar antes do flip.

## Decisões arquiteturais — reuniões 2026-05-12 e 2026-05-13

### Resolvido (2026-05-12)
1. **Topologia de deploy**: **1 APISIX por nó** — decisão atualizada por Marcio Cavalante (anterior: 1 por cluster). Objetivo: resiliência com `externalTrafficPolicy: Local` — cada nó do pool de gateway deve ter exatamente 1 pod APISIX para que o NodePort entregue tráfego sem dropar conexões. Implementado via `podAntiAffinity: required` por `kubernetes.io/hostname` no chart raiz.
2. **Internal ALB para APISIX**: necessário para hosts via ILB interno — pode avançar agora que a topologia está definida.
3. **CI/CD futuro**: **Argo CD** substituirá o Spinnaker para orquestração de ingress. Developers configuram apenas DNS e rotas; infra gerencia os manifestos APISIX via Argo.
4. **helm-apisix → monorepo**: o repositório `helm-apisix` / "Helm fix" se tornará um monorepo para configs do APISIX, Ingress e API Gateway.

### Estratégia de migração — 3 etapas (confirmada em 2026-05-12)
1. **Padronização e estruturação** — validar staging, subir IALB, mapear tipos de deploy
2. **Automação** — centralizar via Argo CD, tirar a responsabilidade de deploy das equipes, bloquear admissão de manifestos NGINX antigos no k8s
3. **Corte definitivo** — remover NGINX dos templates e pipelines

### Pendente
- **Internal ALB para APISIX** — ✅ implementado (PR tf-projects#11224, IP 10.3.96.100)
- **Etcd backup/DR** — Marcio Cavalante coordenando com Everton Rocha

### Decisoes de composicao do chart raiz (apisix-gateway)
- **Keycloak e PostgreSQL**: `enabled: false` no chart raiz. Cada cluster habilita explicitamente nos seus values. PostgreSQL local existe apenas para dev/POC local.
- **Redis**: nao adicionar subchart Redis ao chart raiz. Se um plugin do APISIX que exija Redis for adotado (ex: `limit-count` com backend Redis), o subchart entra nos values do cluster especifico que precisar.

## Plano imediato — validação staging (documento "Planos Ingress NGINX x APISIX")

**Fase 1 — validação staging-234557 (174 ingresses):**
- 1.1 Subir IALB (Internal ALB para APISIX)
- 1.2 Migrar todos os 174 ingresses no cluster staging
- 1.3 Mapear todos os ingresses por tipo de deploy (Spinnaker, manual, Helm, Helm variado, outros)

**Fase 2 — decisão sobre implementação:**
- 2.1 Apresentar à DevExp os cenários de deploy NGINX existentes:
  - Deploy manual
  - Deploy via Spinnaker + Manifesto
  - Deploy via Spinnaker + Helm (default rails)
  - Deploy via Spinnaker + Helm variado (ex: RDSM)
- 2.2 Liderança técnica decide entre:
  - Solução 1: unificação das formas de deploy (CI/CD)
  - Solução 2: centralização no repo APISIX (Argo CD) — preferida pela infra
  - Solução 3: descentralização — cada app repo mantém `manifests/${env}/ingress.yaml`

## Risco crítico — Spinnaker sobrescreve migração

Ingresses com a annotation `moniker.spinnaker.io/application` têm o deploy gerenciado pelo Spinnaker. Após migrar `ingressClassName: nginx → apisix`, o Spinnaker pode **refazer o deploy com o manifesto antigo (nginx)**, revertendo a migração e gerando incidente.

**Ação pendente (Michel Ferreira):** mapear todos os ingresses que possuem `moniker.spinnaker.io/application` para identificar quais precisam de tratamento especial antes do corte.

## Migração de Ingress (padrão alvo)
```yaml
spec:
  ingressClassName: apisix  # era: nginx
```

## Scripts

Os scripts de coleta e análise estão neste repositório. O script de migração (`migrate.py`) está em um repositório separado.

```bash
python3 collect.py    # coleta ingresses dos clusters via kubectl/gcloud
python3 analyze.py    # agrega annotations por frequência/controller
python3 depara.py     # gera CSVs e JSON de mapeamento NGINX/Kong → APISIX
```

### Detalhes dos scripts
- `collect.py` — configura contextos kubectl via gcloud, coleta JSON de cada ingress → `data/raw/<project>__<cluster>.json`
  - Flags: `--cluster <name>` (um só cluster), `--force` (re-coletar)
- `analyze.py` — gera `output/annotations_summary.csv` (86 linhas) e `output/features_report.json`
- `depara.py` — mapeamento hardcoded NGINX→APISIX e Kong→APISIX → `output/depara_nginx_apisix.csv` e `.json`

## Script de migração — migrate.py

**Localização:** `~/workspace/sre-foundation-scripts/nginx-apisix-migration/` (branch `feature/PLATENG-4506-nginx-apisix-migration`)

O script lê os JSONs coletados (`data/raw/*.json`) e gera os manifestos APISIX migrados.

```bash
python3 migrate.py                        # gera em output/migrated/<cluster>/<namespace>/
python3 migrate.py --dest ~/workspace/helm-apisix  # gera em k8s/<cluster>/config/manifests/<namespace>/
python3 migrate.py --cluster rd-crm-stg-01         # processa um cluster só
python3 migrate.py --dry-run              # simula sem salvar
```

### Padrão A — troca direta de annotation
`ingressClassName: nginx` → `ingressClassName: apisix` + annotations NGINX renomeadas para equivalentes APISIX.
Arquivo gerado: `<ingress>.yaml`

### Padrão B — companion ApisixPluginConfig
Usado quando não existe annotation equivalente no APISIX IC (ex: `proxy-body-size`).
O Ingress recebe `k8s.apisix.apache.org/plugin-config-name: <ingress>-plugins` e um arquivo companion é gerado.
Arquivo gerado: `<ingress>.yaml` + `<ingress>-plugins.yaml`

Exemplo (`proxy-body-size: 128m` → `client-control.max_body_size: 134217728`):
```yaml
apiVersion: apisix.apache.org/v2
kind: ApisixPluginConfig
metadata:
  name: <ingress>-plugins
  namespace: <namespace>
spec:
  ingressClassName: apisix
  plugins:
    - name: client-control
      enable: true
      config:
        max_body_size: 134217728  # Nm → N×1048576 bytes
```

**Resultado da primeira execução (2026-05-07):** 411 ingresses NGINX migrados.

## Status da coleta
- **2026-05-07:** 564 ingresses coletados de 36 clusters (re-coleta em sre-foundation-scripts, 0 erros)
- **2026-04-08:** 609 de 709 coletados (100 não encontrados — namespaces efêmeros), 395 nginx, 163 kong, 51 unknown
- 86 annotation keys únicas (58 nginx + 28 kong)

## O que é o depara
Mapeamento **annotation por annotation**: para cada annotation NGINX/Kong presente nos Ingresses dos clusters, define a annotation equivalente no APISIX.

A estratégia preserva o recurso Ingress — **não reescreve os manifestos** para CRDs (ApisixRoute, etc.).
A migração de cada Ingress é: mudar `ingressClassName: nginx` → `ingressClassName: apisix` e trocar as annotations uma a uma pelo equivalente que o APISIX Ingress Controller entende.

## Status do depara (2026-04-14)
- **Suporte completo:** 57 annotations — mapeamento direto
- **Suporte parcial:** 8 annotations (detalhes abaixo)
- **Manual (caso a caso):** 6 annotations (detalhes abaixo)
- **Sem suporte nativo:** 15 annotations (detalhes abaixo)

## Análise das annotations não triviais — tickets JIRA filhos de PLATENG-4497

### Parcial (JIRA concluídos)
| Annotation | Ticket | Ingresses | Decisão |
|---|---|---|---|
| `ssl-protocols` | PLATENG-4549 Done | 3 | Padrão do APISIX já é TLSv1.2+TLSv1.3 — sem config necessária |
| `proxy-next-upstream` | PLATENG-4550 Done | 2 | Configurar `retries` no upstream (a3s-api-internal) |
| `proxy-hide-headers` | PLATENG-4551 Done | 1 | `response-rewrite` setando header como string vazia (oraculo-admin: X-Frame-Options, CSP) |
| `auth-signin` | PLATENG-4548 **Review** | 7 | oauth2-proxy redirect — `forward-auth` não redireciona automaticamente; requer lógica adicional |
| `proxy-body-size` | PLATENG-4673 **Aberto** | 72 | Não há annotation equivalente no APISIX IC — requer `ApisixPluginConfig` com plugin `client-control: max_body_size`; valor precisa conversão Nm → N×1048576 bytes |

### Manual (JIRA concluídos)
| Annotation | Ticket | Ingresses | Decisão |
|---|---|---|---|
| `server-snippet` | PLATENG-4543 Done | 29 | Maioria: `return 444` (deny), IP-based deny, keepalive configs — converter caso a caso via Lua |
| `configuration-snippet` | PLATENG-4544 Done | 20 | DNS resolver, oauth2_proxy auth_request — converter caso a caso via `serverless-pre-function` |
| `custom-http-errors` | PLATENG-4545 Done | 31 | `serverless-post-function` com Lua para interceptar status codes |
| `proxy-intercept-errors` | PLATENG-4547 Done | 1 | oraculo-admin com valor `false` (desabilitado) — não migrar |
| `default-backend` | PLATENG-4546 **Review** | 2 | saas-internal fallback — criar rota catch-all no APISIX com prioridade baixa |

### Sem suporte nativo (JIRA concluídos)
| Annotation | Ticket | Decisão |
|---|---|---|
| `auth-realm` | PLATENG-4536 Done | **Reclassificado para completo** — `basic-auth` plugin tem campo `realm` nativo no APISIX 3.14.1 |
| `proxy-buffer-size` | PLATENG-4537 Done | Maioria copy/paste sem efeito real — não migrar. Casos reais: declarar no nginx.conf base via helm |
| `proxy-buffers-number` | PLATENG-4538 Done | Sem suporte por rota (não há `$proxy_buffers` via ngx.var). Todos valor 32 — verificar se padrão APISIX atende |
| `proxy-max-temp-file-size` | PLATENG-4539 Done | Combinação contraditória com `proxy-buffering=off` (com buffering off, temp files não são usados) — sem efeito real, não migrar |
| `session-cookie-expires` | PLATENG-4540 Done | devexp/backstage — não está no last-applied (manual via Lens). Backstage usa PostgreSQL como session store, sticky session desnecessária |
| `session-cookie-max-age` | PLATENG-4541 Done | Mesmo caso do 4540 — sem configuração necessária |
| `reload-ts` | PLATENG-4542 Done | Timestamp interno do NGINX controller, sem função de roteamento — não migrar |

## Status dos cards principais (PLATENG-4497)
- **PLATENG-4504** Review — depara de annotations concluído
- **PLATENG-4506** In Progress — automação de migração dos manifests (altera `ingressClassName` e converte annotations)
- **PLATENG-4505** Review — reservar IPs do NGINX
- **PLATENG-4507~4512** Prioritized — infraestrutura, validação, execução por cluster, rollback, remoção NGINX

## Status da migração staging-234557 (2026-06-08)

**PR aberto:** https://github.com/ResultadosDigitais/helm-apisix/pull/39 (branch `feat/staging-234557-apisix-migration`)

**Controller:** 246 rotas sincronizadas no APISIX admin API, 0 erros ativos.

**Cobertura:** 189 ingresses NGINX+sem-classe → 176 com par APISIX, 13 fora do escopo. **0 ingresses sem par pendentes.**

**Fora do escopo / sem par intencional (13 ingresses):**
- `contact-mgmt` (4 ingresses: `cdp-core`, `clustering-engine`, `clustering-engine-elasticsearch`, `leads-pls-worker`): sem `moniker.spinnaker.io/application` — decisão de destino de deploy pendente com a equipe
- Namespaces efêmeros/temporários: `crm-es-upgrade/kibana-crm`, `es-testes/kibana-crm`, `review-crmgrowt3481` (2 ingresses) — não migrar
- `ingress-nginx/catch-all-deny-ingress` — ingress de negação do próprio controller NGINX, não faz sentido no APISIX
- `kong-system` (2 ingresses do Kong admin/manager) — fora do escopo
- `rd360-entities/objects-api` — ingress novo, surgiu após a coleta original de dados; verificar antes da migração prod
- `attract/es-staging-ilb` — GKE native ILB (IP próprio, não o NGINX LB), fora do escopo

**Descobertas durante migração:**
- `--watch-ingress-without-class=true` no NGINX controller: 8 ingresses sem classe estavam sendo servidos pelo NGINX e não apareciam no mapeamento automático. Incluiu `sre/sidekiq-dashboard-oauth` (oauth2-proxy companion) e 7 APIs sem annotation de classe.
- `sre/sidekiq-dashboard-gw`: solução `forward-auth` + `serverless-post-function` para replicar `auth-signin` do NGINX (documentado no PLATENG-4548).

**Correções de TLS aplicadas durante migração:**
- `rdsm/web-gw`: `secretName` trocado de `wildcard-rdstation-com-br` para `rdstation-com-br` — webhook de admissão APISIX exige consistência de secret por host em todo o namespace
- `insights/ai-platform-builder-gw` e `ai-platform-builder-app-gw`: `wildcard-staging-rdstation-com` não existe no namespace — substituído por `wildcard-rdstation-com`

**Pendente para 100% (annotations manuais — ~22 ingresses no staging):**
- Implementar `serverless-post-function` para `custom-http-errors` (accounts-auth, platform, email, rms, crm, rdsm)
- Implementar `serverless-pre-function` para `configuration-snippet` (connect, rdsm, crm)
- Implementar `server-snippet` via Lua (crm, connect)
- Definir destino dos 4 ingresses `contact-mgmt` sem Spinnaker

## JIRA — acesso direto via API REST

O MCP do JIRA pode não aparecer como ferramenta disponível na sessão. Nesse caso, usar a API REST diretamente — **nunca perguntar ao usuário**:

```bash
TOKEN=$(python3 -c "import json; print(json.load(open('/home/MICHEL.FERREIRA/.claude/settings.json'))['mcpServers']['jira']['env']['JIRA_API_TOKEN'])")
curl -s -X POST "https://rdstation.atlassian.net/rest/api/3/issue/PLATENG-XXXX/comment" \
  -u "michel.ferreira@rdstation.com:$TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": {"type": "doc", "version": 1, "content": [...]}}'
```

## Planilha de depara
https://docs.google.com/spreadsheets/d/1zaxpESEAjD8uPKcrROjagVP2iiXh5T6XqN3RzCgMjaQ/edit?gid=861619717

## Referências
- Documentação APISIX: usar versão **3.16.0** para staging-234557, **3.14.1** para rd-crm-stg-01 (`apisix.apache.org`)
- CLAUDE.md do helm-apisix: `~/workspace/helm-apisix/CLAUDE.md` (arquitetura detalhada)
- Planilha fonte: `~/Downloads/Ingress a serem migrados (WIP).xlsx` — sheet `ingresses_20260327_170644`
- JIRA: PLATENG-4504 (depara principal), PLATENG-4534 (proxy-buffering=off — concluído)
- Issue de padrões de migração (Padrão A e B): https://github.com/ResultadosDigitais/helm-apisix/issues/21
- Plano completo de migração: https://github.com/ResultadosDigitais/helm-apisix/issues/23

## Aprendizados técnicos relevantes

### Checklist de flip por namespace (descoberto durante accounts-auth em 2026-06-22)

Antes de criar o PR de flip DNS de qualquer namespace, executar esta sequência:

**1. Identificar o controller real de cada host — não confiar no nome do host**
- O sufixo "-internal" no hostname é convenção de nomenclatura da API, não indica VPC-only.
- Verificar o ADDRESS do ingress nginx via kubectl: `35.184.12.116` = NGINX externo (NLB), `10.3.96.94` = NGINX interno (ILB).
- Verificar o DNS real: `dig <host> +short` — pode divergir do ADDRESS do ingress se ambos os controllers servirem o mesmo ingress.
- **Regra:** quem decide o caminho real é o DNS, não o ADDRESS do ingress.

**2. Verificar onde o DNS é gerenciado no terraform**
- DNS de um namespace pode estar em domínios terraform diferentes do esperado. Ex: `a3s-api.staging.rdops.systems` é ingress de `accounts-auth` mas o DNS fica em `domains/connect/staging-234557/`.
- Grep por host no tf-projects: `grep -rn "<host>" ~/workspace/tf-projects/ --include="*.tf" -l`

**3. Checar blast radius antes de mudar variável**
- Variáveis como `internal_ingress` podem ser compartilhadas com outros namespaces/hosts no mesmo arquivo tf.
- Sempre checar todos os usos: `grep -n "internal_ingress" <arquivo>.tf`
- Se a variável cobre hosts de outros namespaces ainda não migrados, hardcodar o CNAME só no record específico, sem tocar a variável.

**4. Checar IPs hardcoded**
- Alguns records usam IP direto (tipo A) em vez da variável. Buscar: `grep -n "10\.3\.96\.94" *.tf`
- Trocar para CNAME `k8s-apisix-internal.staging.rdops.systems.` e tipo A → CNAME.

**5. Confirmar que todos os hosts têm par APISIX no cluster**
```bash
kubectl get ingress -n <namespace> -o json | jq -r '.items[] | "\(.spec.ingressClassName // "none") | \(.metadata.name) | \(.spec.rules[0].host)"' | sort
```

**6. Confirmar rotas sincronizadas no APISIX admin API**
```bash
APISIX_KEY=$(kubectl get secret apisix-admin-credentials -n ingress-apisix -o jsonpath='{.data.admin}' | base64 -d)
kubectl exec -n ingress-apisix deploy/apisix -c apisix -- \
  curl -s "http://localhost:9180/apisix/admin/routes?page_size=500" \
  -H "X-API-KEY: $APISIX_KEY" | python3 -c "
import json,sys; data=json.load(sys.stdin); routes=data.get('list',[])
hosts=['<host1>','<host2>']
found={}
[found.setdefault(r['value']['host'],[]).append(r['value'].get('uri','?')) for r in routes if r.get('value',{}).get('host') in hosts]
[print(h+': '+(str(found[h]) if h in found else 'NAO ENCONTRADO')) for h in hosts]"
```

**7. Identificar hosts com roteamento especial (CDN próprio, Kong bypass)**
- Verificar se o DNS tem CNAME intermediário (`cdn-*`, etc.) e o que está por trás:
  ```bash
  dig <host> +short  # checar se há CNAME intermediário
  gcloud compute addresses list --project=<project> --filter="address=<IP>"
  gcloud compute forwarding-rules list --project=<project> --filter="IPAddress=<IP>"
  ```
- Em staging, alguns hosts são roteados pelo Kong diretamente para o Service (bypassa o ingress nginx e APISIX). Nesses casos o flip requer mudança na config do Kong, não só no DNS.
- Exemplo real: `accounts-staging.rdstation.com.br` em staging → CDN dedicado (`um-accounts-staging`) → Kong → `rd-auth.accounts-auth.svc` — fora do escopo do flip via DNS.

**8. Dois caminhos de flip — escolher o correto por host**
- DNS aponta para NGINX externo (`35.184.12.116`) → flip: mudar DNS para IP do ALB externo (`136.110.194.246` em rd-hostproject-stg-01); o URL Map do PR #11508 já roteia para APISIX.
- DNS aponta para NGINX ILB (`10.3.96.94`) → flip: mudar DNS CNAME para `k8s-apisix-internal.staging.rdops.systems.` (`10.3.96.100`).

**Estado do PR do accounts-auth (2026-06-22)**
- PR: https://github.com/ResultadosDigitais/tf-projects/pull/11519
- Cobre 6 hosts via APISIX ILB: `user-confirmail`, `hydra-admin-api-internal`, `hydra-api-internal`, `platform-session-service`, `accounts-internal`, `a3s-api`
- Fora do escopo deste PR: `accounts-staging.rdstation.com.br` (Kong bypass em staging)

### Gerais (NGINX → APISIX)
- `ngx.var.proxy_buffering = 'off'` via serverless-pre-function NÃO funciona no APISIX sem declarar a variável no nginx.conf base
- `X-Accel-Buffering: no` no header de resposta do backend é a solução correta e funciona nativamente no APISIX
- fastapi_ai_sdk e NestJS MCP (SSE) já enviam `X-Accel-Buffering: no` por padrão
- Sempre verificar manifest do Spinnaker quando a annotation não está no repo GitHub
- Label `k8slens-edit-resource-version: v1` confirma edição manual via Lens (não gerenciado por pipeline)
- DNS discovery habilitado no staging-234557 com `resolv_conf: /etc/resolv.conf`
- URI routing com parâmetros no staging-234557: `http.router: radixtree_uri_with_parameter`
- Annotation `moniker.spinnaker.io/application` identifica ingresses gerenciados pelo Spinnaker — risco de reversão pós-migração
- Erro 400 no sistema de Accounts causado por cookie excedendo 8KB (Kong/NGINX rejeitam na camada de proxy) — não relacionado à migração APISIX
- Múltiplos LBs em sequência adicionam headers de encaminhamento/rastreamento, podendo estourar o limite de 8KB dos proxies
- Spinnaker exige um pipeline por namespace — Argo CD + Kustomize é mais flexível e permite abstração dos manifestos APISIX para os devs

### Aprendizados críticos para migração de clusters produtivos

**TLS e secrets — validar antes de aplicar**
- NGINX tolera secrets TLS inexistentes silenciosamente (o ingress fica ativo mesmo sem o secret). O APISIX controller falha imediatamente ao tentar criar a rota. **Antes de aplicar qualquer manifesto em produção, verificar que todos os `secretName` referenciados existem no namespace correto.**
- O webhook de admissão APISIX (`vingress-v1.kb.io`) exige que o mesmo host use o mesmo `secretName` em todos os ingresses do namespace. Se o NGINX tinha inconsistência (ex: `web` usando `wildcard-X` enquanto outros usavam `rdstation-com-br` para o mesmo host), é necessário alinhar ao secret majoritário antes de aplicar.
- Secret `wildcard-staging-rdstation-com` não existe no cluster staging-234557 — o correto é `wildcard-rdstation-com` (cobre `*.rdstation.com`). Verificar quais secrets existem de fato em cada namespace antes de aplicar.

**Namespace e credenciais do APISIX**
- Os pods do APISIX ficam no namespace `ingress-apisix`, NÃO em `apisix`.
- A chave de admin do APISIX está no secret `apisix-admin-credentials`, campo `admin` (não `admin-key`).
- Para validar rotas: `kubectl exec -n ingress-apisix deploy/apisix -c apisix -- curl -s http://localhost:9180/apisix/admin/routes -H 'X-API-KEY: <key>' | jq '.total'`

**Race condition ao aplicar Ingress + ApisixPluginConfig juntos**
- Quando Ingress e ApisixPluginConfig estão no mesmo YAML (`---`), o controller pode logar `"PluginConfig <name>-plugins not found"` logo após o apply. Isso resolve automaticamente no próximo ciclo de reconciliação — não é erro real, não requer ação.

**Logs de ruído esperado no controller**
- O `ssl-conflict-detector` emite erros do tipo `"ingress class is not controlled by us"` para todos os ingresses do cluster, incluindo nginx/kong/gce. Esses logs são ruído esperado — ignorar ao diagnosticar problemas reais.

**Classificação de ingresses pelo deploy_types.py**
- `app_group: ingress` é uma label customizada de algumas apps, NÃO indica o chart rdsm. No chart rdsm real, `app_group` tem valor igual ao componente (`web`, `api`, `worker`). A checagem correta é `labels["app_group"] != "ingress"`.
- Pipelines "canary" (ex: "Staging Canary") SÃO o deploy principal para algumas apps (ex: `data-platform-data-access-api`). Nunca filtrar pipelines com "canary" no nome.
- Apps que usam **pipeline templates** (WebHookStagingTemplate / WebHookProductionTemplate) retornam stages vazios na API `/pipelineConfigs`. O artifact do chart fica em `variables` ou `expectedArtifacts` na raiz do pipeline — é necessário varrer o JSON completo, não só `inputArtifacts` de stages.
- Sempre validar a saída do `classify()` em uma amostra de ingresses antes de confiar em classificações em massa.

**Backends ausentes em produção**
- Ingresses que referenciam Services inexistentes no namespace são aplicados com sucesso mas a rota falha em runtime. O NGINX mascara esse problema; o APISIX também aceita o ingress mas a rota não funciona. Verificar que todos os Services de backend existem antes de migrar.
- Exemplo real: `rms/contas-staging-gw` referencia Service `rabbitmq` que não existe no namespace `rms`.

**Ingresses sem ingressClassName (`--watch-ingress-without-class`)**
- Se o NGINX controller estiver configurado com `--watch-ingress-without-class=true`, ele serve ingresses que não têm `spec.ingressClassName` nem a annotation `kubernetes.io/ingress.class`. Esses ingresses **não aparecem** na coleta do `deploy_types.py` (que filtra por `ingressClassName: nginx`) e ficam de fora do mapeamento automático.
- Antes de migrar qualquer cluster, verificar se o flag está ativo: `kubectl get deployment -n ingress-nginx <controller> -o jsonpath='{.spec.template.spec.containers[0].args}' | tr ',' '\n' | grep watch-ingress-without-class`
- Se ativo, listar os ingresses sem classe e verificar quais têm o IP do NGINX LB no status (esses estão no escopo): `kubectl get ingress -A -o json | jq -r '.items[] | select((.spec.ingressClassName == null) and (.metadata.annotations["kubernetes.io/ingress.class"] == null)) | "\(.metadata.namespace)/\(.metadata.name) \(.status.loadBalancer.ingress[0].ip)"'`
- Ingresses com IP diferente do NGINX LB (ex: GKE ILB próprio) estão fora do escopo mesmo sem classe.
- Exemplo real: staging-234557 tinha 8 ingresses sem classe servidos pelo NGINX — todos descobertos apenas após checar o flag. Incluía o `sre/sidekiq-dashboard-oauth` (oauth2-proxy companion) e 7 APIs sem annotation de classe.

**Namespaces efêmeros e novos ingresses**
- Namespaces de review/temporários (`review-*`, `crm-es-upgrade`, `es-testes`) devem ser ignorados — são efêmeros e recriam constantemente.
- Sempre re-coletar os dados do cluster com `collect.py --force` antes de iniciar migração em produção. Novos ingresses surgem após a coleta original e ficariam de fora do mapeamento.

**Spinnaker — restrições de acesso**
- Algumas apps Spinnaker retornam HTTP 403 na API de pipelines. Nesses casos não é possível extrair chart/repo automaticamente — registrar como pendência manual.
- Para apps com pipelines templates sem execuções registradas, o fallback de buscar nas últimas execuções também não retorna dados. Verificar diretamente na UI do Spinnaker.

**Risco de reversão pelo Spinnaker**
- Todo ingress com `moniker.spinnaker.io/application` tem o deploy controlado pelo Spinnaker. Após trocar `ingressClassName: nginx → apisix`, o Spinnaker pode refazer o deploy com o manifesto antigo e reverter a migração. Em produção, é preciso desabilitar ou atualizar o stage de deploy do Spinnaker antes do corte, ou garantir que o manifesto APISIX esteja na pipeline.

**ingressClassName hardcoded no TGZ do chart — verificar antes de migrar**
- O TGZ que o Spinnaker baixa pode ser uma versão antiga diferente do source atual em `charts/`. Sempre inspecionar o TGZ real: baixar via API do GitHub, descompactar e verificar `templates/ingress.yaml`.
- Comando de verificação:
  ```bash
  curl -sL "https://api.github.com/repos/ResultadosDigitais/helm/contents/<chart>.tgz" \
    -H "Authorization: token $(gh auth token)" | python3 -c "
  import json,sys,base64; d=json.load(sys.stdin); open('/tmp/chart.tgz','wb').write(base64.b64decode(d['content']))"
  mkdir -p /tmp/chart && tar -xzf /tmp/chart.tgz -C /tmp/chart
  grep -n "nginx\|ingressClassName" /tmp/chart/*/templates/ingress.yaml
  ```
- Se `ingressClassName: nginx` estiver hardcoded (sem `| default`), é necessário corrigir o chart antes de migrar as apps — sem isso, mesmo que a app declare `className: apisix` nos values, o campo é ignorado.
- Correção: trocar `{{ $deployment.ingress.className }}` por `{{ $deployment.ingress.className | default $.Values.ingress.className }}` e manter `className: "nginx"` no `values.yaml` como default. Buildar novo TGZ com `scripts/build-charts.sh` e abrir PR no `ResultadosDigitais/helm`.
- Charts corrigidos: `go-app-0.9.1.tgz` (era `0.7.0`), `rails-0.6.3.tgz` (era `0.5.3`).

**Atualizar versão do chart nas pipelines Spinnaker após novo TGZ**
- Após merge do TGZ no `ResultadosDigitais/helm`, atualizar a variável `helmChart` nas pipelines Staging de cada app via API:
  ```bash
  TOKEN="<session>"
  # 1. buscar config da pipeline
  config=$(curl -s "https://spinnaker-api.rdops.systems/applications/<app>/pipelineConfigs" \
    -H "Cookie: SESSION=$TOKEN" | python3 -c "
  import json,sys; pipes=json.load(sys.stdin)
  [print(json.dumps(p)) for p in pipes if p['name']=='Staging']")
  # 2. atualizar variável e salvar
  echo "$config" | python3 -c "
  import json,sys; p=json.load(sys.stdin); p['variables']['helmChart']='<new>.tgz'; print(json.dumps(p))" \
    | curl -s -X POST "https://spinnaker-api.rdops.systems/pipelines" \
      -H "Cookie: SESSION=$TOKEN" -H "Content-Type: application/json" -d @-
  ```
- Atualizar apenas pipelines Staging — nunca produção sem validação no staging antes.
- O gate do Spinnaker fica em `spinnaker-api.rdops.systems` (não `spinnaker.rdops.systems/gate`, que retorna 503).
- Após atualizar pipelines, refletir as novas versões no `output/skip_helm_chart_mapping.csv`.

**Validação obrigatória de paths reais antes de qualquer flip de DNS (incidente 2026-06-22)**

**Premissa inegociável:** nunca flipar o DNS de um ingress sem antes validar 100% o comportamento do APISIX com os paths reais usados pelo cliente. Validar que o APISIX tem o `service` registrado e que `GET /` retorna 200 NÃO é suficiente.

**Causa raiz do incidente:** o flip de `a3s-api.staging.rdops.systems` para o APISIX ILB causou erro fatal no rd-auth. O APISIX tinha o service registrado mas retornou `{"error_msg":"404 Route Not Found"}` para os paths gRPC-style usados pelo rd-auth (`/users/user_email:email@domain.com`). O a3s-client esperava protobuf, recebeu JSON, crashou com `Google::Protobuf::ParseError` → 500 para todos os usuários.

**Por quê o APISIX retornou 404:** o ingress do a3s-api define `path: /` com `pathType: ImplementationSpecific`. No APISIX IC com o router `radixtree_uri_with_parameter`, isso cria match exato para `/` apenas. O `:` nos paths gRPC (`/users/user_email:email`) é interpretado como separador de parâmetro URI pelo router, impedindo o match.

**Fix no ingress para serviços com paths não-triviais:**
```yaml
path: /
pathType: Prefix  # nunca ImplementationSpecific para serviços com subpaths
```

**Checklist obrigatório antes de flipar DNS de qualquer ingress:**

1. Levantar os paths reais usados pelos clientes — consultar logs do pod de backend:
   ```bash
   kubectl logs -n <namespace> <pod> --tail=1000 | grep -o '"message":"[A-Z][a-zA-Z]*"' | sort | uniq -c | sort -rn
   ```

2. Testar cada path real pelo APISIX ILB **antes** de virar o DNS:
   ```bash
   # Testar pelo IP do APISIX ILB diretamente com Host header
   curl -sk -o /dev/null -w "%{http_code} %{time_total}s\n" \
     -H "Host: <host>" \
     "https://10.3.96.100/<path-real>"
   ```
   - Resposta `{"error_msg":"404 Route Not Found"}` com `server: APISIX` = **NÃO flipar**
   - Resposta do backend (200, 401, 404 do app) = OK para flipar

3. Para serviços que usam protobuf/gRPC-HTTP transcoding — testar com os headers corretos:
   ```bash
   curl -sk -H "Host: <host>" \
     -H "Accept: application/protobuf" \
     -H "Content-Type: application/protobuf" \
     "https://10.3.96.100/<path-grpc>"
   ```
   Se o APISIX retornar JSON onde o cliente espera protobuf → crash garantido.

4. Só após todos os paths críticos validados → flipar DNS.

**Identificar serviços com protobuf/gRPC antes de migrar:** verificar se o ingress serve um serviço gRPC ou com transcoding (headers `Accept: application/protobuf`, `Content-Type: application/grpc`). Esses serviços são os de maior risco pois um 404 JSON do APISIX causa crash no cliente em vez de um erro tratável.

### Annotations críticas do NGINX — nunca remover sem verificar (incidente 2026-06-22)

**O problema:** `kubectl apply` com um manifest sem annotations executa three-way merge e **remove** todas as annotations que não estão no manifest. Edição via FreeLens tem o mesmo efeito. Isso causou um incidente em 2026-06-22 ao remover `nginx.ingress.kubernetes.io/custom-http-errors: 501` do ingress `a3s-api-internal`, fazendo o NGINX global interceptar os 404 do Firestore e retornar HTML em vez de JSON — crashando o rd-auth.

**Regra inegociável: antes de qualquer `kubectl apply` em ingress nginx existente, comparar as annotations atuais com o manifest que será aplicado.** Se o manifest não inclui annotations que existem no cluster, essas annotations serão apagadas.

**Diagnóstico: usar `data/raw/` como backup de referência**
```bash
# Diff annotations do backup vs cluster atual para um namespace
python3 - << 'EOF'
import json, subprocess

with open('data/raw/staging-234557__cluster-staging.json') as f:
    raw = json.load(f)
backup = {i['metadata']['name']: i for i in raw if i['metadata']['namespace'] == '<namespace>'}

curr = json.loads(subprocess.check_output(
    ['kubectl', 'get', 'ingress', '-n', '<namespace>', '-o', 'json']))
current = {i['metadata']['name']: i for i in curr['items']}

IGNORE = {'kubectl.kubernetes.io/last-applied-configuration'}
for name in sorted(set(backup) & set(current)):
    b_ann = {k: v for k, v in backup[name]['metadata'].get('annotations', {}).items() if k not in IGNORE}
    c_ann = {k: v for k, v in current[name]['metadata'].get('annotations', {}).items() if k not in IGNORE}
    for k in sorted(set(b_ann) | set(c_ann)):
        if b_ann.get(k) != c_ann.get(k):
            print(f'{name} [{k}]: backup={b_ann.get(k)} atual={c_ann.get(k)}')
EOF
```

**Annotations de alto impacto no NGINX — verificar se estão presentes antes de qualquer apply:**

| Annotation | Efeito se removida |
|---|---|
| `nginx.ingress.kubernetes.io/custom-http-errors: <código>` | O global do configmap passa a valer — pode interceptar 404/5xx do backend e retornar HTML em vez da resposta original |
| `nginx.ingress.kubernetes.io/ssl-redirect: false` | NGINX passa a redirecionar HTTP → HTTPS (308) mesmo em contextos onde não deve |
| `nginx.ingress.kubernetes.io/whitelist-source-range` | Abre acesso que deveria ser restrito por IP |
| `nginx.ingress.kubernetes.io/proxy-body-size` | Limita uploads ao default global (1m) |

**`nginx.ingress.kubernetes.io/custom-http-errors` por ingress sobrescreve o global.** O configmap do nginx-ingress no staging-234557 tem `custom-http-errors: 404` globalmente. Qualquer ingress que defina essa annotation por ingress (ex: `501`) está dizendo "para este ingress, só intercepta 501 — deixa o 404 do backend passar". Remover a annotation restaura o comportamento global → 404 vira HTML.

**Identificar todos os ingresses com `custom-http-errors` no backup:**
```bash
python3 -c "
import json
with open('data/raw/staging-234557__cluster-staging.json') as f:
    data = json.load(f)
key = 'nginx.ingress.kubernetes.io/custom-http-errors'
for i in data:
    v = i['metadata'].get('annotations', {}).get(key)
    if v:
        print(i['metadata']['namespace'], i['metadata']['name'], '->', v)
"
```

### grpc_pass vs proxy_pass — proxy_intercept_errors não tem efeito em gRPC (2026-06-22)

O NGINX IC detecta serviços gRPC e gera `grpc_pass grpc://upstream_balancer` em vez de `proxy_pass`. A directive `proxy_intercept_errors` **só funciona com `proxy_pass`** — não tem qualquer efeito em `grpc_pass`. Tentar injetar `proxy_intercept_errors off` via `configuration-snippet` num ingress que usa `grpc_pass` causa 502.

Não existe `grpc_intercept_errors` no nginx. Não há forma simples de desabilitar a interceptação de erros para serviços gRPC via annotation de ingress. A solução correta para esse caso é usar `custom-http-errors` por ingress (ex: `501`) para excluir o código 404 do escopo de interceptação global.

**Como identificar se um ingress usa grpc_pass:** verificar no nginx.conf do controller se o server block do host contém `grpc_pass` em vez de `proxy_pass`:
```bash
kubectl exec -n ingress-nginx <pod-nginx> -- grep -A5 "server_name <host>" /etc/nginx/nginx.conf | grep -E "grpc_pass|proxy_pass"
```

### Geração de ingresses APISIX — preservar comportamento das annotations nginx originais

Ao gerar um ingress APISIX com `migrate.py` ou manualmente, **não basta trocar `ingressClassName: nginx → apisix`**. As annotations do ingress nginx original que controlam comportamento global (ex: `custom-http-errors`) devem ser inspecionadas e o comportamento equivalente garantido no APISIX.

**Antes de criar o ingress APISIX de qualquer serviço, checar no backup:**
1. O ingress nginx original tem `custom-http-errors`? Se sim, o serviço depende do NGINX NÃO interceptar determinados códigos HTTP. No APISIX, o equivalente é garantir que o plugin `proxy-rewrite` ou `serverless-post-function` não mascare esses códigos.
2. O ingress tem `ssl-redirect: false`? Se sim, o serviço aceita requisições HTTP puras — verificar se o APISIX também não está forçando redirect.
3. O ingress tem `whitelist-source-range`? Se sim, o acesso é restrito por IP — replicar com `k8s.apisix.apache.org/allowlist-source-range` no ingress APISIX.

**O migrate.py deve alertar (nunca silenciar) quando o ingress nginx original tiver `custom-http-errors`.** Esse é um sinal de que o serviço tem comportamento especial que depende da configuração global do NGINX e que pode quebrar se o APISIX não tiver o equivalente.

### Auditoria de mudanças em ingresses via Cloud Logging (GKE)

Para investigar quem removeu annotations ou fez mudanças em ingresses, usar o Cloud Logging do GKE — os audit logs registram cada patch/update com o principal (email) e o diff aplicado:

```bash
gcloud logging read \
  'resource.type="k8s_cluster" AND resource.labels.cluster_name="cluster-staging" AND resource.labels.project_id="staging-234557" AND protoPayload.resourceName:"namespaces/<namespace>/ingresses/<nome>" AND protoPayload.methodName=~"(patch|update|create)"' \
  --project=staging-234557 \
  --format="value(timestamp, protoPayload.authenticationInfo.principalEmail, protoPayload.methodName, protoPayload.request)" \
  --limit=20 \
  --freshness=1d
```

O campo `protoPayload.request` mostra exatamente o que foi alterado — valores `None` indicam remoção de campo. Util para identificar se foi `kubectl apply`, FreeLens, Atlantis, ou Spinnaker que fez a mudança.
