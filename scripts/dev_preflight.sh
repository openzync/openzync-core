#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — Host-Dev preflight (postgres + redis + falkordb + mailpit + openbao)
# ──────────────────────────────────────────────────────────────────────────────
# The daily dev dependency script. Idempotent — safe to run every morning.
#
# Usage:
#   scripts/dev_preflight.sh [up|down|status]   (default: up)
#
#   up      ensure postgres + redis + falkordb + mailpit + openbao
#           containers, run the full OpenBao bootstrap
#           (scripts/init_openbao.sh — regenerates AppRole secret_ids every
#           run), re-sync .env with fresh credentials.
#   down    stop the dev containers (data volumes are preserved).
#   status  one-line health check per dependency.
#
# Secrets:
#   - Unseal keys / root token / AppRole ids live in the
#     openzync-dev-openbao-init volume (written by init_openbao.sh).
#   - Stable OZ_SECRET_KEY / OZ_WEBHOOK_SIGNING_SECRET / postgres password
#     persist in scripts/.dev_preflight_secrets.env (0600). Generated on first
#     run; reused afterwards so JWTs and encrypted payloads survive restarts.
#   - .env is re-synced from /bao-init after EVERY bootstrap because the
#     init script mints fresh AppRole secret_ids each run.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
SECRETS_FILE="${REPO_ROOT}/scripts/.dev_preflight_secrets.env"
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
POSTGRES_DATA_VOL="openzync-dev-postgres-data"
REDIS_CONTAINER="openzync-dev-redis"
REDIS_IMAGE="redis:7-alpine"
REDIS_DATA_VOL="openzync-dev-redis-data"
FALKORDB_CONTAINER="openzync-dev-falkordb"
FALKORDB_IMAGE="falkordb/falkordb:v4.20.1-alpine"   # pinned — :latest breaks repro
FALKORDB_DATA_VOL="openzync-dev-falkordb-data"
BAO_ADDR="http://127.0.0.1:8200"

log() { echo "[dev_preflight] $(date -Iseconds) $*"; }

container_exists() { docker ps -a --format '{{.Names}}' | grep -qx "$1"; }
container_running() { docker ps --format '{{.Names}}' | grep -qx "$1"; }

ensure_volume() { docker volume inspect "$1" >/dev/null 2>&1 || docker volume create "$1" >/dev/null; }

ensure_tooling_image() {
    docker image inspect "$OPENBAO_INIT_IMAGE" >/dev/null 2>&1 && return 0
    log "Building OpenBao tooling image ${OPENBAO_INIT_IMAGE} ..."
    docker build -f "${REPO_ROOT}/infra/Dockerfile.openbao-tooling" -t "$OPENBAO_INIT_IMAGE" "${REPO_ROOT}/infra"
}

wait_postgres() {
    for _ in $(seq 1 30); do
        docker exec "$POSTGRES_CONTAINER" pg_isready -U postgres -h localhost >/dev/null 2>&1 && return 0
        sleep 1
    done
    log "FATAL: postgres did not become ready within 30s."
    exit 1
}

wait_redis_port() {
    local container="$1"
    for _ in $(seq 1 30); do
        docker exec "$container" redis-cli PING >/dev/null 2>&1 && return 0
        sleep 1
    done
    log "FATAL: redis ${container} did not become ready within 30s."
    exit 1
}

# ── Host-port guards (port-less-container fail-fast) ─────────────────────────
# In-container readiness (pg_isready / redis-cli via docker exec) passes even
# when the host port was never published. These probe the HOST side; on
# failure the caller fail-fasts with the exact fix instead of crashing later
# (e.g. Connection refused at platform_seed).
host_port_listening() { ss -tlnp 2>/dev/null | grep -q ":$1"; }
host_postgres_ping() {
    if command -v pg_isready >/dev/null 2>&1; then
        pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1
    else
        host_port_listening 5432
    fi
}
host_redis_ping() {  # $1 = host port
    if command -v redis-cli >/dev/null 2>&1; then
        redis-cli -h 127.0.0.1 -p "$1" PING >/dev/null 2>&1
    else
        host_port_listening "$1"
    fi
}
portless_fatal() {  # $1 = container, $2 = host port
    cat >&2 <<EOF
[dev_preflight] FATAL: container $1 is running but host port 127.0.0.1:$2 is not reachable (port-less container — recreated without the -p publish flag).
Fix: docker rm -f $1 && scripts/dev_preflight.sh up
EOF
    exit 1
}

