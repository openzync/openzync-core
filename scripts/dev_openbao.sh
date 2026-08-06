#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — Dev OpenBao bootstrap (idempotent)
# ──────────────────────────────────────────────────────────────────────────────
# Creates (if absent) a persistent OpenBao container, bootstraps the
# system namespace / AppRole auth / system config, and syncs fresh
# AppRole credentials into .env. Safe to re-run any time.
#
# Persistence model (survives reboots and container restarts):
#   - Raft storage in named volume openbao-dev-data
#   - Unseal keys + root token saved once in .openbao-dev/init.json
#   - Every start: auto-unseals with 3 of 5 saved keys (Shamir, same
#     flow as scripts/init_openbao.sh in the compose stack)
#
# Usage:  make openbao-dev    (or run this script directly)
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

CONTAINER="openbao-dev"
VOLUME="openbao-dev-data"
IMAGE="openbao/openbao:2.5"
ADDR="http://127.0.0.1:8200"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${ROOT_DIR}/.openbao-dev"
INIT_JSON="${STATE_DIR}/init.json"
CONFIG_FILE="${STATE_DIR}/config.hcl"
POLICIES_DIR="${ROOT_DIR}/infra/openbao/policies"
ENV_FILE="${ROOT_DIR}/.env"

mkdir -p "${STATE_DIR}"

log() { echo "[dev-openbao] $*"; }

# ── 1. Dev config — raft storage, listen on 8200 ──────────────────────────────
cat > "${CONFIG_FILE}" <<HCL
storage "raft" {
  path    = "/vault/data"
  node_id = "dev"
}
listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}
api_addr     = "http://127.0.0.1:8200"
cluster_addr = "http://127.0.0.1:8201"
log_level    = "info"
HCL

# ── 2. Ensure container exists (recreate only if it was manually created) ─────
EXISTS="$(docker inspect "${CONTAINER}" >/dev/null 2>&1 && echo yes || echo no)"
LABELED="$(docker inspect "${CONTAINER}" --format '{{index .Config.Labels "openzync.dev-openbao"}}' 2>/dev/null || echo no)"
if [ "${EXISTS}" = "yes" ] && [ "${LABELED}" != "true" ]; then
    log "Found unmanaged container '${CONTAINER}' (in-memory dev mode) — replacing with persistent one"
    docker rm -f "${CONTAINER}" >/dev/null
    EXISTS="no"
fi

if [ "${EXISTS}" = "no" ]; then
    log "Creating persistent OpenBao container '${CONTAINER}'"
    docker run -d \
        --name "${CONTAINER}" \
        --restart unless-stopped \
        --label openzync.dev-openbao=true \
        --cap-add IPC_LOCK \
        -p 127.0.0.1:8200:8200 \
        -v "${VOLUME}:/vault/data" \
        -v "${CONFIG_FILE}:/etc/openbao/config.hcl:ro,z" \
        -v "${POLICIES_DIR}:/policies:ro,z" \
        --entrypoint /usr/bin/dumb-init \
        "${IMAGE}" -- /bin/sh -c \
        "chown -R openbao:openbao /vault/data 2>/dev/null; exec /usr/local/bin/docker-entrypoint.sh server -config=/etc/openbao/config.hcl" \
        >/dev/null
else
    docker start "${CONTAINER}" >/dev/null 2>&1 || true
    log "Container '${CONTAINER}' already running"
fi

# ── 3. Wait for OpenBao to be reachable ──────────────────────────────────────
# `bao status` exits non-zero on an uninitialised/sealed server, so probe for
# valid JSON output instead of the exit code (same approach as init_openbao.sh).
log "Waiting for OpenBao at ${ADDR} ..."
reachable() {
    local out
    out="$(docker exec "${CONTAINER}" bao status -address="${ADDR}" -format=json 2>/dev/null)" || true
    echo "${out}" | python3 -c "import sys,json; json.load(sys.stdin)" >/dev/null 2>&1
}
for _ in $(seq 1 30); do
    if reachable; then
        break
    fi
    sleep 1
done
reachable || { log "FATAL: OpenBao did not become reachable"; exit 1; }
log "OpenBao reachable."

exec_in() { docker exec "${CONTAINER}" "$@"; }

# ── 4. Init once, then unseal with saved keys on every start ──────────────────
# `bao status` exits non-zero while sealed/uninitialised; absorb that and read
# only the JSON fields we need.
bao_status_field() {
    local field="$1"
    docker exec "${CONTAINER}" bao status -address="${ADDR}" -format=json 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('${field}', False))" \
        || true
}

INITIALIZED="$(bao_status_field initialized)"
if [ "${INITIALIZED}" = "False" ]; then
    log "Initialising OpenBao (5 keys, threshold 3) ..."
    exec_in bao operator init -address="${ADDR}" -key-shares=5 -key-threshold=3 -format=json > "${INIT_JSON}"
    chmod 600 "${INIT_JSON}"
    log "Unseal keys + root token saved to ${INIT_JSON}"
elif [ ! -f "${INIT_JSON}" ]; then
    log "FATAL: OpenBao is initialised but ${INIT_JSON} is missing — cannot unseal."
    exit 1
fi

