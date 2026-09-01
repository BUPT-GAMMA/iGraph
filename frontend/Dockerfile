ARG NODE_IMAGE=node:22-alpine
ARG NGINX_IMAGE=nginx:1.27-alpine
ARG PNPM_VERSION=10.28.1
FROM ${NODE_IMAGE} AS build
ARG PNPM_VERSION
WORKDIR /app
RUN corepack enable \
    && corepack prepare pnpm@${PNPM_VERSION} --activate
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/tsconfig.json frontend/tsconfig.node.json frontend/vite.config.ts ./
COPY frontend/index.html ./index.html
COPY frontend/src ./src
COPY frontend/public/figures ./public/figures
RUN pnpm build

FROM ${NGINX_IMAGE}

COPY --from=build /app/dist /usr/share/nginx/html
COPY frontend/nginx.conf.template /etc/nginx/templates/default.conf.template

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=12 \
  CMD wget -q -O - http://127.0.0.1/health >/dev/null || exit 1