# ── 1. Stable secrets (OZ_SECRET_KEY, webhook secret, SMTP block) ────────────
# OZ_DATABASE_URL is appended by ensure_postgres() on first container creation;
# on a pre-existing postgres container it must already be present in the file.
gen_secrets() {
    umask 077
    if [ ! -f "$SECRETS_FILE" ]; then
        log "Generating initial secrets -> ${SECRETS_FILE}"
        {
            echo "OZ_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
            echo "OZ_WEBHOOK_SIGNING_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
            cat <<'EOF'
OZ_SMTP_HOST=localhost
OZ_SMTP_PORT=1025
OZ_SMTP_USERNAME=
OZ_SMTP_PASSWORD=
OZ_SMTP_FROM_ADDR=no-reply@openzync.local
OZ_SMTP_USE_TLS=false
OZ_SMTP_START_TLS=false
EOF
        } > "$SECRETS_FILE"
        chmod 600 "$SECRETS_FILE"
    elif ! grep -q '^OZ_SMTP_HOST=' "$SECRETS_FILE"; then
        # File predates mailpit — append the SMTP block so the bootstrap
        # env passthrough is never missing keys (they'd be wiped from the
        # system secret on the next full kv put).
        log "Appending SMTP block to ${SECRETS_FILE}"
        cat >> "$SECRETS_FILE" <<'EOF'
OZ_SMTP_HOST=localhost
OZ_SMTP_PORT=1025
OZ_SMTP_USERNAME=
OZ_SMTP_PASSWORD=
OZ_SMTP_FROM_ADDR=no-reply@openzync.local
OZ_SMTP_USE_TLS=false
OZ_SMTP_START_TLS=false
EOF
        chmod 600 "$SECRETS_FILE"
    fi
    # shellcheck disable=SC1090
    set -a; source "$SECRETS_FILE"; set +a
}

# ── 2. Postgres container (owned by this script so the password lives only
#      in .dev_preflight_secrets.env, never in a hardcoded script). ─────────────
ensure_postgres() {
    if container_running "$POSTGRES_CONTAINER"; then
        log "Postgres already running — skipping."
        docker exec "$POSTGRES_CONTAINER" pg_isready -U postgres -h localhost >/dev/null 2>&1 \
            || log "WARN: postgres running but pg_isready failed."
        host_postgres_ping || portless_fatal "$POSTGRES_CONTAINER" 5432
    elif container_exists "$POSTGRES_CONTAINER"; then
        log "Starting postgres container ${POSTGRES_CONTAINER} ..."
        docker start "$POSTGRES_CONTAINER"
        wait_postgres
        host_postgres_ping || portless_fatal "$POSTGRES_CONTAINER" 5432
    else
        if ! grep -q '^OZ_DATABASE_URL=' "$SECRETS_FILE"; then
            local pw
            pw="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
            echo "OZ_DATABASE_URL=postgresql+asyncpg://openzync:${pw}@localhost:5432/openzync" >> "$SECRETS_FILE"
            chmod 600 "$SECRETS_FILE"
        fi
        local pw
        pw="$(sed -n 's|^OZ_DATABASE_URL=postgresql+asyncpg://openzync:\([^@]*\)@.*|\1|p' "$SECRETS_FILE")"
        ensure_volume "$POSTGRES_DATA_VOL"
        log "Creating postgres container ${POSTGRES_CONTAINER} ..."
        docker run -d --name "$POSTGRES_CONTAINER" --restart unless-stopped \
            -e POSTGRES_PASSWORD="$pw" \
            -p 127.0.0.1:5432:5432 \
            -v "$POSTGRES_DATA_VOL:/var/lib/postgresql/data" \
            "$POSTGRES_IMAGE" >/dev/null
        wait_postgres
        host_postgres_ping || portless_fatal "$POSTGRES_CONTAINER" 5432
        docker exec "$POSTGRES_CONTAINER" psql -U postgres -v ON_ERROR_STOP=1 \
            -c "CREATE ROLE openzync LOGIN PASSWORD '${pw}';" \
            -c "CREATE DATABASE openzync OWNER openzync;" >/dev/null
        log "Role 'openzync' + database 'openzync' created."
    fi
    # Re-source: first run appends OZ_DATABASE_URL to the secrets file after
    # gen_secrets already sourced it.
    # shellcheck disable=SC1090
    set -a; source "$SECRETS_FILE"; set +a
}

