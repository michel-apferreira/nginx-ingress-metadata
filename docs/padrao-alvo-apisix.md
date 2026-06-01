# Padrão alvo: Migração NGINX Ingress para APISIX

## Escopo

395 ingresses gerenciados pelo **NGINX Ingress Controller**, distribuídos em 36 clusters GKE.  
Ingresses Kong (GCP Ingress / GCE LB) estão **fora do escopo** e permanecem como estão.

## Abordagem: annotations nativas do APISIX Ingress Controller

A migração preserva o objeto `kind: Ingress` de cada aplicação. A mudança obrigatória é a classe:

```yaml
spec:
  ingressClassName: apisix   # era: nginx
```

O APISIX Ingress Controller tem annotations próprias que substituem a maioria das annotations NGINX diretamente no Ingress, **sem criar objetos extras**. Somente quando não há annotation equivalente é necessário criar objetos APISIX companheiros.

### Tabela de conversão direta

| Annotation NGINX | Annotation APISIX | Observação |
|---|---|---|
| `force-ssl-redirect: "true"` | `k8s.apisix.apache.org/http-to-https: "true"` | |
| `ssl-redirect: "true"` | `k8s.apisix.apache.org/http-to-https: "true"` | |
| `whitelist-source-range: "10.0.0.0/8"` | `k8s.apisix.apache.org/allowlist-source-range: "10.0.0.0/8"` | mesmo formato de CIDRs |
| `denylist-source-range: "1.2.3.4/32"` | `k8s.apisix.apache.org/blocklist-source-range: "1.2.3.4/32"` | |
| `proxy-read-timeout: "300"` | `k8s.apisix.apache.org/upstream-read-timeout: "300s"` | adicionar `s` ao valor |
| `proxy-connect-timeout: "60"` | `k8s.apisix.apache.org/upstream-connect-timeout: "60s"` | adicionar `s` ao valor |
| `proxy-send-timeout: "300"` | `k8s.apisix.apache.org/upstream-send-timeout: "300s"` | adicionar `s` ao valor |
| `send_timeout: "300"` | `k8s.apisix.apache.org/upstream-send-timeout: "300s"` | alias de proxy-send-timeout |
| `backend-protocol: "HTTPS"` | `k8s.apisix.apache.org/upstream-scheme: "https"` | |
| `backend-protocol: "GRPC"` | `k8s.apisix.apache.org/upstream-scheme: "grpc"` | |
| `proxy-next-upstream-tries: "3"` | `k8s.apisix.apache.org/upstream-retries: "3"` | |
| `use-regex: "true"` | `k8s.apisix.apache.org/use-regex: "true"` | mesmo formato |
| `rewrite-target: "/new"` | `k8s.apisix.apache.org/rewrite-target: "/new"` | path simples |
| `rewrite-target: "/api/$2"` + `use-regex` | `k8s.apisix.apache.org/rewrite-target-regex` + `rewrite-target-regex-template` | ver Caso 5b |
| `enable-cors: "true"` | `k8s.apisix.apache.org/enable-cors: "true"` | |
| `cors-allow-origin: "https://..."` | `k8s.apisix.apache.org/cors-allow-origin: "https://..."` | |
| `cors-allow-headers: "Auth, CT"` | `k8s.apisix.apache.org/cors-allow-headers: "Auth,CT"` | |
| `cors-allow-methods: "GET, POST"` | `k8s.apisix.apache.org/cors-allow-methods: "GET,POST"` | sem espaços |
| `auth-url: "https://..."` | `k8s.apisix.apache.org/auth-uri: "https://..."` | |
| `auth-response-headers: "X-User"` | `k8s.apisix.apache.org/auth-upstream-headers: "X-User"` | |
| `proxy-hide-headers: "X-Foo,X-Bar"` | `k8s.apisix.apache.org/enable-response-rewrite: "true"` + `response-rewrite-remove-header: "X-Foo,X-Bar"` | |

### Quando ainda é necessário criar objetos APISIX

| Caso | Objeto necessário |
|---|---|
| `proxy-body-size` | `ApisixPluginConfig` com plugin `client-control` |
| `upstream-vhost` | `ApisixPluginConfig` com plugin `proxy-rewrite` |
| `limit-rpm` | `ApisixPluginConfig` com plugin `limit-req` |
| `auth-type: basic` com credenciais | `ApisixPluginConfig` + `ApisixConsumer` |
| `cors-allow-credentials` ou `cors-max-age` | `ApisixPluginConfig` com plugin `cors` |
| `proxy-hide-headers` | `k8s.apisix.apache.org/response-rewrite-remove-header` + `enable-response-rewrite: "true"` |
| Canary / traffic split | `ApisixRoute` |

