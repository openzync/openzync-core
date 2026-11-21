#!/bin/sh
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — Worker Service Entrypoint
# ──────────────────────────────────────────────────────────────────────────────
# Runs as PID 1 in the `worker` container. All config arrives via the
# environment (compose env_file: ../.env plus direct OZ_* entries) — no
# sidecars, no rendered secret files. Fail-fasts on the keys the worker
# cannot run without, then execs the ARQ worker so the Python process
# becomes PID 1 and receives signals directly (no intermediate shell).
# ──────────────────────────────────────────────────────────────────────────────
set -e
set -u
# pipefail would be ideal but /bin/sh is dash on python:3.12-slim

log() { echo "[entrypoint_worker] $(date -Iseconds) $*"; }

# Surface any unexpected failure as a visible FATAL line before exiting.
# ERR trap is NOT supported in this dash version — 'set -e' handles exits.
# If ERR trap is added later, wrap in: trap ... ERR 2>/dev/null || true

# ── Fail fast on required config ──────────────────────────────────────────
if [ -z "${OZ_DATABASE_URL:-${DATABASE_URL:-}}" ]; then
    log "FATAL: OZ_DATABASE_URL (or DATABASE_URL) is not set — refusing to boot."
    exit 1
fi
if [ -z "${OZ_SECRET_KEY:-}" ]; then
    log "FATAL: OZ_SECRET_KEY is not set — refusing to boot."
    exit 1
fi

log "DATABASE_URL set: $(echo "${OZ_DATABASE_URL:-$DATABASE_URL}" | sed 's|://[^:]*:[^@]*@|://***:***@|')"
log "Starting ARQ worker ..."
exec python -m services.worker.worker