# ── 3. Redis cache/queue (host :6379 — what OZ_REDIS_URL points at) ──────────
ensure_redis() {
    if container_running "$REDIS_CONTAINER"; then
        log "Redis already running — skipping."
        docker exec "$REDIS_CONTAINER" redis-cli PING >/dev/null 2>&1 \
            || log "WARN: redis running but PING failed."
        host_redis_ping 6379 || portless_fatal "$REDIS_CONTAINER" 6379
    elif container_exists "$REDIS_CONTAINER"; then
        log "Starting redis container ${REDIS_CONTAINER} ..."
        docker start "$REDIS_CONTAINER"
        wait_redis_port "$REDIS_CONTAINER"
        host_redis_ping 6379 || portless_fatal "$REDIS_CONTAINER" 6379
    else
        # Fail fast if another process holds :6379 (e.g. a local redis-server
        # or the compose stack) — same guard style as openbao :8200 below.
        if ss -tlnp 2>/dev/null | grep -q ':6379' \
            && ! container_running "$REDIS_CONTAINER"; then
            cat >&2 <<'EOF'
[dev_preflight] FATAL: port 127.0.0.1:6379 is already in use.
Likely cause: a local redis-server or another redis container is holding it.
Fix: sudo systemctl stop redis-server  # or: docker stop <container>
EOF
            exit 1
        fi
        ensure_volume "$REDIS_DATA_VOL"
        log "Creating redis container ${REDIS_CONTAINER} ..."
        docker run -d --name "$REDIS_CONTAINER" --restart unless-stopped \
            -p 127.0.0.1:6379:6379 \
            -v "$REDIS_DATA_VOL:/data" \
            "$REDIS_IMAGE" >/dev/null
        wait_redis_port "$REDIS_CONTAINER"
        host_redis_ping 6379 || portless_fatal "$REDIS_CONTAINER" 6379
    fi
}

# ── 4. FalkorDB graph backend (RESP on host :6380 → container :6379) ─────────
ensure_falkordb() {
    if container_running "$FALKORDB_CONTAINER"; then
        log "FalkorDB already running — skipping."
        docker exec "$FALKORDB_CONTAINER" redis-cli PING >/dev/null 2>&1 \
            || log "WARN: falkordb running but PING failed."
        host_redis_ping 6380 || portless_fatal "$FALKORDB_CONTAINER" 6380
    elif container_exists "$FALKORDB_CONTAINER"; then
        log "Starting FalkorDB container ${FALKORDB_CONTAINER} ..."
        docker start "$FALKORDB_CONTAINER"
        wait_redis_port "$FALKORDB_CONTAINER"
        host_redis_ping 6380 || portless_fatal "$FALKORDB_CONTAINER" 6380
    else
        # Fail fast if another process holds :6380 — same guard style as :8200.
        if ss -tlnp 2>/dev/null | grep -q ':6380' \
            && ! container_running "$FALKORDB_CONTAINER"; then
            cat >&2 <<'EOF'
[dev_preflight] FATAL: port 127.0.0.1:6380 is already in use.
Likely cause: another falkordb/redis container is holding it.
Fix: docker stop <container>
EOF
            exit 1
        fi
        ensure_volume "$FALKORDB_DATA_VOL"
        log "Creating FalkorDB container ${FALKORDB_CONTAINER} ..."
        docker run -d --name "$FALKORDB_CONTAINER" --restart unless-stopped \
            -p 127.0.0.1:6380:6379 \
            -e REDIS_ARGS="--appendonly yes" \
            -v "$FALKORDB_DATA_VOL:/data" \
            "$FALKORDB_IMAGE" >/dev/null
        wait_redis_port "$FALKORDB_CONTAINER"
        host_redis_ping 6380 || portless_fatal "$FALKORDB_CONTAINER" 6380
    fi
}

# ── 5. Mailpit (SMTP sink + web UI; local dev only) ──────────────────────────
ensure_mailpit() {
    if container_running mailpit; then
        log "Mailpit already running — skipping."
    elif container_exists mailpit; then
        log "Starting mailpit ..."
        docker start mailpit
    else
        log "Creating mailpit container ..."
        docker run -d --name mailpit --restart unless-stopped \
            -p 127.0.0.1:1025:1025 \
            -p 127.0.0.1:8025:8025 \
            axllent/mailpit >/dev/null
    fi
}

