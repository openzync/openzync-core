#!/bin/sh
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — Worker Service Entrypoint
# ──────────────────────────────────────────────────────────────────────────────
# Runs as PID 1 in the `worker` container.
#
# Orchestration contract:
#   - A sidecar `openbao-agent-worker` container shares the `worker-secrets`
#     tmpfs volume mounted at /run/secrets.
#   - The Agent authenticates to OpenBao via AppRole and renders the system
#     config to /run/secrets/system.env as KEY=VALUE lines.
#   - This entrypoint waits for that file, sources it as environment, and
#     execs the ARQ worker so the Python process becomes PID 1 and receives
#     signals directly (no intermediate shell wrapper).
# ──────────────────────────────────────────────────────────────────────────────
set -e
set -u
# pipefail would be ideal but /bin/sh is dash on python:3.12-slim

log() { echo "[entrypoint_worker] $(date -Iseconds) $*"; }

# Surface any unexpected failure as a visible FATAL line before exiting.
trap 'log "FATAL: unexpected error on line $LINENO (exit $?)"; exit 1' ERR

SECRETS_FILE="/run/secrets/system.env"
TIMEOUT_SEC=90
log "Waiting for OpenBao Agent to render secrets to ${SECRETS_FILE} ..."
_i=0
while [ "$_i" -lt "${TIMEOUT_SEC}" ]; do
    if [ -s "${SECRETS_FILE}" ] && grep -q '^DATABASE_URL=' "${SECRETS_FILE}"; then
        break
    fi
    _i=$((_i + 1))
    sleep 1
done

if [ "$_i" -ge "${TIMEOUT_SEC}" ]; then
    log "FATAL: ${SECRETS_FILE} not rendered within ${TIMEOUT_SEC}s."
    exit 1
fi

log "OpenBao Agent secrets ready. Sourcing ${SECRETS_FILE} ..."
set -a
. "${SECRETS_FILE}"
set +a

# Fallback: if OZ_OPENBAO_ROLE_ID / OZ_OPENBAO_SECRET_ID were not in the
# system.env (they are NOT part of the system secret), read them from the
# bootstrap files written by init_openbao.sh to the shared init-data volume.
# The worker sidecar Agent authenticates using worker-role_id / worker-secret_id;
# the Python OpenBaoClient reuses the same credentials for runtime auth.
if [ -z "${OZ_OPENBAO_ROLE_ID:-}" ] || [ -z "${OZ_OPENBAO_SECRET_ID:-}" ]; then
    if [ -f /openbao-bootstrap/worker-role_id ] && [ -f /openbao-bootstrap/worker-secret_id ]; then
        log "Reading OpenBao AppRole credentials from bootstrap files ..."
        OZ_OPENBAO_ROLE_ID=$(cat /openbao-bootstrap/worker-role_id)
        OZ_OPENBAO_SECRET_ID=$(cat /openbao-bootstrap/worker-secret_id)
        export OZ_OPENBAO_ROLE_ID OZ_OPENBAO_SECRET_ID
    else
        log "WARNING: OZ_OPENBAO_ROLE_ID not set and bootstrap files not found at /openbao-bootstrap/"
        log "WARNING: Python OpenBaoClient will fail at startup without these credentials."
    fi
fi

log "DATABASE_URL set: $(echo "$DATABASE_URL" | sed 's|://[^:]*:[^@]*@|://***:***@|')"
log "Starting ARQ worker ..."
exec python -m services.worker.worker