---

## Casos com troca direta de annotation

### Caso 1: Ingress simples (sem annotations de comportamento)

Ingresses com apenas `force-ssl-redirect`, `ssl-redirect` ou sem annotations relevantes.
Afeta **244+ ingresses** (o caso mais comum).

**Antes:**
```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/force-ssl-redirect: "true"
spec:
  ingressClassName: nginx
```

**Depois:**
```yaml
metadata:
  annotations:
    k8s.apisix.apache.org/http-to-https: "true"
spec:
  ingressClassName: apisix
```

---

### Caso 2: Restrição de IP (`whitelist-source-range`)

Afeta **173 ingresses**.

**Antes:**
```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/whitelist-source-range: "10.0.0.0/8,192.168.0.0/16"
    nginx.ingress.kubernetes.io/force-ssl-redirect: "true"
spec:
  ingressClassName: nginx
```

**Depois:**
```yaml
metadata:
  annotations:
    k8s.apisix.apache.org/allowlist-source-range: "10.0.0.0/8,192.168.0.0/16"
    k8s.apisix.apache.org/http-to-https: "true"
spec:
  ingressClassName: apisix
```

---

### Caso 3: Timeouts (`proxy-read-timeout`, `proxy-connect-timeout`, `proxy-send-timeout`)

Afeta **50+ ingresses**. Valores em segundos — adicionar o sufixo `s` ao migrar.

**Antes:**
```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "300"
    nginx.ingress.kubernetes.io/proxy-connect-timeout: "60"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "300"
spec:
  ingressClassName: nginx
```

**Depois:**
```yaml
metadata:
  annotations:
    k8s.apisix.apache.org/upstream-read-timeout: "300s"
    k8s.apisix.apache.org/upstream-connect-timeout: "60s"
    k8s.apisix.apache.org/upstream-send-timeout: "300s"
spec:
  ingressClassName: apisix
```

---

### Caso 4: Backend HTTPS (`backend-protocol: HTTPS`)

Afeta **46 ingresses**.

**Antes:**
```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/backend-protocol: "HTTPS"
spec:
  ingressClassName: nginx
```

**Depois:**
```yaml
metadata:
  annotations:
    k8s.apisix.apache.org/upstream-scheme: "https"
spec:
  ingressClassName: apisix
```

Para gRPC (`backend-protocol: GRPC`):
```yaml
metadata:
  annotations:
    k8s.apisix.apache.org/upstream-scheme: "grpc"
```

---

### Caso 5: Reescrita de path (`rewrite-target`)

#### 5a: Path simples (sem regex)

**Antes:**
```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /
spec:
  ingressClassName: nginx
```

**Depois:**
```yaml
metadata:
  annotations:
    k8s.apisix.apache.org/rewrite-target: /
spec:
  ingressClassName: apisix
```

#### 5b: Reescrita com regex

Afeta **12 ingresses**.

**Antes:**
```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /api/$2
    nginx.ingress.kubernetes.io/use-regex: "true"
spec:
  ingressClassName: nginx
  rules:
    - http:
        paths:
          - path: /app(/|$)(.*)
```

**Depois:**
```yaml
metadata:
  annotations:
    k8s.apisix.apache.org/use-regex: "true"
    k8s.apisix.apache.org/rewrite-target-regex: "^/app(/|$)(.*)"
    k8s.apisix.apache.org/rewrite-target-regex-template: "/api/$2"
spec:
  ingressClassName: apisix
```

---

### Caso 6: CORS básico (origins, headers, methods)

Afeta **14+ ingresses**.

**Antes:**
```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/enable-cors: "true"
    nginx.ingress.kubernetes.io/cors-allow-origin: "https://app.rdstation.com"
    nginx.ingress.kubernetes.io/cors-allow-methods: "GET, POST, OPTIONS"
    nginx.ingress.kubernetes.io/cors-allow-headers: "Authorization, Content-Type"
spec:
  ingressClassName: nginx
```