# ── 6. OpenBao server container (Shamir-sealed, config.dev.hcl) ───────────────
ensure_openbao() {
    ensure_volume "$OPENBAO_DATA_VOL"
    ensure_volume "$OPENBAO_INIT_VOL"
    if container_running "$OPENBAO_CONTAINER"; then
        log "OpenBao already running — skipping."
        curl -sf "${BAO_ADDR}/v1/sys/health" >/dev/null 2>&1 \
            || log "WARN: openbao running but health check failed (may still be sealed)."
    elif container_exists "$OPENBAO_CONTAINER"; then
        log "Starting OpenBao container ${OPENBAO_CONTAINER} ..."
        docker start "$OPENBAO_CONTAINER"
    else
        # Fail fast if another process/container holds :8200 (e.g. the compose
        # stack's openzync-openbao) — docker's "port is already allocated"
        # error is cryptic and leaves a stuck Created container behind.
        if ss -tlnp 2>/dev/null | grep -q ':8200' \
            && ! docker ps --format '{{.Names}}' | grep -qx "$OPENBAO_CONTAINER"; then
            cat >&2 <<'EOF'
[dev_preflight] FATAL: port 127.0.0.1:8200 is already in use.
Likely cause: the compose stack's openzync-openbao container is holding it.
Fix: docker stop openzync-openbao
EOF
            exit 1
        fi
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
    fi
}

# ── 7. Full bootstrap (idempotent — always re-run) ────────────────────────────
bootstrap() {
    log "Running init_openbao.sh bootstrap ..."
    docker run --rm --network host --entrypoint /bin/sh \
        -e BAO_ADDR="$BAO_ADDR" \
        -e BAO_SKIP_VERIFY=true \
        -e OZ_REDIS_URL=redis://localhost:6379/0 \
        -e OZ_FALKORDB_URL=redis://localhost:6380 \
        -e OZ_FALKORDB_MAX_CONNECTIONS=10 \
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
        -e OZ_SMTP_HOST="$OZ_SMTP_HOST" \
        -e OZ_SMTP_PORT="$OZ_SMTP_PORT" \
        -e OZ_SMTP_USERNAME="$OZ_SMTP_USERNAME" \
        -e OZ_SMTP_PASSWORD="$OZ_SMTP_PASSWORD" \
        -e OZ_SMTP_FROM_ADDR="$OZ_SMTP_FROM_ADDR" \
        -e OZ_SMTP_USE_TLS="$OZ_SMTP_USE_TLS" \
        -e OZ_SMTP_START_TLS="$OZ_SMTP_START_TLS" \
        -v "$POLICIES_DIR:/policies:ro,z" \
        -v "$OPENBAO_INIT_VOL:/bao-init" \
        -v "$INIT_SCRIPT:/init_openbao.sh:ro,z" \
        "$OPENBAO_INIT_IMAGE" /init_openbao.sh
}

# ── 8. Re-sync .env with fresh AppRole credentials from the init volume ───────
sync_env() {
    local api_role api_secret worker_role worker_secret
    api_role="$(docker run --rm --entrypoint /bin/sh -v "$OPENBAO_INIT_VOL:/bao-init" "$OPENBAO_IMAGE" -c 'cat /bao-init/api-role_id')"
    api_secret="$(docker run --rm --entrypoint /bin/sh -v "$OPENBAO_INIT_VOL:/bao-init" "$OPENBAO_IMAGE" -c 'cat /bao-init/api-secret_id')"
    worker_role="$(docker run --rm --entrypoint /bin/sh -v "$OPENBAO_INIT_VOL:/bao-init" "$OPENBAO_IMAGE" -c 'cat /bao-init/worker-role_id')"
    worker_secret="$(docker run --rm --entrypoint /bin/sh -v "$OPENBAO_INIT_VOL:/bao-init" "$OPENBAO_IMAGE" -c 'cat /bao-init/worker-secret_id')"
    python3 - "$ENV_FILE" "$api_role" "$api_secret" "$worker_role" "$worker_secret" <<'PY'
import sys

path, api_role, api_secret, worker_role, worker_secret = sys.argv[1:]
with open(path, "w") as f:
    f.write("# Bootstrap creds — app reads runtime config from OpenBao\n")
    f.write("OZ_OPENBAO_ADDR=http://localhost:8200\n")
    f.write(f"OZ_OPENBAO_ROLE_ID={api_role}\n")
    f.write(f"OZ_OPENBAO_SECRET_ID={api_secret}\n")
    f.write(f"OZ_OPENBAO_WORKER_ROLE_ID={worker_role}\n")
    f.write(f"OZ_OPENBAO_WORKER_SECRET_ID={worker_secret}\n")
print(f"  Wrote 5 keys to {path}")
PY
    chmod 600 "$ENV_FILE"
}

