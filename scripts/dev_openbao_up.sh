#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — Host-Dev OpenBao + Postgres bring-up
# ──────────────────────────────────────────────────────────────────────────────
# The daily dev dependency script. Idempotent — safe to run every morning.
#
# Usage:
#   scripts/dev_openbao_up.sh [up|down|status]   (default: up)
#
#   up      ensure postgres + openbao containers, run the full OpenBao
#           bootstrap (scripts/init_openbao.sh — regenerates AppRole
#           secret_ids every run), re-sync .env with fresh credentials.
#   down    stop both containers (data volumes are preserved).
#   status  one-line health check.
#
# Secrets:
#   - Unseal keys / root token / AppRole ids live in the
#     openzync-dev-openbao-init volume (written by init_openbao.sh).
#   - Stable OZ_SECRET_KEY / OZ_WEBHOOK_SIGNING_SECRET / postgres password
#     persist in scripts/.dev_openbao_secrets.env (0600). Generated on first
#     run; reused afterwards so JWTs and encrypted payloads survive restarts.
#   - .env is re-synced from /bao-init after EVERY bootstrap because the
#     init script mints fresh AppRole secret_ids each run.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
SECRETS_FILE="${REPO_ROOT}/scripts/.dev_openbao_secrets.env"
CONFIG_FILE="${REPO_ROOT}/infra/openbao/config.dev.hcl"
POLICIES_DIR="${REPO_ROOT}/infra/openbao/policies"
INIT_SCRIPT="${REPO_ROOT}/scripts/init_openbao.sh"

OPENBAO_CONTAINER="openzync-dev-openbao"
OPENBAO_IMAGE="openbao/openbao:2.5"
OPENBAO_INIT_IMAGE="infra-openbao-init:latest"   # base + python3, built from infra/Dockerfile.openbao-tooling
OPENBAO_DATA_VOL="openzync-dev-openbao-data"
OPENBAO_INIT_VOL="openzync-dev-openbao-init"
POSTGRES_CONTAINER="openzync-dev-postgres"
POSTGRES_IMAGE="pgvector/pgvector:pg15"   # postgres 15 + pgvector (app uses vector extension)
BAO_ADDR="http://127.0.0.1:8200"

log() { echo "[dev_openbao] $(date -Iseconds) $*"; }

container_exists() { docker ps -a --format '{{.Names}}' | grep -qx "$1"; }

ensure_volume() { docker volume inspect "$1" >/dev/null 2>&1 || docker volume create "$1" >/dev/null; }

wait_postgres() {
    for _ in $(seq 1 30); do
        docker exec "$POSTGRES_CONTAINER" pg_isready -U postgres -h localhost >/dev/null 2>&1 && return 0
        sleep 1
    done
    log "FATAL: postgres did not become ready within 30s."
    exit 1
}

# ── 1. Stable secrets (OZ_SECRET_KEY, webhook secret, postgres URL) ───────────
gen_secrets() {
    umask 077
    [ -f "$SECRETS_FILE" ] || {
        log "Generating initial secrets -> ${SECRETS_FILE}"
        {
            echo "OZ_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
            echo "OZ_WEBHOOK_SIGNING_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
        } > "$SECRETS_FILE"
        chmod 600 "$SECRETS_FILE"
    }
    # shellcheck disable=SC1090
    set -a; source "$SECRETS_FILE"; set +a
}