**Depois:**
```yaml
metadata:
  annotations:
    k8s.apisix.apache.org/enable-cors: "true"
    k8s.apisix.apache.org/cors-allow-origin: "https://app.rdstation.com"
    k8s.apisix.apache.org/cors-allow-methods: "GET,POST,OPTIONS"
    k8s.apisix.apache.org/cors-allow-headers: "Authorization,Content-Type"
spec:
  ingressClassName: apisix
```

---

### Caso 7: Retries (`proxy-next-upstream-tries`)

Afeta **5 ingresses**.

**Antes:**
```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/proxy-next-upstream-tries: "3"
spec:
  ingressClassName: nginx
```

**Depois:**
```yaml
metadata:
  annotations:
    k8s.apisix.apache.org/upstream-retries: "3"
spec:
  ingressClassName: apisix
```

> Para `proxy-next-upstream` com condições HTTP específicas (http_502, http_503): usar passive health check via `ApisixUpstream`.

---

### Caso 8: Remover headers do upstream (`proxy-hide-headers`)

Afeta **1 ingress**.

**Antes:**
```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/proxy-hide-headers: "X-Frame-Options, Content-Security-Policy"
spec:
  ingressClassName: nginx
```

**Depois:**
```yaml
metadata:
  annotations:
    k8s.apisix.apache.org/enable-response-rewrite: "true"
    k8s.apisix.apache.org/response-rewrite-remove-header: "X-Frame-Options,Content-Security-Policy"
spec:
  ingressClassName: apisix
```

---

## Casos que requerem objeto APISIX

### Caso 9: Tamanho máximo do body (`proxy-body-size`)

Afeta **72 ingresses**. Não há annotation equivalente: requer `ApisixPluginConfig`.

**Antes:**
```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "50m"
spec:
  ingressClassName: nginx
```

**Depois:**
```yaml
# ingress.yaml
metadata:
  annotations:
    k8s.apisix.apache.org/plugin-config-name: my-app-plugins
spec:
  ingressClassName: apisix
---
# apisix-plugin-config.yaml
apiVersion: apisix.apache.org/v2
kind: ApisixPluginConfig
metadata:
  name: my-app-plugins
  namespace: <namespace>
spec:
  plugins:
    - name: client-control
      enable: true
      config:
        max_body_size: 52428800   # 50m em bytes (50 * 1024 * 1024)
```

> Conversão: `Nm` equivale a `N * 1048576` bytes. `0` = sem limite.

---

### Caso 10: Host do upstream (`upstream-vhost`)

Afeta **32 ingresses**. Não há annotation equivalente: requer `ApisixPluginConfig`.

**Antes:**
```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/upstream-vhost: "internal.service.cluster.local"
spec:
  ingressClassName: nginx
```

**Depois:**
```yaml
# ingress.yaml
metadata:
  annotations:
    k8s.apisix.apache.org/plugin-config-name: my-app-plugins
spec:
  ingressClassName: apisix
---
# apisix-plugin-config.yaml
apiVersion: apisix.apache.org/v2
kind: ApisixPluginConfig
metadata:
  name: my-app-plugins
  namespace: <namespace>
spec:
  plugins:
    - name: proxy-rewrite
      enable: true
      config:
        host: "internal.service.cluster.local"
```

---

### Caso 11: Rate limiting (`limit-rpm`)

Afeta **3 ingresses**. Não há annotation equivalente: requer `ApisixPluginConfig`.

**Antes:**
```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/limit-rpm: "100"
    nginx.ingress.kubernetes.io/limit-burst-multiplier: "5"
spec:
  ingressClassName: nginx
```

**Depois:**
```yaml
# ingress.yaml
metadata:
  annotations:
    k8s.apisix.apache.org/plugin-config-name: my-app-plugins
spec:
  ingressClassName: apisix
---
# apisix-plugin-config.yaml
apiVersion: apisix.apache.org/v2
kind: ApisixPluginConfig
metadata:
  name: my-app-plugins
  namespace: <namespace>
spec:
  plugins:
    - name: limit-req
      enable: true
      config:
        rate: 1.67        # limit-rpm / 60 (requests por segundo)
        burst: 500        # rate * limit-burst-multiplier * 60 (aproximado)
        key_type: "var"
        key: "remote_addr"
```

---

### Caso 12: CORS com credentials ou max-age