# ── 9. Host-dev Ollama embed URL seed (native Ollama on host) ───────────────
# Host-run uvicorn cannot resolve the compose-DNS name `ollama`, so the
# default OLLAMA_EMBED_URL (http://ollama:11434) fail-fasts the lifespan
# prewarm with DNS [Errno -2]. When a NATIVE Ollama answers on
# 127.0.0.1:11434 but `ollama` does not resolve from the host, seed
# OZ_OLLAMA_EMBED_URL=http://127.0.0.1:11434 into the OpenBao system
# secret (same namespace/path + read-merge-put mechanism as
# write_db_to_openbao.sh, CAS-guarded). Idempotent: skips the write when
# the stored value already matches. Never touches .env (bootstrap-only).
host_resolves_ollama() {
    if command -v getent >/dev/null 2>&1; then
        getent hosts ollama >/dev/null 2>&1
    else
        python3 -c 'import socket; socket.gethostbyname("ollama")' >/dev/null 2>&1
    fi
}

EFFECTIVE_EMBED_URL="http://ollama:11434"

seed_host_ollama_embed_url() {
    local host_url="http://127.0.0.1:11434"
    local want=""
    if curl -sf "${host_url}/api/tags" >/dev/null 2>&1; then
        if host_resolves_ollama; then
            log "'ollama' resolves from host — default embed URL works, skip."
        else
            want="$host_url"
            log "Native Ollama up but 'ollama' unresolvable — seeding system secret ..."
        fi
    else
        log "No native Ollama on 127.0.0.1:11434 — leaving embed URL as stored."
    fi
    # One docker call: merge-seed (only when want is set and stale) and
    # print the resulting effective URL to stdout (human logs go to stderr
    # so the capture stays clean). A bare `bao kv put key=val` would wipe
    # the other system keys — hence read-merge-put, like write_db_to_openbao.
    EFFECTIVE_EMBED_URL="$(docker run --rm -i --network host --entrypoint python3 \
        -e BAO_ADDR="$BAO_ADDR" \
        -e BAO_SKIP_VERIFY=true \
        -e OZ_OLLAMA_WANT="$want" \
        -v "$OPENBAO_INIT_VOL:/bao-init" \
        "$OPENBAO_INIT_IMAGE" - <<'PYEOF'
import json
import os
import subprocess
import sys

NAMESPACE = "system/"
SECRET_PATH = "config/system"
DEFAULT_URL = "http://ollama:11434"

with open("/bao-init/root-token") as _f:
    os.environ["BAO_TOKEN"] = _f.read().strip()

want = os.environ.get("OZ_OLLAMA_WANT", "")

result = subprocess.run(
    ["bao", "kv", "get", "-namespace=" + NAMESPACE, "-format=json", SECRET_PATH],
    capture_output=True, text=True,
    env={**os.environ, "BAO_TOKEN": os.environ["BAO_TOKEN"]},
)
if result.returncode != 0:
    if "not found" in result.stderr.lower() or "no value found" in result.stderr.lower():
        print("[ollama-seed] No existing system secret — starting empty", file=sys.stderr)
        existing, version = {}, 0
    else:
        sys.exit("FATAL: bao kv get failed: " + result.stderr.strip())
else:
    parsed = json.loads(result.stdout)
    existing = parsed.get("data", {}).get("data", {})
    version = parsed.get("data", {}).get("metadata", {}).get("version", 0)

if want and existing.get("OZ_OLLAMA_EMBED_URL") != want:
    existing["OZ_OLLAMA_EMBED_URL"] = want
    args = ["bao", "kv", "put", "-namespace=" + NAMESPACE]
    if version > 0:
        args.append("-cas=" + str(version))
    args.append(SECRET_PATH)
    for k, v in existing.items():
        args.append(k + "=" + str(v))
    result = subprocess.run(
        args, capture_output=True, text=True,
        env={**os.environ, "BAO_TOKEN": os.environ["BAO_TOKEN"]},
    )
    if result.returncode != 0:
        sys.exit("FATAL: bao kv put failed: " + result.stderr.strip())
    print("[ollama-seed] Seeded OZ_OLLAMA_EMBED_URL=" + want, file=sys.stderr)
else:
    print("[ollama-seed] OZ_OLLAMA_EMBED_URL already correct — no write", file=sys.stderr)

print(existing.get("OZ_OLLAMA_EMBED_URL", "") or DEFAULT_URL)
PYEOF
)"
}

