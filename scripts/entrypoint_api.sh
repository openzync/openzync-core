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
set -o pipefail

log() { echo "[entrypoint_api] $(date -Iseconds) $*"; }
trap 'log "FATAL: unexpected error on line $LINENO — exiting"; exit 1' ERR

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

log "DATABASE_URL set: $(echo "$DATABASE_URL" | sed 's|://[^:]*:[^@]*@|://***:***@|')"
log "SECRET_KEY set: ${SECRET_KEY:+yes (length=${#SECRET_KEY})}"

log "Starting uvicorn ..."
exec uvicorn services.api.asgi:app --host 0.0.0.0 --port 8000 --workers "${UVICORN_WORKERS:-1}"