As annotations APISIX não suportam `allow-credentials` nem `max-age`: requer `ApisixPluginConfig`.

**Antes:**
```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/enable-cors: "true"
    nginx.ingress.kubernetes.io/cors-allow-origin: "https://app.rdstation.com"
    nginx.ingress.kubernetes.io/cors-allow-credentials: "true"
    nginx.ingress.kubernetes.io/cors-max-age: "3600"
spec:
  ingressClassName: nginx
```

**Depois:**
```yaml
# ingress.yaml
metadata:
  annotations:
    k8s.apisix.apache.org/plugin-config-name: my-app-plugins
spec:
  ingressClassName: apisix
---
# apisix-plugin-config.yaml
apiVersion: apisix.apache.org/v2
kind: ApisixPluginConfig
metadata:
  name: my-app-plugins
  namespace: <namespace>
spec:
  plugins:
    - name: cors
      enable: true
      config:
        allow_origins: "https://app.rdstation.com"
        allow_methods: "GET,POST,OPTIONS"
        allow_headers: "Authorization,Content-Type"
        allow_credential: true
        max_age: 3600
```

---

### Caso 13: Autenticação básica (`auth-type: basic`)

Afeta **6 ingresses**. O plugin `basic-auth` do APISIX usa `ApisixConsumer` para armazenar credenciais.

**Antes:**
```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/auth-type: basic
    nginx.ingress.kubernetes.io/auth-secret: my-basic-auth-secret
    nginx.ingress.kubernetes.io/auth-realm: "Authentication Required"
spec:
  ingressClassName: nginx
```

**Depois:**
```yaml
# ingress.yaml
metadata:
  annotations:
    k8s.apisix.apache.org/auth-type: basicAuth
spec:
  ingressClassName: apisix
---
# apisix-consumer.yaml (um por usuário)
apiVersion: apisix.apache.org/v2
kind: ApisixConsumer
metadata:
  name: my-user
  namespace: <namespace>
spec:
  authParameter:
    basicAuth:
      value:
        username: my-user
        password: my-password   # ou via secretRef
```

---

## Annotations que são simplesmente removidas

Essas annotations não precisam de equivalente e são removidas durante a migração:

| Annotation | Motivo |
|---|---|
| `force-ssl-redirect`, `ssl-redirect` | Substituídas por `k8s.apisix.apache.org/http-to-https` |
| `ssl-protocols` | Configuração global no helm (`TLSv1.2 TLSv1.3`), já aplicada |
| `proxy-buffering` | Não configurável por rota no APISIX, padrão cobre |
| `proxy-buffers-number` | Não configurável por rota, padrão (8x4k) cobre |
| `proxy-max-temp-file-size` | Não configurável por rota, padrão (1024m) cobre |
| `proxy-buffer-size` | Não configurável por rota, padrão (4k) cobre na maioria dos casos |
| `reload-ts` | Interna do NGINX IC, ignorar |
| `session-cookie-expires`, `session-cookie-max-age` | Sem equivalente nativo, controlado pela aplicação |
| `service-upstream` | Comportamento padrão do APISIX IC |

---

## Casos que requerem análise individual

Esses ingresses não têm receita única e precisam ser tratados caso a caso:

| Annotation | Situação | Ingresses |
|---|---|---|
| `auth-signin` + `auth-url` | Decisão pendente: oauth2-proxy full-proxy vs plugin `openid-connect` | 7 |
| `server-snippet` | 7 grupos de uso identificados, solução específica por grupo | 29 |
| `configuration-snippet` | Mapear para `response-rewrite` ou `serverless-pre-function` por caso | 20 |
| `custom-http-errors` | Sem equivalente nativo, APISIX passa erro do upstream diretamente | 31 |
| `default-backend` | Sem equivalente nativo, aguardando decisão | 2 |

---

## Referências

- [Documentação APISIX 3.14](https://apisix.apache.org/docs/apisix/3.14/getting-started/README/)
- [Annotations do APISIX Ingress Controller](https://apisix.apache.org/docs/ingress-controller/reference/apisix-ingress-controller/annotation/)
- [Helm chart interno](https://github.com/ResultadosDigitais/helm-apisix)
- [Depara completo](https://docs.google.com/spreadsheets/d/1zaxpESEAjD8uPKcrROjagVP2iiXh5T6XqN3RzCgMjaQ/edit?gid=1281196205#gid=1281196205)
