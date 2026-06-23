# Validação do flip ILB — namespace accounts-auth (staging-234557)

PR flip: https://github.com/ResultadosDigitais/tf-projects/pull/11519 (revertido após incidente)
PR manifests: https://github.com/ResultadosDigitais/helm-apisix/pull/40

Hosts: migração do NGINX ILB (`10.3.96.94`) → APISIX ILB (`10.3.96.100`).

---

## Resultado da validação — 2026-06-22

Todos os 6 hosts foram validados via APISIX ILB (`10.3.96.100`) usando `curl -H "Host: ..."` de dentro do cluster (pod `platform-session-service`).

### Resumo por host

| Host | Path testado | Status APISIX | Resposta app | OK? |
|---|---|---|---|---|
| `a3s-api.staging.rdops.systems` | `GET /` | routa | 200 (gRPC response) | ✅ |
| `a3s-api.staging.rdops.systems` | `GET /users/user_email:test@rdstation.com` | routa | 404 (Firestore: user not found) | ✅ |
| `a3s-api.staging.rdops.systems` | `GET /platform_accounts/123` | routa | 404 (app) | ✅ |
| `hydra-api-internal.staging.rdops.systems` | `GET /.well-known/openid-configuration` | routa | 200 (JSON Hydra discovery) | ✅ |
| `hydra-api-internal.staging.rdops.systems` | `GET /oauth2/auth` | routa | 302 (redirect to login) | ✅ |
| `hydra-api-internal.staging.rdops.systems` | `POST /oauth2/token` | routa | 400 (Hydra: missing params) | ✅ |
| `hydra-api-internal.staging.rdops.systems` | `GET /userinfo` | routa | 401 (Hydra: no token) | ✅ |
| `hydra-admin-api-internal.staging.rdops.systems` | `GET /health/alive` | routa | 200 `{"status":"ok"}` | ✅ |
| `hydra-admin-api-internal.staging.rdops.systems` | `GET /admin/clients` | routa | 404 (Hydra: path não existe nesta versão) | ✅ |
| `platform-session-service.staging.rdops.systems` | `GET /health_check` | routa | 200 | ✅ |
| `platform-session-service.staging.rdops.systems` | `POST /api/v1/sessions/assert` | routa | 401 (sem auth) | ✅ |
| `platform-session-service.staging.rdops.systems` | `POST /api/v1/sessions` | routa | 401 (sem auth) | ✅ |
| `platform-session-service.staging.rdops.systems` | `DELETE /api/v1/sessions/{id}/revoke` | routa | 404 (app) | ✅ |
| `platform-session-service.staging.rdops.systems` | `POST /api/v1/devices/verify` | routa | 401 (sem auth) | ✅ |
| `accounts-internal.staging.rdops.systems` | `GET /api/v1/internal` | routa | HTML rd-auth (Rails 404) | ✅ |
| `accounts-internal.staging.rdops.systems` | `GET /` | **Route Not Found** | `{"message":"no Route matched..."}` | ✅ (esperado — ingress só aceita `/api/v1/internal`) |
| `user-confirmail.staging.rdops.systems` | `GET /health_check` | routa | 200 | ✅ |
| `user-confirmail.staging.rdops.systems` | `GET /` | routa | 404 (app) | ✅ |

**Conclusão: todos os hosts roteiam corretamente via APISIX. Nenhum "Route Not Found" do APISIX nos paths esperados.**

O path crítico que causou o incidente — `GET /users/user_email:email@domain.com` — foi testado explicitamente e retorna 404 do Firestore (usuário não existe em staging), confirmando que o fix `pathType: Prefix` está funcionando.

---

## Pré-requisitos para o próximo flip

### 1 — Confirmar contexto do cluster

```bash
kubectl config use-context gke_staging-234557_us-central1_cluster-staging
```

### 2 — Verificar DNS atual (deve apontar para NGINX ILB)

```bash
for host in \
  a3s-api.staging.rdops.systems \
  user-confirmail.staging.rdops.systems \
  hydra-admin-api-internal.staging.rdops.systems \
  hydra-api-internal.staging.rdops.systems \
  platform-session-service.staging.rdops.systems \
  accounts-internal.staging.rdops.systems; do
  echo -n "$host -> "; dig +short $host
done
```

Esperado antes do flip: todos apontando para `k8s-nginx-ingress-internal.staging.rdops.systems.` / `10.3.96.94`.

### 3 — Confirmar ingresses APISIX com pathType: Prefix

