ARG NODE_IMAGE=node:22-alpine
ARG NGINX_IMAGE=nginx:1.27-alpine
FROM ${NODE_IMAGE} AS build
WORKDIR /app
COPY frontend/package.json frontend/tsconfig.json frontend/tsconfig.node.json frontend/vite.config.ts ./
RUN npm install
COPY frontend/index.html ./index.html
COPY frontend/src ./src
COPY frontend/public/figures ./public/figures
RUN npm run build

FROM ${NGINX_IMAGE}

COPY --from=build /app/dist /usr/share/nginx/html
COPY frontend/nginx.conf.template /etc/nginx/templates/default.conf.template

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=12 \
  CMD wget -q -O - http://127.0.0.1/health >/dev/null || exit 1
