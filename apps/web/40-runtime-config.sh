#!/bin/sh
set -eu

envsubst \
  '${MOSAIC_API_BASE_URL} ${MOSAIC_ENTRA_TENANT_ID} ${MOSAIC_ENTRA_CLIENT_ID} ${MOSAIC_ENTRA_API_SCOPE} ${MOSAIC_APPLICATIONINSIGHTS_CONNECTION_STRING}' \
  < /opt/mosaic/config.template.js \
  > /usr/share/nginx/html/config.js
