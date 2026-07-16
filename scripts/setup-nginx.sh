#!/bin/sh
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — One-time nginx configuration setup
# ──────────────────────────────────────────────────────────────────────────────
# Run this ONCE on the VPS after the first deployment (or after a fresh server
# rebuild).  CI never touches nginx — this script is purely manual.
#
# Usage:
#   sudo ./scripts/setup-nginx.sh
#
# What it does:
#   1. Copies the default HTTP template → conf.d/openzync.conf
#   2. Ensures conf.d/ directory exists with correct permissions
#   3. Does NOT enable SSL (requires Cloudflare Origin CA certs)
#   4. Does NOT restart the nginx container
# ──────────────────────────────────────────────────────────────────────────────
set -e

NGINX_CONF_DIR="$(dirname "$0")/../infra/nginx/conf.d"
TEMPLATES_DIR="$(dirname "$0")/../infra/nginx/templates"

# Ensure conf.d/ exists
mkdir -p "${NGINX_CONF_DIR}"

# Copy the default HTTP config if not already present
if [ ! -f "${NGINX_CONF_DIR}/openzync.conf" ]; then
    cp "${TEMPLATES_DIR}/openzync.conf" "${NGINX_CONF_DIR}/openzync.conf"
    echo "Created ${NGINX_CONF_DIR}/openzync.conf"
else
    echo "SKIP: ${NGINX_CONF_DIR}/openzync.conf already exists"
fi

echo ""
echo "─── nginx setup complete ───"
echo ""
echo "If you just created the config file for the first time, restart nginx:"
echo "  docker restart infra-nginx-1"
echo ""
echo "To enable SSL (requires Cloudflare Origin CA certs in /etc/nginx/certs/):"
echo "  cp ${TEMPLATES_DIR}/openzync.ssl.conf ${NGINX_CONF_DIR}/"
echo "  docker restart infra-nginx-1"
