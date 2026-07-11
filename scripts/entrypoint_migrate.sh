#!/bin/sh
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — Migration Container Entrypoint
# ──────────────────────────────────────────────────────────────────────────────
# Runs in the `postgres-migrate` Compose service as PID 1.
#
# This entrypoint bridges postgres-init's credential file to Alembic. It
# reads the migrator password from /bao-init/db-creds.json (written by
# scripts/init_postgres.sh), constructs DATABASE_URL using the migrator
# credentials, exports it, then execs the CMD (typically `alembic upgrade
# head`). This avoids the need for a dedicated OpenBao Agent sidecar for
# the migration container — the migrator password is read directly from
# the shared init-data volume.
#
# Why no Agent sidecar here?
#   Migrations need the MIGRATOR URL (openzync_migrator user, DDL grant),
#   not the APP URL. The api/worker Agents render DATABASE_URL using the
#   app user. Reusing them would mean either (a) widening the Agent
#   template to render two different URLs, or (b) forking the AppRole
#   policy. Both are heavier than simply reading the creds file the
#   postgres-init container already wrote to the shared volume.
#
# Deps (in python:3.12-slim): sh, sed, sleep, python3, alembic.
# ──────────────────────────────────────────────────────────────────────────────
set -e
set -u
set -o pipefail

log() { echo "[entrypoint_migrate] $(date -Iseconds) $*"; }
trap 'log "FATAL: unexpected error on line $LINENO (rc=$?)"; exit 1' ERR

CREDS_FILE="/bao-init/db-creds.json"
TIMEOUT_SEC=60

# ── 1. Wait for db-creds.json (postgres-init may still be writing) ───────────
log "Waiting for ${CREDS_FILE} to appear (postgres-init may still be running) ..."
_i=0
while [ "$_i" -lt "$TIMEOUT_SEC" ]; do
    if [ -s "${CREDS_FILE}" ]; then
        break
    fi
    _i=$((_i + 1))
    sleep 1
done
if [ "$_i" -ge "$TIMEOUT_SEC" ]; then
    log "FATAL: ${CREDS_FILE} did not appear within ${TIMEOUT_SEC}s"
    exit 1
fi

# ── 2. Build DATABASE_URL via python3 (defence-in-depth: never interpolate
#       the password via the shell) ──────────────────────────────────────────
log "Reading migrator credentials from ${CREDS_FILE} ..."
DATABASE_URL=$(python3 <<'PYEOF'
import json
import sys

with open("/bao-init/db-creds.json") as f:
    creds = json.load(f)

required = ("migrator", "migrator_user", "db_host", "db_port", "db_name")
for key in required:
    if key not in creds:
        sys.exit("FATAL: db-creds.json missing required field: " + key)

# Migrations need the DDL-bearing migrator user. asyncpg is the driver
# the api/worker already use; the alembic env.py reads sqlalchemy.url
# from the same env var, so the URL is consistent end-to-end.
print(
    "postgresql+asyncpg://"
    + str(creds["migrator_user"])
    + ":"
    + str(creds["migrator"])
    + "@"
    + str(creds["db_host"])
    + ":"
    + str(creds["db_port"])
    + "/"
    + str(creds["db_name"])
)
PYEOF
)
export DATABASE_URL

# Sanity: print the URL with the password redacted.
log "DATABASE_URL set: $(echo "$DATABASE_URL" | sed 's|://[^:]*:[^@]*@|://***:***@|')"

# ── 3. Exec the CMD (e.g. `alembic upgrade head`) as PID 1 ──────────────────
# exec replaces the shell so the child process receives SIGTERM directly
# (graceful shutdown for `docker compose stop`).
log "Executing: $*"
exec "$@"