SEALED="$(bao_status_field sealed)"
if [ "${SEALED}" = "True" ]; then
    log "Unsealing with 3 of 5 saved keys ..."
    for _i in 0 1 2; do
        KEY="$(python3 -c "import json; print(json.load(open('${INIT_JSON}'))['unseal_keys_b64'][${_i}])")"
        exec_in bao operator unseal -address="${ADDR}" "${KEY}" >/dev/null
    done
    log "OpenBao unsealed."
fi

ROOT_TOKEN="$(python3 -c "import json; print(json.load(open('${INIT_JSON}'))['root_token'])")"
if [ -z "${ROOT_TOKEN}" ]; then
    log "FATAL: root token missing in ${INIT_JSON}"
    exit 1
fi

# ── 5. Idempotent bootstrap (namespace / KV / AppRole / config) ───────────────
# set -f (noglob) so values like CORS_ORIGINS=* are passed literally to bao,
# not glob-expanded by the inner shell.
bao() { exec_in sh -c "set -f; BAO_ADDR=${ADDR} BAO_TOKEN=${ROOT_TOKEN} bao $*"; }

log "Creating 'system' namespace ..."
bao "namespace create system 2>/dev/null || true"

log "Enabling KV v2 at system/config ..."
bao "secrets enable -namespace=system/ -path=config kv-v2 2>/dev/null || true"

log "Enabling AppRole auth in system/ ..."
bao "auth enable -namespace=system/ approle 2>/dev/null || true"

log "Writing ACL policies ..."
bao "policy write -namespace=system/ openzync-app /policies/openzync-app.hcl"
bao "policy write -namespace=system/ openzync-worker /policies/openzync-worker.hcl"

log "Creating AppRole roles ..."
bao "write -namespace=system/ auth/approle/role/openzync-app token_policies=openzync-app token_ttl=24h token_max_ttl=72h"
bao "write -namespace=system/ auth/approle/role/openzync-worker token_policies=openzync-worker token_ttl=72h token_max_ttl=168h"

# System config — dev defaults. Secret key / webhook secret are generated
# once and persisted so JWT signing stays stable across restarts.
SECRET_KEY_FILE="${STATE_DIR}/secret-key"
WEBHOOK_FILE="${STATE_DIR}/webhook-secret"
if [ ! -f "${SECRET_KEY_FILE}" ]; then python3 -c "import secrets; print(secrets.token_urlsafe(48))" > "${SECRET_KEY_FILE}"; fi
if [ ! -f "${WEBHOOK_FILE}" ]; then python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "${WEBHOOK_FILE}"; fi

log "Writing system config ..."
bao "kv put -namespace=system/ config/system \
    OZ_REDIS_URL=redis://localhost:6379/0 \
    OZ_DATABASE_URL=postgresql+asyncpg://openzep@localhost:5432/openzep \
    OZ_SECRET_KEY=$(cat "${SECRET_KEY_FILE}") \
    OZ_WEBHOOK_SIGNING_SECRET=$(cat "${WEBHOOK_FILE}") \
    OZ_ENVIRONMENT=development \
    OZ_CORS_ORIGINS=* \
    OZ_LOG_LEVEL=INFO \
    OZ_MAX_WORKERS=4 \
    OZ_JWT_ACCESS_TOKEN_TTL_MINUTES=30 \
    OZ_JWT_REFRESH_TOKEN_TTL_DAYS=7 \
    OZ_PROMETHEUS_URL=http://localhost:9090 \
    OZ_HOSTS_ALLOWED=* \
    OZ_PROMPT_CACHING_ENABLED=true \
    OZ_PROMPT_CACHING_ANTHROPIC_MIN_TOKENS=1024 \
    OZ_PROMPT_CACHING_ANTHROPIC_TTL=5m" >/dev/null

# ── 6. Read fresh credentials and sync into .env ──────────────────────────────
APP_ROLE_ID="$(bao "read -namespace=system/ -field=role_id auth/approle/role/openzync-app/role-id")"
APP_SECRET_ID="$(bao "write -f -namespace=system/ -field=secret_id auth/approle/role/openzync-app/secret-id")"
WORKER_ROLE_ID="$(bao "read -namespace=system/ -field=role_id auth/approle/role/openzync-worker/role-id")"
WORKER_SECRET_ID="$(bao "write -f -namespace=system/ -field=secret_id auth/approle/role/openzync-worker/secret-id")"

export APP_ROLE_ID APP_SECRET_ID WORKER_ROLE_ID WORKER_SECRET_ID
python3 - "${ENV_FILE}" <<'PYEOF'
import os
import pathlib
import sys

env_file = pathlib.Path(sys.argv[1])
vars = {
    "OZ_OPENBAO_ROLE_ID": os.environ["APP_ROLE_ID"],
    "OZ_OPENBAO_SECRET_ID": os.environ["APP_SECRET_ID"],
    "OZ_OPENBAO_WORKER_ROLE_ID": os.environ["WORKER_ROLE_ID"],
    "OZ_OPENBAO_WORKER_SECRET_ID": os.environ["WORKER_SECRET_ID"],
}

text = env_file.read_text() if env_file.exists() else ""
lines = text.splitlines()
out = []
for line in lines:
    key = line.split("=", 1)[0].strip()
    if key in vars:
        out.append(f"{key}={vars.pop(key)}")
    else:
        out.append(line)
for key, value in vars.items():
    out.append(f"{key}={value}")
env_file.write_text("\n".join(out) + "\n")
PYEOF

log "Credentials synced to .env"
log "OpenBao ready at ${ADDR} — run 'make dev' to start the API."
