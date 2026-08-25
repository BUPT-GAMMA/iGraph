ARG NGINX_IMAGE=nginx:1.27-alpine
FROM ${NGINX_IMAGE}

COPY index.html knowledge_graph_v2.html /usr/share/nginx/html/
COPY figures /usr/share/nginx/html/figures
COPY frontend/nginx.conf.template /etc/nginx/templates/default.conf.template

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=12 \
  CMD wget -q -O - http://127.0.0.1/health >/dev/null || exit 1
