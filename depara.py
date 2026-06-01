#!/usr/bin/env python3
"""
depara.py — Gera o mapeamento (depara) de features NGINX Ingress para APISIX.

Lê o features_report.json gerado pelo analyze.py e produz:
  - output/depara_nginx_apisix.csv — mapeamento completo para revisão/edição manual
  - output/depara_nginx_apisix.json — mesma informação em JSON

O CSV é a base de trabalho: pode ser aberto em planilhas e preenchido
colaborativamente com a coluna apisix_equivalente e observacoes.

Uso:
  python3 depara.py [--features <path>]
"""

import argparse
import csv
import json
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent / "output"
FEATURES_REPORT = OUTPUT_DIR / "features_report.json"

# ---------------------------------------------------------------------------
# Base de conhecimento: NGINX annotation -> equivalente APISIX
#
# Estrutura de cada entrada:
#   "nginx_annotation": {
#       "funcionalidade":     descrição da feature
#       "apisix_plugin":      plugin(s) APISIX que cobrem essa feature
#       "apisix_config":      como configurar no APISIX (resumo)
#       "suporte":            "completo" | "parcial" | "manual" | "sem_suporte"
#       "observacoes":        notas de migração
#   }
# ---------------------------------------------------------------------------

NGINX_TO_APISIX: dict[str, dict] = {
    # -----------------------------------------------------------------------
    # Rewrite / Routing
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/rewrite-target": {
        "funcionalidade": "Reescreve o path antes de encaminhar ao backend",
        "apisix_plugin": "proxy-rewrite",
        "apisix_config": "plugin proxy-rewrite: regex_uri ou uri",
        "suporte": "completo",
        "observacoes": "APISIX usa regex_uri: ['regex', 'replacement']. Sintaxe diferente do $1/$2 do NGINX.",
    },
    "nginx.ingress.kubernetes.io/use-regex": {
        "funcionalidade": "Habilita regex no path do Ingress",
        "apisix_plugin": "proxy-rewrite",
        "apisix_config": "Rota APISIX suporta regex nativamente no campo 'uri'",
        "suporte": "completo",
        "observacoes": "APISIX suporta regex nas rotas por padrão.",
    },
    "nginx.ingress.kubernetes.io/app-root": {
        "funcionalidade": "Redireciona / para um path específico",
        "apisix_plugin": "redirect",
        "apisix_config": "plugin redirect: uri",
        "suporte": "completo",
        "observacoes": "",
    },

    # -----------------------------------------------------------------------
    # SSL / TLS
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/ssl-redirect": {
        "funcionalidade": "Redireciona HTTP para HTTPS",
        "apisix_plugin": "redirect",
        "apisix_config": "plugin redirect: http_to_https: true",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/force-ssl-redirect": {
        "funcionalidade": "Força redirecionamento para HTTPS mesmo com X-Forwarded-Proto",
        "apisix_plugin": "redirect",
        "apisix_config": "plugin redirect: http_to_https: true",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/ssl-passthrough": {
        "funcionalidade": "Passa o tráfego TLS diretamente sem terminar SSL no ingress",
        "apisix_plugin": "stream-proxy (L4)",
        "apisix_config": "Configurar stream route no APISIX",
        "suporte": "parcial",
        "observacoes": "APISIX suporta via stream routes (L4). Requer configuração diferente do Ingress.",
    },
    "nginx.ingress.kubernetes.io/backend-protocol": {
        "funcionalidade": "Protocolo do backend: HTTP, HTTPS, GRPC, GRPCS, AJP, FCGI",
        "apisix_plugin": "grpc-transcode / configuração do upstream",
        "apisix_config": "Upstream scheme: http|https|grpc|grpcs",
        "suporte": "completo",
        "observacoes": "GRPC requer plugin grpc-transcode ou rota gRPC nativa.",
    },

    # -----------------------------------------------------------------------
    # Rate Limiting
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/limit-rps": {
        "funcionalidade": "Limita requisições por segundo por IP",
        "apisix_plugin": "limit-req",
        "apisix_config": "plugin limit-req: rate, burst, key: remote_addr",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/limit-rpm": {
        "funcionalidade": "Limita requisições por minuto por IP",
        "apisix_plugin": "limit-count",
        "apisix_config": "plugin limit-count: count, time_window: 60, key: remote_addr",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/limit-connections": {
        "funcionalidade": "Limita conexões simultâneas por IP",
        "apisix_plugin": "limit-conn",
        "apisix_config": "plugin limit-conn: conn, burst, key: remote_addr",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/limit-burst-multiplier": {
        "funcionalidade": "Multiplicador de burst para rate limiting",
        "apisix_plugin": "limit-req",
        "apisix_config": "plugin limit-req: burst",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/limit-whitelist": {
        "funcionalidade": "CIDRs isentos de rate limiting",
        "apisix_plugin": "limit-req com ip-restriction",
        "apisix_config": "Usar plugin ip-restriction para bypass antes do limit-req",
        "suporte": "parcial",
        "observacoes": "Não há whitelist nativa no limit-req; implementar com ip-restriction ou lua.",
    },

    # -----------------------------------------------------------------------
    # Autenticação
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/auth-type": {
        "funcionalidade": "Tipo de autenticação: basic ou digest",
        "apisix_plugin": "basic-auth",
        "apisix_config": "plugin basic-auth",
        "suporte": "completo",
        "observacoes": "digest-auth não tem plugin nativo no APISIX; basic-auth sim.",
    },
    "nginx.ingress.kubernetes.io/auth-secret": {
        "funcionalidade": "Secret com credenciais de autenticação básica",
        "apisix_plugin": "basic-auth",
        "apisix_config": "Consumer com plugin basic-auth",
        "suporte": "completo",
        "observacoes": "Credenciais devem ser migradas para Consumers no APISIX.",
    },
    "nginx.ingress.kubernetes.io/auth-url": {
        "funcionalidade": "URL externa para autenticação (auth_request)",
        "apisix_plugin": "forward-auth",
        "apisix_config": "plugin forward-auth: uri",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/auth-signin": {
        "funcionalidade": "URL de redirecionamento quando autenticação falha",
        "apisix_plugin": "forward-auth",
        "apisix_config": "plugin forward-auth: request_headers + resposta 401",
        "suporte": "parcial",
        "observacoes": "APISIX forward-auth não redireciona automaticamente; requer lógica adicional.",
    },
    "nginx.ingress.kubernetes.io/auth-response-headers": {
        "funcionalidade": "Headers retornados pelo auth service a serem repassados ao backend",
        "apisix_plugin": "forward-auth",
        "apisix_config": "plugin forward-auth: upstream_headers",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/auth-cache-key": {
        "funcionalidade": "Chave de cache da resposta de autenticação",
        "apisix_plugin": "forward-auth + proxy-cache",
        "apisix_config": "Combinar forward-auth com proxy-cache",
        "suporte": "parcial",
        "observacoes": "Cache de auth não é nativo; requer combinação de plugins.",
    },

    # -----------------------------------------------------------------------
    # CORS
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/enable-cors": {
        "funcionalidade": "Habilita CORS",
        "apisix_plugin": "cors",
        "apisix_config": "plugin cors",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/cors-allow-origin": {
        "funcionalidade": "Origins permitidas para CORS",
        "apisix_plugin": "cors",
        "apisix_config": "plugin cors: allow_origins",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/cors-allow-methods": {
        "funcionalidade": "Métodos HTTP permitidos para CORS",
        "apisix_plugin": "cors",
        "apisix_config": "plugin cors: allow_methods",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/cors-allow-headers": {
        "funcionalidade": "Headers permitidos para CORS",
        "apisix_plugin": "cors",
        "apisix_config": "plugin cors: allow_headers",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/cors-expose-headers": {
        "funcionalidade": "Headers expostos na resposta CORS",
        "apisix_plugin": "cors",
        "apisix_config": "plugin cors: expose_headers",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/cors-allow-credentials": {
        "funcionalidade": "Permite credentials em CORS",
        "apisix_plugin": "cors",
        "apisix_config": "plugin cors: allow_credential: true",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/cors-max-age": {
        "funcionalidade": "Max-age do preflight CORS",
        "apisix_plugin": "cors",
        "apisix_config": "plugin cors: max_age",
        "suporte": "completo",
        "observacoes": "",
    },

    # -----------------------------------------------------------------------
    # Proxy / Upstream
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/proxy-body-size": {
        "funcionalidade": "Tamanho máximo do body da requisição",
        "apisix_plugin": "client-control",
        "apisix_config": "ApisixPluginConfig com plugin client-control: max_body_size (valor em bytes)",
        "suporte": "parcial",
        "observacoes": "Não há annotation equivalente no APISIX IC. Requer criação de ApisixPluginConfig por ingress. Valor precisa conversão: Nm → N*1048576 bytes; '0' = ilimitado.",
    },
    "nginx.ingress.kubernetes.io/proxy-read-timeout": {
        "funcionalidade": "Timeout de leitura do backend",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "upstream: read_timeout",
        "suporte": "completo",
        "observacoes": "Configurar no objeto Upstream do APISIX.",
    },
    "nginx.ingress.kubernetes.io/proxy-send-timeout": {
        "funcionalidade": "Timeout de envio para o backend",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "upstream: send_timeout",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/proxy-connect-timeout": {
        "funcionalidade": "Timeout de conexão com o backend",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "upstream: connect_timeout",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/proxy-buffer-size": {
        "funcionalidade": "Tamanho do buffer de proxy",
        "apisix_plugin": "não mapeado diretamente",
        "apisix_config": "Configurar via nginx.conf customizado (se necessário)",
        "suporte": "sem_suporte",
        "observacoes": "APISIX não expõe proxy_buffer_size como plugin; geralmente não necessário.",
    },
    "nginx.ingress.kubernetes.io/proxy-next-upstream": {
        "funcionalidade": "Condições para tentar próximo upstream",
        "apisix_plugin": "configuração do upstream (retries)",
        "apisix_config": "upstream: retries, retry_timeout",
        "suporte": "parcial",
        "observacoes": "APISIX suporta retries mas não todas as condições do proxy_next_upstream.",
    },
    "nginx.ingress.kubernetes.io/upstream-hash-by": {
        "funcionalidade": "Hash para load balancing (sticky por variável)",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "upstream: hash_on + key (header, cookie, etc.)",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/load-balance": {
        "funcionalidade": "Algoritmo de load balancing",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "upstream: type: roundrobin|chash|ewma|least_conn",
        "suporte": "completo",
        "observacoes": "",
    },

    # -----------------------------------------------------------------------
    # Headers
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/configuration-snippet": {
        "funcionalidade": "Bloco de configuração NGINX customizado por location",
        "apisix_plugin": "serverless-pre-function / serverless-post-function",
        "apisix_config": "plugin serverless-pre-function com código Lua",
        "suporte": "manual",
        "observacoes": "Requer análise caso a caso. Cada snippet deve ser convertido manualmente para Lua ou plugin equivalente.",
    },
    "nginx.ingress.kubernetes.io/server-snippet": {
        "funcionalidade": "Bloco de configuração NGINX customizado no server block",
        "apisix_plugin": "serverless-pre-function",
        "apisix_config": "plugin serverless-pre-function com código Lua",
        "suporte": "manual",
        "observacoes": "Requer análise caso a caso.",
    },
    "nginx.ingress.kubernetes.io/proxy-set-headers": {
        "funcionalidade": "ConfigMap com headers a adicionar nas requisições ao backend",
        "apisix_plugin": "proxy-rewrite",
        "apisix_config": "plugin proxy-rewrite: headers",
        "suporte": "completo",
        "observacoes": "Headers devem ser mapeados explicitamente no plugin.",
    },
    "nginx.ingress.kubernetes.io/add-base-url": {
        "funcionalidade": "Adiciona base URL ao HTML retornado",
        "apisix_plugin": "response-rewrite",
        "apisix_config": "plugin response-rewrite: body",
        "suporte": "manual",
        "observacoes": "Requer regex no response-rewrite.",
    },

    # -----------------------------------------------------------------------
    # Canary / Traffic Splitting
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/canary": {
        "funcionalidade": "Habilita roteamento canary",
        "apisix_plugin": "traffic-split",
        "apisix_config": "plugin traffic-split",
        "suporte": "completo",
        "observacoes": "APISIX traffic-split é mais poderoso; suporta múltiplos upstreams.",
    },
    "nginx.ingress.kubernetes.io/canary-weight": {
        "funcionalidade": "Peso percentual do tráfego para o canary",
        "apisix_plugin": "traffic-split",
        "apisix_config": "plugin traffic-split: rules[].weighted_upstreams[].weight",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/canary-by-header": {
        "funcionalidade": "Roteia para canary baseado em header",
        "apisix_plugin": "traffic-split",
        "apisix_config": "plugin traffic-split: rules[].match com header condition",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/canary-by-cookie": {
        "funcionalidade": "Roteia para canary baseado em cookie",
        "apisix_plugin": "traffic-split",
        "apisix_config": "plugin traffic-split: rules[].match com cookie condition",
        "suporte": "completo",
        "observacoes": "",
    },

    # -----------------------------------------------------------------------
    # IP / Whitelist / Blacklist
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/whitelist-source-range": {
        "funcionalidade": "CIDRs permitidos (whitelist de IP)",
        "apisix_plugin": "ip-restriction",
        "apisix_config": "plugin ip-restriction: whitelist",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/denylist-source-range": {
        "funcionalidade": "CIDRs bloqueados (blacklist de IP)",
        "apisix_plugin": "ip-restriction",
        "apisix_config": "plugin ip-restriction: blacklist",
        "suporte": "completo",
        "observacoes": "",
    },

    # -----------------------------------------------------------------------
    # Session / Affinity
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/affinity": {
        "funcionalidade": "Session affinity (cookie-based sticky sessions)",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "upstream: hash_on: cookie, key: <cookie_name>",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/session-cookie-name": {
        "funcionalidade": "Nome do cookie de session affinity",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "upstream: hash_on: cookie, key: <nome>",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/session-cookie-path": {
        "funcionalidade": "Path do cookie de session affinity",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "Configurar via set-cookie no response",
        "suporte": "parcial",
        "observacoes": "",
    },

    # -----------------------------------------------------------------------
    # Misc
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/service-upstream": {
        "funcionalidade": "Usa o ClusterIP do Service em vez de IPs dos pods",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "Upstream apontando para ClusterIP do Service",
        "suporte": "completo",
        "observacoes": "No APISIX via Ingress controller, o discovery cuida disso.",
    },
    "nginx.ingress.kubernetes.io/server-alias": {
        "funcionalidade": "Aliases de host para o server block",
        "apisix_plugin": "rota APISIX com múltiplos hosts",
        "apisix_config": "Criar rota com array de hosts",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/permanent-redirect": {
        "funcionalidade": "Redirect 301 permanente para URL",
        "apisix_plugin": "redirect",
        "apisix_config": "plugin redirect: uri, ret_code: 301",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/temporal-redirect": {
        "funcionalidade": "Redirect 302 temporário para URL",
        "apisix_plugin": "redirect",
        "apisix_config": "plugin redirect: uri, ret_code: 302",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/custom-http-errors": {
        "funcionalidade": "Redireciona erros HTTP para URL de erro customizada",
        "apisix_plugin": "response-rewrite / serverless",
        "apisix_config": "plugin serverless-post-function para interceptar status codes",
        "suporte": "manual",
        "observacoes": "Não há equivalente direto; requer lógica Lua.",
    },
    "nginx.ingress.kubernetes.io/enable-access-log": {
        "funcionalidade": "Habilita/desabilita access log por rota",
        "apisix_plugin": "configuração global / plugin http-logger",
        "apisix_config": "Plugin http-logger ou desabilitar via configuração",
        "suporte": "parcial",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/from-to-www-redirect": {
        "funcionalidade": "Redireciona www para non-www ou vice-versa",
        "apisix_plugin": "redirect",
        "apisix_config": "plugin redirect com regex",
        "suporte": "completo",
        "observacoes": "",
    },

    # -----------------------------------------------------------------------
    # Proxy / Upstream — complementos
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/upstream-vhost": {
        "funcionalidade": "Define o header Host enviado ao upstream (sobrescreve o host original)",
        "apisix_plugin": "proxy-rewrite",
        "apisix_config": "plugin proxy-rewrite: headers.host: <valor>",
        "suporte": "completo",
        "observacoes": "Usado para backends S3, serviços internos ou proxies que exigem Host específico.",
    },
    "nginx.ingress.kubernetes.io/proxy-buffering": {
        "funcionalidade": "Habilita ou desabilita o buffer de resposta do upstream (on/off)",
        "apisix_plugin": "sem equivalente direto",
        "apisix_config": "Configurar via nginx variables no APISIX se necessário",
        "suporte": "sem_suporte",
        "observacoes": "Relevante para streaming. APISIX herda comportamento padrão do nginx (on). Avaliar se o serviço precisa de streaming antes de migrar.",
    },
    "nginx.ingress.kubernetes.io/proxy-buffers-number": {
        "funcionalidade": "Número de buffers usados para leitura da resposta do upstream",
        "apisix_plugin": "sem equivalente direto",
        "apisix_config": "Não exposto como plugin; usar configuração nginx customizada se crítico",
        "suporte": "sem_suporte",
        "observacoes": "Tunning de baixo nível. Na maioria dos casos o padrão do APISIX é suficiente.",
    },
    "nginx.ingress.kubernetes.io/proxy-max-temp-file-size": {
        "funcionalidade": "Tamanho máximo do arquivo temporário quando o buffer está cheio",
        "apisix_plugin": "sem equivalente direto",
        "apisix_config": "Não exposto como plugin",
        "suporte": "sem_suporte",
        "observacoes": "Tunning de baixo nível. Avaliar se é realmente necessário.",
    },
    "nginx.ingress.kubernetes.io/proxy-next-upstream-tries": {
        "funcionalidade": "Número máximo de tentativas ao próximo upstream em caso de falha",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "upstream: retries: <valor>",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/proxy-hide-headers": {
        "funcionalidade": "Lista de headers da resposta do upstream a remover antes de enviar ao cliente",
        "apisix_plugin": "response-rewrite",
        "apisix_config": "plugin response-rewrite: headers (setar header como empty string para remover)",
        "suporte": "parcial",
        "observacoes": "APISIX response-rewrite permite remover headers setando valor vazio.",
    },
    "nginx.ingress.kubernetes.io/proxy-intercept-errors": {
        "funcionalidade": "Intercepta respostas de erro do upstream para repassar ao custom-http-errors",
        "apisix_plugin": "serverless-post-function",
        "apisix_config": "plugin serverless-post-function para interceptar status codes de erro",
        "suporte": "manual",
        "observacoes": "Funciona em conjunto com custom-http-errors. Requer lógica Lua.",
    },
    "nginx.ingress.kubernetes.io/send_timeout": {
        "funcionalidade": "Timeout de envio para o cliente (nota: underscore em vez de hífen — possível typo no cluster)",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "upstream: send_timeout",
        "suporte": "completo",
        "observacoes": "Annotation com underscore provavelmente não tem efeito no NGINX. Verificar se é typo de proxy-send-timeout.",
    },

    # -----------------------------------------------------------------------
    # Keepalive
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/keepalive": {
        "funcionalidade": "Número máximo de conexões keepalive ao upstream",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "upstream: keepalive: <valor>",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/keepalive-timeout": {
        "funcionalidade": "Timeout das conexões keepalive ao upstream (segundos)",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "upstream: keepalive_timeout: <valor>",
        "suporte": "completo",
        "observacoes": "",
    },

    # -----------------------------------------------------------------------
    # SSL
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/ssl-protocols": {
        "funcionalidade": "Protocolos TLS aceitos (ex: TLSv1.2 TLSv1.3)",
        "apisix_plugin": "configuração global de SSL do APISIX",
        "apisix_config": "apisix.ssl.ssl_protocols no config.yaml do APISIX",
        "suporte": "parcial",
        "observacoes": "Não é configurável por rota no APISIX — é configuração global. Verificar se o padrão (TLSv1.2, TLSv1.3) atende.",
    },

    # -----------------------------------------------------------------------
    # Autenticação — complementos
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/auth-realm": {
        "funcionalidade": "Realm exibido no popup de autenticação básica do browser",
        "apisix_plugin": "basic-auth",
        "apisix_config": "plugin basic-auth: config.realm: <valor>",
        "suporte": "completo",
        "observacoes": "APISIX 3.14.1 suporta o campo realm nativamente no plugin basic-auth (default: 'basic'). Migração direta.",
    },

    # -----------------------------------------------------------------------
    # CORS — complementos
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/cors-allow-Origin": {
        "funcionalidade": "Origins permitidas para CORS (typo: 'O' maiúsculo — provavelmente inativo no cluster)",
        "apisix_plugin": "cors",
        "apisix_config": "plugin cors: allow_origins",
        "suporte": "completo",
        "observacoes": "Atenção: esta annotation tem 'O' maiúsculo em 'Origin', o que é um typo — o NGINX provavelmente ignora ela. Usar nginx.ingress.kubernetes.io/cors-allow-origin (minúsculo) no APISIX.",
    },

    # -----------------------------------------------------------------------
    # Forwarded Headers / IP real do cliente
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/compute-full-forwarded-for": {
        "funcionalidade": "Acumula todos os IPs no header X-Forwarded-For (não sobrescreve)",
        "apisix_plugin": "real-ip / proxy-rewrite",
        "apisix_config": "plugin real-ip: recursive: true",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/use-forwarded-headers": {
        "funcionalidade": "Usa os headers X-Forwarded-* recebidos em vez de sobrescrevê-los",
        "apisix_plugin": "real-ip",
        "apisix_config": "plugin real-ip: trusted_addresses com CIDRs dos proxies upstream",
        "suporte": "completo",
        "observacoes": "",
    },

    # -----------------------------------------------------------------------
    # Session / Affinity — complementos
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/session-cookie-expires": {
        "funcionalidade": "Tempo de expiração do cookie de session affinity (segundos)",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "Não há campo de expiração de cookie no upstream do APISIX",
        "suporte": "sem_suporte",
        "observacoes": "APISIX não gerencia expiração do cookie de sticky session. O cookie persiste enquanto o browser estiver aberto.",
    },
    "nginx.ingress.kubernetes.io/session-cookie-max-age": {
        "funcionalidade": "Max-age do cookie de session affinity (segundos)",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "Não há campo de max-age de cookie no upstream do APISIX",
        "suporte": "sem_suporte",
        "observacoes": "Mesmo caso do session-cookie-expires.",
    },

    # -----------------------------------------------------------------------
    # Misc — complementos
    # -----------------------------------------------------------------------
    "nginx.ingress.kubernetes.io/default-backend": {
        "funcionalidade": "Service de fallback quando nenhuma regra do ingress bate",
        "apisix_plugin": "configuração da rota APISIX",
        "apisix_config": "Criar rota catch-all com upstream apontando para o service de fallback",
        "suporte": "manual",
        "observacoes": "Criar uma rota separada no APISIX com prioridade baixa para capturar requisições não roteadas.",
    },
    "nginx.ingress.kubernetes.io/priority": {
        "funcionalidade": "Prioridade da rota quando múltiplos ingresses têm regras conflitantes",
        "apisix_plugin": "configuração da rota APISIX",
        "apisix_config": "Route: priority: <valor>",
        "suporte": "completo",
        "observacoes": "APISIX tem campo priority nativo nas rotas.",
    },
    "nginx.ingress.kubernetes.io/health-check-path": {
        "funcionalidade": "Path de health check do upstream",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "upstream: checks.active.http_path: <path>",
        "suporte": "completo",
        "observacoes": "APISIX suporta health checks ativos e passivos no upstream.",
    },
    "nginx.ingress.kubernetes.io/mirror-target": {
        "funcionalidade": "URL para espelhar (mirror) o tráfego — útil para testes em paralelo",
        "apisix_plugin": "proxy-mirror",
        "apisix_config": "plugin proxy-mirror: host: <url>",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/mirror-request-body": {
        "funcionalidade": "Habilita/desabilita espelhamento do body da requisição",
        "apisix_plugin": "proxy-mirror",
        "apisix_config": "plugin proxy-mirror — espelha body por padrão",
        "suporte": "completo",
        "observacoes": "",
    },
    "nginx.ingress.kubernetes.io/reload-ts": {
        "funcionalidade": "Timestamp interno usado pelo NGINX controller para forçar reload de configuração",
        "apisix_plugin": "não aplicável",
        "apisix_config": "não aplicável",
        "suporte": "sem_suporte",
        "observacoes": "Annotation interna do NGINX Ingress Controller. Não tem função de roteamento e não precisa ser migrada.",
    },
}