```bash
kubectl get ingress -n accounts-auth -o json | python3 -c "
import json, sys
for i in json.load(sys.stdin)['items']:
    cls = i['spec'].get('ingressClassName','none')
    if cls == 'apisix':
        for r in i['spec'].get('rules',[]):
            for p in r.get('http',{}).get('paths',[]):
                print(cls, i['metadata']['name'], r.get('host'), p['path'], '['+p['pathType']+']')
" | sort
```

Todos os ingresses APISIX devem ter `[Prefix]`. Nenhum pode ter `[ImplementationSpecific]`.

### 4 — Confirmar services no APISIX admin API

```bash
kubectl port-forward -n ingress-apisix svc/apisix-admin 9180:9180 &
sleep 2

APISIX_KEY=$(kubectl get secret apisix-admin-credentials -n ingress-apisix \
  -o jsonpath='{.data.admin}' | base64 -d)

curl -s "http://localhost:9180/apisix/admin/services?page_size=500" \
  -H "X-API-KEY: $APISIX_KEY" | python3 -c "
import json, sys
svcs = json.load(sys.stdin).get('list', [])
targets = [
    'a3s-api.staging.rdops.systems',
    'user-confirmail.staging.rdops.systems',
    'hydra-admin-api-internal.staging.rdops.systems',
    'hydra-api-internal.staging.rdops.systems',
    'platform-session-service.staging.rdops.systems',
    'accounts-internal.staging.rdops.systems',
]
found = {}
for s in svcs:
    for h in s.get('value',{}).get('hosts',[]) or []:
        if h in targets: found[h] = s['value']['id']
[print(('OK' if t in found else '!!MISSING'), t) for t in targets]
"

kill %1
```

### 5 — Testar paths reais via APISIX ILB (de dentro do cluster)

```bash
# Executar de um pod com curl, ex: platform-session-service
kubectl exec -n accounts-auth deploy/platform-session-service -- sh -c '
APISIX_ILB="10.3.96.100"
for test in \
  "GET https://$APISIX_ILB/ Host:a3s-api.staging.rdops.systems" \
  "GET https://$APISIX_ILB/users/user_email:test@rdstation.com Host:a3s-api.staging.rdops.systems" \
  "GET https://$APISIX_ILB/.well-known/openid-configuration Host:hydra-api-internal.staging.rdops.systems" \
  "GET https://$APISIX_ILB/health/alive Host:hydra-admin-api-internal.staging.rdops.systems" \
  "GET https://$APISIX_ILB/health_check Host:platform-session-service.staging.rdops.systems" \
  "GET https://$APISIX_ILB/api/v1/internal Host:accounts-internal.staging.rdops.systems" \
  "GET https://$APISIX_ILB/health_check Host:user-confirmail.staging.rdops.systems"; do
  method=$(echo $test | awk "{print \$1}")
  url=$(echo $test | awk "{print \$2}")
  host=$(echo $test | awk "{print \$3}" | cut -d: -f2)
  status=$(curl -sk -o /dev/null -w "%{http_code}" -X $method -H "Host: $host" "$url")
  echo "$status $host $url"
done
'
```

**Critério de sucesso:** nenhum status `000` (timeout), nenhuma resposta com `"error_msg":"404 Route Not Found"` ou `"no Route matched"` nos paths esperados.

---

## Paths reais por serviço (levantados de logs — 2026-06-22)

| Serviço | Paths observados nos logs |
|---|---|
| `a3s-api` | `GET /` (200), `GET /users/<id>`, `GET /users/user_email:<email>`, `GET /platform_accounts/<id>`, `POST /users/<id>/signin` (gRPC: GetUser, GetPlatformAccount, SignInUser, SetPlatformAccount, SetUserPassword) |
| `hydra-api` | `GET /.well-known/openid-configuration`, `GET /oauth2/auth`, `POST /oauth2/token`, `GET /userinfo`, `GET /health/alive` |
| `hydra-admin-api` | `GET /health/alive` (200), `GET /admin/...` (404 da aplicação, não APISIX) |
| `platform-session-service` | `GET /health_check`, `POST /api/v1/sessions/assert`, `POST /api/v1/sessions`, `DELETE /api/v1/sessions/{id}/revoke`, `POST /api/v1/devices/verify` |
| `rd-auth` (accounts-internal) | `GET /api/v1/internal/*` (ingress só aceita esse prefixo) |
| `user-confirmail` | `GET /` (Datadog healthcheck), `GET /health_check` |