# ── 2. Postgres container (owned by this script so the password lives only
#      in .dev_openbao_secrets.env, never in a hardcoded script). ─────────────
ensure_postgres() {
    if ! container_exists "$POSTGRES_CONTAINER"; then
        if ! grep -q '^OZ_DATABASE_URL=' "$SECRETS_FILE"; then
            local pw
            pw="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
            echo "OZ_DATABASE_URL=postgresql+asyncpg://openzync:${pw}@localhost:5432/openzync" >> "$SECRETS_FILE"
            chmod 600 "$SECRETS_FILE"
        fi
        local pw
        pw="$(sed -n 's|^OZ_DATABASE_URL=postgresql+asyncpg://openzync:\([^@]*\)@.*|\1|p' "$SECRETS_FILE")"
        log "Creating postgres container ${POSTGRES_CONTAINER} ..."
        docker run -d --name "$POSTGRES_CONTAINER" --restart unless-stopped \
            -e POSTGRES_PASSWORD="$pw" \
            -p 127.0.0.1:5432:5432 \
            "$POSTGRES_IMAGE" >/dev/null
        wait_postgres
        docker exec "$POSTGRES_CONTAINER" psql -U postgres -v ON_ERROR_STOP=1 \
            -c "CREATE ROLE openzync LOGIN PASSWORD '${pw}';" \
            -c "CREATE DATABASE openzync OWNER openzync;" >/dev/null
        log "Role 'openzync' + database 'openzync' created."
    else
        docker start "$POSTGRES_CONTAINER" >/dev/null 2>&1 || true
        wait_postgres
    fi
    # Re-source: first run appends OZ_DATABASE_URL to the secrets file after
    # gen_secrets already sourced it.
    # shellcheck disable=SC1090
    set -a; source "$SECRETS_FILE"; set +a
}

# ── 3. OpenBao server container (Shamir-sealed, config.dev.hcl) ───────────────
ensure_openbao() {
    ensure_volume "$OPENBAO_DATA_VOL"
    ensure_volume "$OPENBAO_INIT_VOL"
    if ! container_exists "$OPENBAO_CONTAINER"; then
        log "Creating OpenBao container ${OPENBAO_CONTAINER} ..."
        # Image drops to openbao (UID 100) via su-exec; chown the volume first
        # (same convention as infra/docker-compose.backend.yml openbao service).
        docker run -d --name "$OPENBAO_CONTAINER" --restart unless-stopped \
            --entrypoint /usr/bin/dumb-init \
            -v "$OPENBAO_DATA_VOL:/vault/data" \
            -v "$CONFIG_FILE:/vault/config.hcl:ro,z" \
            -p 127.0.0.1:8200:8200 \
            -p 127.0.0.1:8201:8201 \
            "$OPENBAO_IMAGE" -- /bin/sh -c \
            "chown -R openbao:openbao /vault/data 2>/dev/null; exec /usr/local/bin/docker-entrypoint.sh server -config=/vault/config.hcl" >/dev/null
    else
        docker start "$OPENBAO_CONTAINER" >/dev/null 2>&1 || true
    fi
}

# ── 4. Full bootstrap (idempotent — always re-run) ────────────────────────────
bootstrap() {
    log "Running init_openbao.sh bootstrap ..."
    docker run --rm --network host --entrypoint /bin/sh \
        -e BAO_ADDR="$BAO_ADDR" \
        -e BAO_SKIP_VERIFY=true \
        -e OZ_REDIS_URL=redis://localhost:6379/0 \
        -e OZ_FALKORDB_URL=redis://localhost:6380 \
        -e OZ_FALKORDB_MAX_CONNECTIONS=20 \
        -e OZ_FALKORDB_SOCKET_TIMEOUT=30 \
        -e OZ_SECRET_KEY="$OZ_SECRET_KEY" \
        -e OZ_WEBHOOK_SIGNING_SECRET="$OZ_WEBHOOK_SIGNING_SECRET" \
        -e OZ_ENVIRONMENT=development \
        -e OZ_LOG_LEVEL=INFO \
        -e OZ_MAX_WORKERS=4 \
        -e OZ_JWT_ACCESS_TOKEN_TTL_MINUTES=30 \
        -e OZ_JWT_REFRESH_TOKEN_TTL_DAYS=7 \
        -e OZ_RATE_LIMIT_IP_MAX=10 \
        -e OZ_RATE_LIMIT_WINDOW_SEC=60 \
        -e OZ_HOSTS_ALLOWED=localhost:8000 \
        -e OZ_PROMPT_CACHING_ENABLED=true \
        -e OZ_PROMPT_CACHING_ANTHROPIC_MIN_TOKENS=1024 \
        -e OZ_PROMPT_CACHING_ANTHROPIC_TTL=5m \
        -e OZ_CORS_ORIGINS=http://localhost:3000 \
        -e OZ_DATABASE_URL="$OZ_DATABASE_URL" \
        -v "$POLICIES_DIR:/policies:ro,z" \
        -v "$OPENBAO_INIT_VOL:/bao-init" \
        -v "$INIT_SCRIPT:/init_openbao.sh:ro,z" \
        "$OPENBAO_INIT_IMAGE" /init_openbao.sh
}