# ---------------------------------------------------------------------------
# Mapeamento Kong annotation -> equivalente APISIX
# ---------------------------------------------------------------------------

KONG_TO_APISIX: dict[str, dict] = {
    "konghq.com/strip-path": {
        "funcionalidade": "Remove o path do Ingress antes de encaminhar ao upstream",
        "apisix_plugin": "proxy-rewrite",
        "apisix_config": "plugin proxy-rewrite: regex_uri para remover prefixo do path",
        "suporte": "completo",
        "observacoes": "true = remove o path; false = mantém. Equivalente a rewrite-target no NGINX.",
    },
    "konghq.com/preserve-host": {
        "funcionalidade": "Preserva o header Host original na requisição ao upstream",
        "apisix_plugin": "proxy-rewrite",
        "apisix_config": "plugin proxy-rewrite: headers.host = $http_host",
        "suporte": "completo",
        "observacoes": "",
    },
    "konghq.com/protocols": {
        "funcionalidade": "Protocolos aceitos pela rota: http, https, grpc, grpcs",
        "apisix_plugin": "configuração da rota APISIX",
        "apisix_config": "Route: protocols array (http, https, grpc, grpcs)",
        "suporte": "completo",
        "observacoes": "APISIX suporta os mesmos protocolos nativamente.",
    },
    "konghq.com/connect-timeout": {
        "funcionalidade": "Timeout de conexão com o upstream (ms)",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "upstream: connect_timeout (converte ms → segundos)",
        "suporte": "completo",
        "observacoes": "Kong usa milissegundos; APISIX usa milissegundos também.",
    },
    "konghq.com/read-timeout": {
        "funcionalidade": "Timeout de leitura da resposta do upstream (ms)",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "upstream: read_timeout",
        "suporte": "completo",
        "observacoes": "",
    },
    "konghq.com/write-timeout": {
        "funcionalidade": "Timeout de envio da requisição ao upstream (ms)",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "upstream: send_timeout",
        "suporte": "completo",
        "observacoes": "",
    },
    "konghq.com/retries": {
        "funcionalidade": "Número de tentativas em caso de falha no upstream",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "upstream: retries",
        "suporte": "completo",
        "observacoes": "",
    },
    "konghq.com/https-redirect-status-code": {
        "funcionalidade": "Status code do redirect HTTP → HTTPS (301 ou 426)",
        "apisix_plugin": "redirect",
        "apisix_config": "plugin redirect: http_to_https: true, ret_code: 301|302",
        "suporte": "completo",
        "observacoes": "APISIX não suporta 426 nativo; usar 301 ou 302.",
    },
    "konghq.com/path-handling": {
        "funcionalidade": "Como o path é composto ao encaminhar (v0 ou v1)",
        "apisix_plugin": "proxy-rewrite",
        "apisix_config": "plugin proxy-rewrite: uri para controlar composição do path",
        "suporte": "parcial",
        "observacoes": "v0 e v1 diferem em como concatenam path da rota + path da requisição. Requer análise por caso.",
    },
    "konghq.com/request-buffering": {
        "funcionalidade": "Habilita/desabilita buffer do body da requisição",
        "apisix_plugin": "client-control",
        "apisix_config": "Configuração de buffer via nginx variables",
        "suporte": "parcial",
        "observacoes": "Relevante para uploads grandes. APISIX herda comportamento do nginx.",
    },
    "konghq.com/response-buffering": {
        "funcionalidade": "Habilita/desabilita buffer da resposta do upstream",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "Configuração de proxy_buffering via nginx variables",
        "suporte": "parcial",
        "observacoes": "Relevante para streaming. APISIX herda comportamento do nginx.",
    },
    "konghq.com/regex-priority": {
        "funcionalidade": "Prioridade da rota quando múltiplas rotas usam regex",
        "apisix_plugin": "configuração da rota APISIX",
        "apisix_config": "Route: priority (inteiro — maior valor = maior prioridade)",
        "suporte": "completo",
        "observacoes": "APISIX tem campo priority nativo nas rotas.",
    },
    "konghq.com/tags": {
        "funcionalidade": "Tags para organização/filtragem de rotas no Kong",
        "apisix_plugin": "labels na rota APISIX",
        "apisix_config": "Route: labels (key-value map)",
        "suporte": "completo",
        "observacoes": "APISIX usa labels em vez de tags.",
    },
    "konghq.com/plugins": {
        "funcionalidade": "Lista de KongPlugin resources a aplicar nesta rota",
        "apisix_plugin": "plugins na rota APISIX",
        "apisix_config": "Route: plugins array ou ApisixPluginConfig",
        "suporte": "manual",
        "observacoes": "Cada KongPlugin referenciado deve ser analisado individualmente e convertido para o plugin APISIX equivalente.",
    },
    "konghq.com/grpc": {
        "funcionalidade": "Habilita suporte a gRPC nesta rota",
        "apisix_plugin": "configuração da rota APISIX",
        "apisix_config": "Route: protocols: [grpc, grpcs]",
        "suporte": "completo",
        "observacoes": "",
    },
    "konghq.com/methods": {
        "funcionalidade": "Métodos HTTP aceitos pela rota",
        "apisix_plugin": "configuração da rota APISIX",
        "apisix_config": "Route: methods array",
        "suporte": "completo",
        "observacoes": "",
    },
    "ingress.kubernetes.io/service-upstream": {
        "funcionalidade": "Usa o ClusterIP do Service em vez dos IPs dos pods",
        "apisix_plugin": "configuração do upstream",
        "apisix_config": "Upstream apontando para ClusterIP — comportamento padrão no APISIX via Ingress Controller",
        "suporte": "completo",
        "observacoes": "O APISIX Ingress Controller usa service discovery; este comportamento é o padrão.",
    },

    # -----------------------------------------------------------------------
    # Routing — complementos Kong
    # -----------------------------------------------------------------------
    "ingress.kubernetes.io/rewrite-target": {
        "funcionalidade": "Reescreve o path antes de encaminhar ao upstream",
        "apisix_plugin": "proxy-rewrite",
        "apisix_config": "plugin proxy-rewrite: regex_uri ou uri",
        "suporte": "completo",
        "observacoes": "Equivalente ao nginx.ingress.kubernetes.io/rewrite-target.",
    },
    "ingress.kubernetes.io/use-regex": {
        "funcionalidade": "Habilita regex no path do Ingress",
        "apisix_plugin": "configuração da rota APISIX",
        "apisix_config": "Rota APISIX suporta regex nativamente no campo uri",
        "suporte": "completo",
        "observacoes": "",
    },
    "ingress.kubernetes.io/whitelist-source-range": {
        "funcionalidade": "CIDRs permitidos (whitelist de IP)",
        "apisix_plugin": "ip-restriction",
        "apisix_config": "plugin ip-restriction: whitelist",
        "suporte": "completo",
        "observacoes": "Equivalente ao nginx.ingress.kubernetes.io/whitelist-source-range.",
    },

    # -----------------------------------------------------------------------
    # GKE Load Balancer — annotations geradas automaticamente pelo Google
    # Não precisam ser migradas: são metadados de infraestrutura do GKE LB,
    # não configuram comportamento de roteamento.
    # -----------------------------------------------------------------------
    "ingress.kubernetes.io/backends": {
        "funcionalidade": "Metadado do GKE Load Balancer — backends registrados automaticamente pelo Google",
        "apisix_plugin": "não aplicável",
        "apisix_config": "não aplicável",
        "suporte": "sem_suporte",
        "observacoes": "Annotation gerada automaticamente pelo GKE. Não tem função de roteamento e não precisa ser migrada.",
    },
    "ingress.kubernetes.io/forwarding-rule": {
        "funcionalidade": "Metadado do GKE Load Balancer — forwarding rule do Google Cloud",
        "apisix_plugin": "não aplicável",
        "apisix_config": "não aplicável",
        "suporte": "sem_suporte",
        "observacoes": "Annotation gerada automaticamente pelo GKE. Não precisa ser migrada.",
    },
    "ingress.kubernetes.io/https-forwarding-rule": {
        "funcionalidade": "Metadado do GKE Load Balancer — forwarding rule HTTPS do Google Cloud",
        "apisix_plugin": "não aplicável",
        "apisix_config": "não aplicável",
        "suporte": "sem_suporte",
        "observacoes": "Annotation gerada automaticamente pelo GKE. Não precisa ser migrada.",
    },
    "ingress.kubernetes.io/https-target-proxy": {
        "funcionalidade": "Metadado do GKE Load Balancer — HTTPS target proxy do Google Cloud",
        "apisix_plugin": "não aplicável",
        "apisix_config": "não aplicável",
        "suporte": "sem_suporte",
        "observacoes": "Annotation gerada automaticamente pelo GKE. Não precisa ser migrada.",
    },
    "ingress.kubernetes.io/ssl-cert": {
        "funcionalidade": "Metadado do GKE Load Balancer — certificado SSL gerenciado pelo Google",
        "apisix_plugin": "não aplicável",
        "apisix_config": "não aplicável",
        "suporte": "sem_suporte",
        "observacoes": "Annotation gerada automaticamente pelo GKE. TLS no APISIX é configurado via Secret do Kubernetes.",
    },
    "ingress.kubernetes.io/static-ip": {
        "funcionalidade": "Metadado do GKE Load Balancer — IP estático reservado no Google Cloud",
        "apisix_plugin": "não aplicável",
        "apisix_config": "não aplicável",
        "suporte": "sem_suporte",
        "observacoes": "Annotation gerada automaticamente pelo GKE. IP do APISIX é gerenciado pelo Service do tipo LoadBalancer.",
    },
    "ingress.kubernetes.io/target-proxy": {
        "funcionalidade": "Metadado do GKE Load Balancer — HTTP target proxy do Google Cloud",
        "apisix_plugin": "não aplicável",
        "apisix_config": "não aplicável",
        "suporte": "sem_suporte",
        "observacoes": "Annotation gerada automaticamente pelo GKE. Não precisa ser migrada.",
    },
    "ingress.kubernetes.io/url-map": {
        "funcionalidade": "Metadado do GKE Load Balancer — URL map do Google Cloud",
        "apisix_plugin": "não aplicável",
        "apisix_config": "não aplicável",
        "suporte": "sem_suporte",
        "observacoes": "Annotation gerada automaticamente pelo GKE. Não precisa ser migrada.",
    },
}

