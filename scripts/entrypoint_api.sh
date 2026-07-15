#!/bin/sh
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — API Service Container Entrypoint
# ──────────────────────────────────────────────────────────────────────────────
# Runs in the `api` container as PID 1. A sidecar `openbao-agent-api`
# container shares the `api-secrets` tmpfs volume at /run/secrets, auths
# to OpenBao via AppRole, and renders system config to /run/secrets/system.env
# as KEY=VALUE lines. This script waits for that file, sources it, then
# execs uvicorn so the process receives signals directly (graceful SIGTERM).
# Deps (in python:3.12-slim): sh, sed, grep, sleep, uvicorn.
# ──────────────────────────────────────────────────────────────────────────────
set -e
set -u
# set -x  # uncomment temporarily to enable command tracing for debugging
# pipefail would be ideal but /bin/sh is dash on python:3.12-slim

log() { echo "[entrypoint_api] $(date -Iseconds) $*"; }
# ERR trap not supported in this dash version (python:3.12-slim).
# 'set -e' handles error exits; the trap would just add diagnostics.
# If ERR trap support is needed, wrap in: trap ... ERR 2>/dev/null || true

log "Waiting for OpenBao Agent to render secrets to /run/secrets/system.env ..."

i=0
while [ "$i" -lt 90 ]; do
  if [ -s /run/secrets/system.env ] && grep -q '^DATABASE_URL=' /run/secrets/system.env; then
    break
  fi
  i=$((i + 1))
  sleep 1
done

if [ "$i" -ge 90 ]; then
  log "FATAL: timed out (90s) waiting for /run/secrets/system.env — sidecar may be down or AppRole auth failing"
  exit 1
fi

log "OpenBao Agent secrets ready. Sourcing /run/secrets/system.env ..."

# `set -a` auto-exports every variable assigned during the source — the POSIX
# equivalent of bash's `source <(envsubst ...)` and works identically.
set -a
. /run/secrets/system.env
set +a

# Fallback: if OZ_OPENBAO_ROLE_ID / OZ_OPENBAO_SECRET_ID were not in the
# system.env (they are NOT part of the system secret), read them from the
# bootstrap files written by init_openbao.sh to the shared init-data volume.
# The api sidecar Agent authenticates using api-role_id / api-secret_id;
# the Python OpenBaoClient reuses the same credentials for runtime auth.
if [ -z "${OZ_OPENBAO_ROLE_ID:-}" ] || [ -z "${OZ_OPENBAO_SECRET_ID:-}" ]; then
    if [ -f /openbao-bootstrap/api-role_id ] && [ -f /openbao-bootstrap/api-secret_id ]; then
        log "Reading OpenBao AppRole credentials from bootstrap files ..."
        OZ_OPENBAO_ROLE_ID=$(cat /openbao-bootstrap/api-role_id)
        OZ_OPENBAO_SECRET_ID=$(cat /openbao-bootstrap/api-secret_id)
        export OZ_OPENBAO_ROLE_ID OZ_OPENBAO_SECRET_ID
    else
        log "WARNING: OZ_OPENBAO_ROLE_ID not set and bootstrap files not found at /openbao-bootstrap/"
        log "WARNING: Python OpenBaoClient will fail at startup without these credentials."
    fi
fi

log "DATABASE_URL set: $(echo "$DATABASE_URL" | sed 's|://[^:]*:[^@]*@|://***:***@|')"
log "SECRET_KEY set: ${SECRET_KEY:+yes (length=${#SECRET_KEY})}"

log "Starting uvicorn ..."
exec uvicorn services.api.asgi:app --host 0.0.0.0 --port 8000 --workers "${UVICORN_WORKERS:-1}"
