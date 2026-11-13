#!/bin/sh
set -eu

# Keep malformed portal configuration as data, not executable JavaScript.
MOSAIC_PORTAL_URL="$(
  awk 'BEGIN {
    backslash = sprintf("%c", 92)
    quote = sprintf("%c", 39)
    for (i = 1; i < 32; i++) controls[sprintf("%c", i)] = backslash sprintf("u%04x", i)
    value = ENVIRON["MOSAIC_PORTAL_URL"]
    for (i = 1; i <= length(value); i++) {
      character = substr(value, i, 1)
      if (character == backslash || character == quote) printf "%s%s", backslash, character
      else if (character in controls) printf "%s", controls[character]
      else printf "%s", character
    }
  }'
)"
export MOSAIC_PORTAL_URL

envsubst \
  '${MOSAIC_API_BASE_URL} ${MOSAIC_PORTAL_URL} ${MOSAIC_ENTRA_TENANT_ID} ${MOSAIC_ENTRA_CLIENT_ID} ${MOSAIC_ENTRA_API_SCOPE} ${MOSAIC_APPLICATIONINSIGHTS_CONNECTION_STRING}' \
  < /opt/mosaic/config.template.js \
  > /usr/share/nginx/html/config.js