# ---------------------------------------------------------------------------
# Nível de suporte -> cor/label para o CSV
# ---------------------------------------------------------------------------
SUPORTE_LABEL = {
    "completo": "Completo",
    "parcial": "Parcial - requer ajuste",
    "manual": "Manual - análise caso a caso",
    "sem_suporte": "Sem suporte nativo",
}


def write_depara_csv(annotation_usage: dict, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / "depara_nginx_apisix.csv"

    # Separa annotations por controller
    nginx_found = {k: v for k, v in annotation_usage.items()
                   if k.startswith("nginx.ingress.kubernetes.io/")
                   or k.startswith("nginx.org/")}
    kong_found = {k: v for k, v in annotation_usage.items()
                  if k.startswith("konghq.com/")
                  or k.startswith("ingress.kubernetes.io/")}

    nginx_not_mapped = {k for k in nginx_found if k not in NGINX_TO_APISIX}
    kong_not_mapped = {k for k in kong_found if k not in KONG_TO_APISIX}

    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "source_controller",
            "annotation_original",
            "uso_nos_clusters",
            "funcionalidade",
            "apisix_plugin",
            "apisix_config",
            "suporte",
            "observacoes",
            "valores_encontrados",
        ])

        def write_section(found: dict, mapping_db: dict, controller: str):
            for key in sorted(found.keys(), key=lambda k: -len(found.get(k, []))):
                usage = found.get(key, [])
                mapping = mapping_db.get(key, {})
                # Valores: trunca longos, remove quebras de linha, limita a 5
                raw_values = sorted(set(v for _, v in usage))
                clean_values = [v.replace("\n", " ").replace("\r", "")[:60]
                                for v in raw_values[:5]]
                writer.writerow([
                    controller,
                    key,
                    len(usage),
                    mapping.get("funcionalidade", "A PREENCHER"),
                    mapping.get("apisix_plugin", "A PREENCHER"),
                    mapping.get("apisix_config", "A PREENCHER"),
                    SUPORTE_LABEL.get(mapping.get("suporte", ""), "A PREENCHER"),
                    mapping.get("observacoes", ""),
                    " | ".join(clean_values),
                ])

        # NGINX e Kong juntos, ordenados por uso (sem linha em branco separando)
        all_found = {**nginx_found, **kong_found}
        for key in sorted(all_found.keys(), key=lambda k: -len(all_found[k])):
            if key in nginx_found:
                write_section({key: nginx_found[key]}, NGINX_TO_APISIX, "nginx")
            else:
                write_section({key: kong_found[key]}, KONG_TO_APISIX, "kong")

    def _report(label, found, mapping_db, not_mapped):
        mapped = len([k for k in found if k in mapping_db])
        print(f"  {label}: {len(found)} encontradas, {mapped} mapeadas, {len(not_mapped)} a revisar")
        if not_mapped:
            for k in sorted(not_mapped):
                print(f"    [{len(found[k]):3d}x] {k}")

    print(f"Salvo: {out}")
    _report("NGINX", nginx_found, NGINX_TO_APISIX, nginx_not_mapped)
    _report("Kong ", kong_found, KONG_TO_APISIX, kong_not_mapped)
    return out


