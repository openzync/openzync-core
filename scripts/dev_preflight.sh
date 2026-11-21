#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — Host-Dev preflight (postgres + redis + falkordb + mailpit)
# ──────────────────────────────────────────────────────────────────────────────
# The daily dev dependency script. Idempotent — safe to run every morning.
#
# Usage:
#   scripts/dev_preflight.sh [up|down|status]   (default: up)
#
#   up      ensure postgres + redis + falkordb + mailpit containers,
#           re-sync the full runtime .env, apply pending Alembic migrations.
#   down    stop the dev containers (data volumes are preserved).
#   status  one-line health check per dependency.
#
# Secrets:
#   - Stable OZ_SECRET_KEY / postgres password persist in
#     scripts/.dev_preflight_secrets.env (0600). Generated on first run;
#     reused afterwards so JWTs and encrypted payloads survive restarts.
#     (Webhook signing is per-endpoint via core/crypto — no global secret.)
#   - .env is the full runtime env (OZ_DATABASE_URL, OZ_REDIS_URL,
#     OZ_FALKORDB_URL, secrets, SMTP, ...), re-synced from the secrets file
#     plus host-local service URLs on every run.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
SECRETS_FILE="${REPO_ROOT}/scripts/.dev_preflight_secrets.env"

POSTGRES_CONTAINER="openzync-dev-postgres"
POSTGRES_IMAGE="pgvector/pgvector:pg15"   # postgres 15 + pgvector (app uses vector extension)
POSTGRES_DATA_VOL="openzync-dev-postgres-data"
REDIS_CONTAINER="openzync-dev-redis"
REDIS_IMAGE="redis:7-alpine"
REDIS_DATA_VOL="openzync-dev-redis-data"
FALKORDB_CONTAINER="openzync-dev-falkordb"
FALKORDB_IMAGE="falkordb/falkordb:v4.20.1-alpine"   # pinned — :latest breaks repro
FALKORDB_DATA_VOL="openzync-dev-falkordb-data"

log() { echo "[dev_preflight] $(date -Iseconds) $*"; }

container_exists() { docker ps -a --format '{{.Names}}' | grep -qx "$1"; }
container_running() { docker ps --format '{{.Names}}' | grep -qx "$1"; }

ensure_volume() { docker volume inspect "$1" >/dev/null 2>&1 || docker volume create "$1" >/dev/null; }

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