# ── 10. Embeddings preflight probe (fail-fast before uvicorn) ───────────────
# The API lifespan prewarms embeddings and aborts boot when Ollama is
# unreachable — catch that here with an actionable message instead of a
# traceback after `make dev`.
probe_embeddings() {  # $1 = effective embed URL (resolved in section 9)
    if curl -sf "$1/api/tags" >/dev/null 2>&1; then
        log "Embeddings backend reachable at $1 (/api/tags OK)."
        return 0
    fi
    cat >&2 <<EOF
[dev_preflight] FATAL: Ollama not reachable at $1 (/api/tags failed).
The API lifespan prewarms embeddings and fail-fasts at boot without it.
Fix (native Ollama on host):
  ollama serve &
  ollama pull nomic-embed-text:v1.5
Then re-run: scripts/dev_preflight.sh up
EOF
    exit 1
}

up() {
    gen_secrets
    ensure_tooling_image
    ensure_postgres
    ensure_redis
    ensure_falkordb
    ensure_mailpit
    ensure_openbao
    # Unconditional every run: secret_ids rotate on each bootstrap, so .env must re-sync.
    bootstrap
    sync_env
    seed_host_ollama_embed_url
    probe_embeddings "$EFFECTIVE_EMBED_URL"
    log "Done. Dev deps up. API: uvicorn services.api.asgi:app --reload --host 0.0.0.0"
}

down() {
    for c in "$OPENBAO_CONTAINER" "$POSTGRES_CONTAINER" "$REDIS_CONTAINER" "$FALKORDB_CONTAINER" mailpit; do
        container_exists "$c" && { log "Stopping ${c} ..."; docker stop "$c" >/dev/null || true; }
    done
}

status() {
    if curl -sf "${BAO_ADDR}/v1/sys/health" 2>/dev/null | grep -q '"sealed":false'; then
        echo "OpenBao: UP (initialized+unsealed)"
    else
        echo "OpenBao: DOWN / sealed"
    fi
    if container_running "$POSTGRES_CONTAINER" && host_postgres_ping; then
        echo "Postgres: UP"
    elif container_running "$POSTGRES_CONTAINER"; then
        echo "Postgres: DEGRADED (port-less container — host :5432 dead; fix: docker rm -f $POSTGRES_CONTAINER)"
    else
        echo "Postgres: DOWN"
    fi
    if container_running "$REDIS_CONTAINER" && host_redis_ping 6379; then
        echo "Redis: UP (127.0.0.1:6379)"
    elif container_running "$REDIS_CONTAINER"; then
        echo "Redis: DEGRADED (port-less container — host :6379 dead; fix: docker rm -f $REDIS_CONTAINER)"
    else
        echo "Redis: DOWN"
    fi
    if container_running "$FALKORDB_CONTAINER" && host_redis_ping 6380; then
        echo "FalkorDB: UP (127.0.0.1:6380)"
    elif container_running "$FALKORDB_CONTAINER"; then
        echo "FalkorDB: DEGRADED (port-less container — host :6380 dead; fix: docker rm -f $FALKORDB_CONTAINER)"
    else
        echo "FalkorDB: DOWN"
    fi
    if container_exists mailpit && docker ps --format '{{.Names}}' | grep -qx mailpit; then
        echo "Mailpit: UP (SMTP 127.0.0.1:1025, UI http://127.0.0.1:8025)"
    else
        echo "Mailpit: DOWN"
    fi
}

case "${1:-up}" in
    up) up ;;
    down) down ;;
    status) status ;;
    *) echo "usage: $0 [up|down|status]"; exit 1 ;;
esac