def write_depara_json(annotation_usage: dict, output_dir: Path):
    nginx_found = {k: v for k, v in annotation_usage.items()
                   if k.startswith("nginx.ingress.kubernetes.io/")
                   or k.startswith("nginx.org/")}

    result = {}
    for key, entries in sorted(nginx_found.items(), key=lambda x: -len(x[1])):
        values = sorted(set(v for _, v in entries))
        mapping = NGINX_TO_APISIX.get(key, {})
        result[key] = {
            "uso_nos_clusters": len(entries),
            "valores_encontrados": values[:20],
            **mapping,
            "mapeado": key in NGINX_TO_APISIX,
        }

    out = output_dir / "depara_nginx_apisix.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"Salvo: {out}")
    return out


RAW_DIR = Path(__file__).parent / "data" / "raw"

NGINX_PREFIXES = ("nginx.ingress.kubernetes.io/", "nginx.org/")
KONG_PREFIXES = ("konghq.com/", "ingress.kubernetes.io/")


def load_raw_usage(raw_dir: Path) -> dict[str, list[tuple[str, str, str, str]]]:
    """
    Lê os JSONs brutos e retorna:
      annotation_key -> [(cluster, namespace, ingress_name, value), ...]
    """
    usage: dict[str, list] = {}
    for path in sorted(raw_dir.glob("*.json")):
        cluster = path.stem
        data = json.loads(path.read_text())
        items = data if isinstance(data, list) else data.get("items", [])
        for ing in items:
            if not isinstance(ing, dict):
                continue
            ns = ing.get("metadata", {}).get("namespace", "")
            name = ing.get("metadata", {}).get("name", "")
            annotations = ing.get("metadata", {}).get("annotations", {}) or {}
            for key, value in annotations.items():
                if not (any(key.startswith(p) for p in NGINX_PREFIXES)
                        or any(key.startswith(p) for p in KONG_PREFIXES)):
                    continue
                usage.setdefault(key, []).append((cluster, ns, name, value))
    return usage


