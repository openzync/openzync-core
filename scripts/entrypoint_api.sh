#!/bin/sh
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — API Service Container Entrypoint
# ──────────────────────────────────────────────────────────────────────────────
# Runs in the `api` container as PID 1. All config arrives via the
# environment (compose env_file: ../.env plus direct OZ_* entries) — no
# sidecars, no rendered secret files. This script fail-fasts on the two
# keys the app cannot boot without, logs a redacted summary, then execs
# uvicorn so the process receives signals directly (graceful SIGTERM).
# Deps (in python:3.12-slim): sh, sed, uvicorn.
# ──────────────────────────────────────────────────────────────────────────────
set -e
set -u
# set -x  # uncomment temporarily to enable command tracing for debugging
# pipefail would be ideal but /bin/sh is dash on python:3.12-slim

log() { echo "[entrypoint_api] $(date -Iseconds) $*"; }
# ERR trap not supported in this dash version (python:3.12-slim).
# 'set -e' handles error exits; the trap would just add diagnostics.
# If ERR trap support is needed, wrap in: trap ... ERR 2>/dev/null || true

# ── Fail fast on required config ──────────────────────────────────────────
if [ -z "${OZ_DATABASE_URL:-${DATABASE_URL:-}}" ]; then
    log "FATAL: OZ_DATABASE_URL (or DATABASE_URL) is not set — refusing to boot."
    exit 1
fi
if [ -z "${OZ_SECRET_KEY:-}" ]; then
    log "FATAL: OZ_SECRET_KEY is not set — refusing to boot."
    exit 1
fi

# Mask the password if a full URL leaked into a log line elsewhere.
log "DATABASE_URL set: $(echo "${OZ_DATABASE_URL:-$DATABASE_URL}" | sed 's|://[^:]*:[^@]*@|://***:***@|')"
log "SECRET_KEY set: yes (length=${#OZ_SECRET_KEY})"

log "Starting uvicorn ..."
# --proxy-headers makes uvicorn rewrite scope["client"] from X-Forwarded-For
# so the per-IP rate limiters and the audit log see the real client instead
# of the reverse proxy.  Only peers listed in --forwarded-allow-ips may set
# that header — an unset OZ_TRUSTED_PROXY_IPS falls back to 127.0.0.1
# (uvicorn's own default), never to "*", so a client-supplied
# X-Forwarded-For from an arbitrary peer is ignored.
exec uvicorn services.api.asgi:app --host 0.0.0.0 --port 8000 \
    --workers "${UVICORN_WORKERS:-1}" \
    --proxy-headers \
    --forwarded-allow-ips="${OZ_TRUSTED_PROXY_IPS:-127.0.0.1}"
