#!/bin/sh
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — Migration Container Entrypoint
# ──────────────────────────────────────────────────────────────────────────────
# Runs in the `postgres-migrate` Compose service as PID 1.
#
# Reads DATABASE_URL / OZ_DATABASE_URL straight from the environment
# (compose env_file: ../.env) and execs the CMD (typically `alembic upgrade
# head`). Use the MIGRATOR url here (openzync_migrator, DDL grant) — it
# differs from the app url the api/worker use.
#
# Deps (in python:3.12-slim): sh, sed.
# ──────────────────────────────────────────────────────────────────────────────
set -e
set -u

log() { echo "[entrypoint_migrate] $(date -Iseconds) $*"; }
trap 'log "FATAL: unexpected error on line $LINENO (rc=$?)"; exit 1' ERR

# ── 1. Resolve DATABASE_URL from the environment ───────────────────────────
if [ -n "${DATABASE_URL:-}" ]; then
    OZ_DATABASE_URL="${DATABASE_URL}"
elif [ -n "${OZ_DATABASE_URL:-}" ]; then
    DATABASE_URL="${OZ_DATABASE_URL}"
else
    log "FATAL: neither DATABASE_URL nor OZ_DATABASE_URL is set — refusing to migrate."
    exit 1
fi
export DATABASE_URL OZ_DATABASE_URL

# Sanity: print the URL with the password redacted.
log "DATABASE_URL set: $(echo "$DATABASE_URL" | sed 's|://[^:]*:[^@]*@|://***:***@|')"

# ── 2. Exec the CMD (e.g. `alembic upgrade head`) as PID 1 ─────────────────
# exec replaces the shell so the child process receives SIGTERM directly
# (graceful shutdown for `docker compose stop`).
log "Executing: $*"
exec "$@"