def write_detailed_csv(raw_usage: dict, output_dir: Path):
    """
    Gera depara_detalhado.csv: uma linha por (annotation, cluster, ingress),
    com as colunas de mapeamento APISIX agregadas.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / "depara_detalhado.csv"

    all_mappings = {**NGINX_TO_APISIX, **KONG_TO_APISIX}

    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "source_controller",
            "annotation",
            "cluster",
            "namespace",
            "ingress",
            "valor",
            "funcionalidade",
            "apisix_plugin",
            "apisix_config",
            "suporte",
            "observacoes",
            "jira_card",
        ])
        for key in sorted(raw_usage.keys(), key=lambda k: -len(raw_usage[k])):
            entries = raw_usage[key]
            mapping = all_mappings.get(key, {})
            if any(key.startswith(p) for p in NGINX_PREFIXES):
                controller = "nginx"
            else:
                controller = "kong"
            suporte = SUPORTE_LABEL.get(mapping.get("suporte", ""), "A PREENCHER")
            for cluster, ns, name, value in entries:
                clean_value = value.replace("\n", " ").replace("\r", "")[:120]
                writer.writerow([
                    controller,
                    key,
                    cluster,
                    ns,
                    name,
                    clean_value,
                    mapping.get("funcionalidade", "A PREENCHER"),
                    mapping.get("apisix_plugin", "A PREENCHER"),
                    mapping.get("apisix_config", "A PREENCHER"),
                    suporte,
                    mapping.get("observacoes", ""),
                    "",  # jira_card — preenchida durante análise dos tickets
                ])

    total = sum(len(v) for v in raw_usage.values())
    print(f"Salvo: {out}  ({len(raw_usage)} annotations, {total} linhas)")
    return out


def main():
    parser = argparse.ArgumentParser(description="Gera depara NGINX Ingress -> APISIX")
    parser.add_argument("--features", default=str(FEATURES_REPORT),
                        help="Caminho para features_report.json")
    parser.add_argument("--raw-dir", default=str(RAW_DIR),
                        help="Diretório com os JSONs brutos (para depara_detalhado.csv)")
    args = parser.parse_args()

    features_path = Path(args.features)
    if not features_path.exists():
        print(f"[ERRO] {features_path} não encontrado.")
        print("Execute analyze.py primeiro.")
        return

    report = json.loads(features_path.read_text())

    # Constrói usage com contagem real de ingresses (lendo dos JSONs brutos)
    raw_dir = Path(args.raw_dir)
    raw_usage = load_raw_usage(raw_dir)

    # Para compatibilidade com write_depara_csv/json: (ingress_id, value)
    full_usage: dict[str, list[tuple[str, str]]] = {
        key: [(f"{ns}/{name}", val) for _, ns, name, val in entries]
        for key, entries in raw_usage.items()
    }

    write_depara_csv(full_usage, OUTPUT_DIR)
    write_depara_json(full_usage, OUTPUT_DIR)
    write_detailed_csv(raw_usage, OUTPUT_DIR)


if __name__ == "__main__":
    main()