# ── 1. Stable secrets (OZ_SECRET_KEY, SMTP block) ────────────
# OZ_DATABASE_URL is appended by ensure_postgres() on first container creation;
# on a pre-existing postgres container it must already be present in the file.
gen_secrets() {
    umask 077
    if [ ! -f "$SECRETS_FILE" ]; then
        log "Generating initial secrets -> ${SECRETS_FILE}"
        {
            echo "OZ_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
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
    # pgvector in POSTGRES_IMAGE is not a trusted extension (superuser-only) and
    # the openzync role is not superuser, so the DB owner cannot create it —
    # every app connection would then die in the pgvector codec with
    # "unknown type: public.vector". Same one-liner as scripts/init_postgres.sh.
    docker exec "$POSTGRES_CONTAINER" psql -U postgres -d openzync -v ON_ERROR_STOP=1 \
        -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null
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
        # or the compose stack) is holding it.
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
        # Fail fast if another process holds :6380.
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

# ── 6. Re-sync the full runtime .env ────────────────────────────────────────
# .env is the single source `make dev` / `make worker` source into the
# environment: stable secrets from the secrets file plus host-local URLs
# for the containers ensured above.
sync_env() {
    python3 - "$ENV_FILE" "$SECRETS_FILE" <<'PY'
import sys

env_path, secrets_path = sys.argv[1], sys.argv[2]
vals = {}
with open(secrets_path) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k] = v
for required in ("OZ_SECRET_KEY", "OZ_DATABASE_URL"):
    if not vals.get(required):
        sys.exit(
            f"FATAL: {required} missing in {secrets_path} — delete the file "
            "and re-run to regenerate, or add the key manually."
        )

runtime = {
    # Stable secrets — generated once, reused so JWTs survive restarts.
    "OZ_SECRET_KEY": vals["OZ_SECRET_KEY"],
    "OZ_DATABASE_URL": vals["OZ_DATABASE_URL"],
    # Host-local service URLs (containers publish to 127.0.0.1).
    "OZ_REDIS_URL": "redis://localhost:6379/0",
    "OZ_FALKORDB_URL": vals.get("OZ_FALKORDB_URL", "redis://localhost:6380"),
    "OZ_ENVIRONMENT": vals.get("OZ_ENVIRONMENT", "development"),
    "OZ_LOG_LEVEL": vals.get("OZ_LOG_LEVEL", "INFO"),
    "OZ_CORS_ORIGINS": vals.get("OZ_CORS_ORIGINS", "http://localhost:3000"),
    "OZ_HOSTS_ALLOWED": vals.get("OZ_HOSTS_ALLOWED", "localhost:8000"),
    "DATABASE_URL": vals["OZ_DATABASE_URL"],
}
for k in (
    "OZ_SMTP_HOST", "OZ_SMTP_PORT", "OZ_SMTP_USERNAME", "OZ_SMTP_PASSWORD",
    "OZ_SMTP_FROM_ADDR", "OZ_SMTP_USE_TLS", "OZ_SMTP_START_TLS",
):
    if k in vals:
        runtime[k] = vals[k]

with open(env_path, "w") as f:
    f.write("# Full runtime env — regenerated by scripts/dev_preflight.sh. 0600.\n")
    for k, v in runtime.items():
        f.write(f"{k}={v}\n")
print(f"  Wrote {len(runtime)} keys to {env_path}")
PY
    chmod 600 "$ENV_FILE"
}

# ── 7. Host-dev Ollama embed URL seed (native Ollama on host) ───────────────
# Host-run uvicorn cannot resolve the compose-DNS name `ollama`, so the
# default OLLAMA_EMBED_URL (http://ollama:11434) fail-fasts the lifespan
# prewarm with DNS [Errno -2]. When a NATIVE Ollama answers on
# 127.0.0.1:11434 but `ollama` does not resolve from the host, upsert
# OZ_OLLAMA_EMBED_URL=http://127.0.0.1:11434 into .env. Idempotent: skips
# the write when the stored value already matches.
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
            log "Native Ollama up but 'ollama' unresolvable — upserting into .env ..."
        fi
    else
        log "No native Ollama on 127.0.0.1:11434 — leaving embed URL as stored."
    fi
    if [ -n "$want" ]; then
        python3 - "$ENV_FILE" "$want" <<'PY'
import sys

path, want = sys.argv[1], sys.argv[2]
lines, seen = [], False
with open(path) as f:
    for line in f:
        if line.startswith("OZ_OLLAMA_EMBED_URL="):
            lines.append(f"OZ_OLLAMA_EMBED_URL={want}\n")
            seen = True
        else:
            lines.append(line)
if not seen:
    lines.append(f"OZ_OLLAMA_EMBED_URL={want}\n")
with open(path, "w") as f:
    f.writelines(lines)
print(f"[ollama-seed] OZ_OLLAMA_EMBED_URL={want}", file=sys.stderr)
PY
        EFFECTIVE_EMBED_URL="$want"
    else
        EFFECTIVE_EMBED_URL="$(sed -n 's/^OZ_OLLAMA_EMBED_URL=//p' "$ENV_FILE" | tail -n 1)"
        EFFECTIVE_EMBED_URL="${EFFECTIVE_EMBED_URL:-http://ollama:11434}"
    fi
}

# ── 8. Embeddings preflight probe (fail-fast before uvicorn) ───────────────
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

# ── 9. Alembic migrations (schema must exist before uvicorn boots) ───────────
# migrate.sh reads OZ_DATABASE_URL from the environment and execs alembic —
# .env (just synced above) provides it. Failure aborts the whole preflight
# (set -e): a schema-less database is not "deps up", and the API would only
# log platform_seed.failed — degraded.
apply_migrations() {
    if ! command -v alembic >/dev/null 2>&1; then
        log "FATAL: alembic not on PATH — activate the venv first: source .venv/bin/activate"
        exit 1
    fi
    log "Applying Alembic migrations ..."
    (
        cd "$REPO_ROOT"
        set -a
        # shellcheck disable=SC1090
        . "$ENV_FILE"
        set +a
        ./scripts/migrate.sh upgrade head
    )
}

up() {
    gen_secrets
    ensure_postgres
    ensure_redis
    ensure_falkordb
    ensure_mailpit
    # Unconditional every run: .env is regenerated from stable secrets +
    # current host-local service URLs.
    sync_env
    apply_migrations
    seed_host_ollama_embed_url
    probe_embeddings "$EFFECTIVE_EMBED_URL"
    log "Done. Dev deps up. API: uvicorn services.api.asgi:app --reload --host 0.0.0.0"
}

down() {
    for c in "$POSTGRES_CONTAINER" "$REDIS_CONTAINER" "$FALKORDB_CONTAINER" mailpit; do
        container_exists "$c" && { log "Stopping ${c} ..."; docker stop "$c" >/dev/null || true; }
    done
}

status() {
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
