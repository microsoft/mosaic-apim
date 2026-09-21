FROM node:24-alpine AS builder
WORKDIR /workspace/apps/portal
COPY apps/portal/package.json apps/portal/package-lock.json ./
RUN npm ci
COPY apps/portal/ ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.29-alpine AS runtime
COPY --from=builder --chown=101:101 /workspace/apps/portal/dist /usr/share/nginx/html
COPY apps/portal/nginx.conf /etc/nginx/conf.d/default.conf
COPY apps/portal/config.template.js /opt/mosaic/config.template.js
COPY apps/portal/40-runtime-config.sh /docker-entrypoint.d/40-runtime-config.sh
USER root
# The base image's html directory is owned by root, and COPY --chown only sets ownership on the
# entries it copies, not on a destination that already exists. The entrypoint writes config.js
# there at start-up as uid 101, so the directory itself has to be handed over explicitly.
RUN sed -i 's/\r$//' /docker-entrypoint.d/40-runtime-config.sh \
    && chmod +x /docker-entrypoint.d/40-runtime-config.sh \
    && chown 101:101 /usr/share/nginx/html
USER 101
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -q -O - http://127.0.0.1:8080/healthz || exit 1