# ── 5. Re-sync .env with fresh AppRole credentials from the init volume ───────
sync_env() {
    local api_role api_secret worker_role worker_secret
    api_role="$(docker run --rm --entrypoint /bin/sh -v "$OPENBAO_INIT_VOL:/bao-init" "$OPENBAO_IMAGE" -c 'cat /bao-init/api-role_id')"
    api_secret="$(docker run --rm --entrypoint /bin/sh -v "$OPENBAO_INIT_VOL:/bao-init" "$OPENBAO_IMAGE" -c 'cat /bao-init/api-secret_id')"
    worker_role="$(docker run --rm --entrypoint /bin/sh -v "$OPENBAO_INIT_VOL:/bao-init" "$OPENBAO_IMAGE" -c 'cat /bao-init/worker-role_id')"
    worker_secret="$(docker run --rm --entrypoint /bin/sh -v "$OPENBAO_INIT_VOL:/bao-init" "$OPENBAO_IMAGE" -c 'cat /bao-init/worker-secret_id')"
    python3 - "$ENV_FILE" "$api_role" "$api_secret" "$worker_role" "$worker_secret" <<'PY'
import sys

path, api_role, api_secret, worker_role, worker_secret = sys.argv[1:]
repl = {
    "OZ_OPENBAO_ROLE_ID=": f"OZ_OPENBAO_ROLE_ID={api_role}\n",
    "OZ_OPENBAO_SECRET_ID=": f"OZ_OPENBAO_SECRET_ID={api_secret}\n",
    "OZ_OPENBAO_WORKER_ROLE_ID=": f"OZ_OPENBAO_WORKER_ROLE_ID={worker_role}\n",
    "OZ_OPENBAO_WORKER_SECRET_ID=": f"OZ_OPENBAO_WORKER_SECRET_ID={worker_secret}\n",
}
with open(path) as f:
    lines = f.readlines()
with open(path, "w") as f:
    for line in lines:
        for prefix, new_line in repl.items():
            if line.startswith(prefix):
                line = new_line
                break
        f.write(line)
print(f"  Synced {len(repl)} OZ_OPENBAO_* keys in {path}")
PY
    chmod 600 "$SECRETS_FILE"
}

up() {
    gen_secrets
    ensure_postgres
    ensure_openbao
    bootstrap
    sync_env
    log "Done. Dev deps up. API: uvicorn services.api.asgi:app --reload"
}

down() {
    for c in "$OPENBAO_CONTAINER" "$POSTGRES_CONTAINER"; do
        container_exists "$c" && { log "Stopping ${c} ..."; docker stop "$c" >/dev/null; }
    done
}

status() {
    if curl -sf "${BAO_ADDR}/v1/sys/health" 2>/dev/null | grep -q '"sealed":false'; then
        echo "OpenBao: UP (initialized+unsealed)"
    else
        echo "OpenBao: DOWN / sealed"
    fi
    if container_exists "$POSTGRES_CONTAINER" && docker ps --format '{{.Names}}' | grep -qx "$POSTGRES_CONTAINER"; then
        echo "Postgres: UP"
    else
        echo "Postgres: DOWN"
    fi
}

case "${1:-up}" in
    up) up ;;
    down) down ;;
    status) status ;;
    *) echo "usage: $0 [up|down|status]"; exit 1 ;;
esac
