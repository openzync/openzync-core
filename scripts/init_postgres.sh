#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — Postgres Bootstrap Initialisation Script
# ──────────────────────────────────────────────────────────────────────────────
# Run once inside the pgvector/pgvector:pg15 container on a fresh data volume.
# Idempotent: safe to re-run — rotates migrator/app passwords and re-applies
# GRANTs. The postgres superuser password is rotated only when the script
# auto-generates it (the operator's env-provided password is left untouched
# so subsequent container restarts continue to work).
#
# Dependencies (all present in pgvector/pgvector:pg15):
#   - psql, pg_isready   (postgresql-client)
#   - openssl            (password generation)
#   - python3            (safe JSON construction)
#   - bash               (for `set -o pipefail`)
# ──────────────────────────────────────────────────────────────────────────────
set -e
set -u
set -o pipefail

# ── Paths & defaults ─────────────────────────────────────────────────────────
DB_INIT_DIR="${DB_INIT_DIR:-/bao-init}"
CREDS_FILE="${DB_INIT_DIR}/db-creds.json"
POSTGRES_HOST="${POSTGRES_HOST:-postgres}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
DB_NAME="${DB_NAME:-openzync}"
MIGRATOR_USER="openzync_migrator"
APP_USER="openzync_app"
PG_RETRY_MAX=60
PG_RETRY_DELAY=2

# ── Logging helper ───────────────────────────────────────────────────────────
log() { echo "[init_postgres] $(date -Iseconds) $*"; }

# ── Tool check ───────────────────────────────────────────────────────────────
for _cmd in psql pg_isready openssl python3; do
    if ! command -v "${_cmd}" >/dev/null 2>&1; then
        log "FATAL: Required tool '${_cmd}' is not available in PATH."
        exit 1
    fi
done

mkdir -p "${DB_INIT_DIR}"

# ── Error trap: log clearly, do NOT write creds file on failure ──────────────
trap 'rc=$?; log "FATAL: error on line ${LINENO} (rc=${rc}) — aborting. Credentials file NOT written."; exit 1' ERR

# ── 1. Wait for Postgres to become reachable ─────────────────────────────────
log "Waiting for Postgres at ${POSTGRES_HOST}:${POSTGRES_PORT} as 'postgres' ..."
_i=1
while [ "${_i}" -le "${PG_RETRY_MAX}" ]; do
    if pg_isready -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U postgres -q; then
        log "Postgres is reachable."
        break
    fi
    if [ "${_i}" -eq "${PG_RETRY_MAX}" ]; then
        log "FATAL: Postgres did not become reachable within ${PG_RETRY_MAX} attempts ($((PG_RETRY_MAX * PG_RETRY_DELAY))s)."
        exit 1
    fi
    log "  attempt ${_i}/${PG_RETRY_MAX}: not ready yet, sleeping ${PG_RETRY_DELAY}s"
    _i=$((_i + 1))
    sleep "${PG_RETRY_DELAY}"
done

# ── 2. Determine superuser password source ───────────────────────────────────
if [ -n "${POSTGRES_SUPERUSER_PASSWORD:-}" ]; then
    log "Superuser password sourced from POSTGRES_SUPERUSER_PASSWORD env var (operator-provided) — no rotation."
    OP_SUPERUSER_PASSWORD="${POSTGRES_SUPERUSER_PASSWORD}"
    _SUPERUSER_FROM_ENV=1
else
    log "POSTGRES_SUPERUSER_PASSWORD not set — auto-generating 32-byte base64 password and rotating the postgres superuser."
    OP_SUPERUSER_PASSWORD=$(openssl rand -base64 32)
    _SUPERUSER_FROM_ENV=0
fi

# ── 3. Always generate fresh migrator + app passwords (rotation-friendly) ────
log "Generating fresh migrator and app passwords (32-byte base64 each) ..."
OP_MIGRATOR_PASSWORD=$(openssl rand -base64 32)
OP_APP_PASSWORD=$(openssl rand -base64 32)

# ── 4. Set PGPASSWORD for all subsequent psql calls ─────────────────────────
#      (kept in-memory only; never on the command line)
export PGPASSWORD="${OP_SUPERUSER_PASSWORD}"

# ── 5. Idempotency check: does the openzync database already exist? ──────────
log "Checking for existing database '${DB_NAME}' ..."
_DB_EXISTS=$(psql \
    -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U postgres -d postgres \
    -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" 2>/dev/null || true)

if [ "${_DB_EXISTS}" = "1" ]; then
    log "Database '${DB_NAME}' already exists — entering rotation/re-apply mode (passwords rotated, GRANTs re-applied)."
    _DB_FRESH=0
else
    log "Database '${DB_NAME}' does not exist — will CREATE DATABASE, CREATE roles, and apply GRANTs."
    _DB_FRESH=1
fi

# ── 6. Rotate postgres superuser (only if we auto-generated the password) ───
if [ "${_SUPERUSER_FROM_ENV}" -eq 0 ]; then
    log "Rotating postgres superuser password ..."
    psql -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U postgres -d postgres \
        -v ON_ERROR_STOP=1 \
        -c "ALTER USER postgres WITH ENCRYPTED PASSWORD '${OP_SUPERUSER_PASSWORD}';"
fi

# ── 7. CREATE DATABASE (only on first run) ──────────────────────────────────
if [ "${_DB_FRESH}" -eq 1 ]; then
    log "Creating database '${DB_NAME}' ..."
    psql -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U postgres -d postgres \
        -v ON_ERROR_STOP=1 \
        -c "CREATE DATABASE \"${DB_NAME}\";"
fi

# ── 8. CREATE / ALTER role openzync_migrator (idempotent) ───────────────────
log "Ensuring role '${MIGRATOR_USER}' exists with fresh password ..."
psql -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U postgres -d postgres \
    -v ON_ERROR_STOP=1 <<-EOSQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '${MIGRATOR_USER}') THEN
        CREATE USER ${MIGRATOR_USER} WITH ENCRYPTED PASSWORD '${OP_MIGRATOR_PASSWORD}';
    ELSE
        ALTER USER ${MIGRATOR_USER} WITH ENCRYPTED PASSWORD '${OP_MIGRATOR_PASSWORD}';
    END IF;
END
\$\$;
EOSQL

# ── 9. CREATE / ALTER role openzync_app (idempotent) ────────────────────────
log "Ensuring role '${APP_USER}' exists with fresh password ..."
psql -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U postgres -d postgres \
    -v ON_ERROR_STOP=1 <<-EOSQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '${APP_USER}') THEN
        CREATE USER ${APP_USER} WITH ENCRYPTED PASSWORD '${OP_APP_PASSWORD}';
    ELSE
        ALTER USER ${APP_USER} WITH ENCRYPTED PASSWORD '${OP_APP_PASSWORD}';
    END IF;
END
\$\$;
EOSQL

# ── 10. Apply GRANTs inside the openzync database ───────────────────────────
log "Applying GRANTs in database '${DB_NAME}' (migrator: DDL, app: CRUD) ..."
psql -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U postgres -d "${DB_NAME}" \
    -v ON_ERROR_STOP=1 <<-EOSQL
-- Migrator: full DDL on database + schema, default privileges for future objects
GRANT ALL ON DATABASE "${DB_NAME}" TO ${MIGRATOR_USER};
GRANT ALL ON SCHEMA public TO ${MIGRATOR_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO ${MIGRATOR_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO ${MIGRATOR_USER};

-- App: connect + CRUD on current objects, default privileges for future
--      objects created by the migrator role.
GRANT CONNECT ON DATABASE "${DB_NAME}" TO ${APP_USER};
GRANT USAGE ON SCHEMA public TO ${APP_USER};
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ${APP_USER};
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ${APP_USER};
ALTER DEFAULT PRIVILEGES FOR ROLE ${MIGRATOR_USER} IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ${APP_USER};
ALTER DEFAULT PRIVILEGES FOR ROLE ${MIGRATOR_USER} IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO ${APP_USER};
EOSQL

# ── 11. Write credentials JSON (mode 0600, built safely via python3) ────────
GENERATED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

log "Writing credentials to ${CREDS_FILE} (mode 0600) ..."
python3 - "${CREDS_FILE}" \
    "${MIGRATOR_USER}" \
    "${APP_USER}" \
    "${OP_MIGRATOR_PASSWORD}" \
    "${OP_APP_PASSWORD}" \
    "${POSTGRES_HOST}" \
    "${POSTGRES_PORT}" \
    "${DB_NAME}" \
    "${GENERATED_AT}" <<-'PYEOF'
"""
Build the credentials JSON safely.

The script-injected argv values are passed positionally (not interpolated into
Python source), so no escaping is required. The file is opened with mode 0o600
atomically via os.open() to avoid any race with the process umask.
"""
import json
import os
import sys

creds_file      = sys.argv[1]
migrator_user   = sys.argv[2]
app_user        = sys.argv[3]
migrator_pw     = sys.argv[4]
app_pw          = sys.argv[5]
db_host         = sys.argv[6]
db_port         = int(sys.argv[7])
db_name         = sys.argv[8]
generated_at    = sys.argv[9]

payload = {
    "migrator": migrator_pw,
    "app": app_pw,
    "migrator_user": migrator_user,
    "app_user": app_user,
    "db_host": db_host,
    "db_port": db_port,
    "db_name": db_name,
    "generated_at": generated_at,
}

fd = os.open(
    creds_file,
    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
    0o600,
)
with os.fdopen(fd, "w") as f:
    json.dump(payload, f, indent=2)
    f.write("\n")
PYEOF
chmod 600 "${CREDS_FILE}"

# ── 12. Clear passwords from memory ─────────────────────────────────────────
unset OP_SUPERUSER_PASSWORD OP_MIGRATOR_PASSWORD OP_APP_PASSWORD PGPASSWORD

# ── 13. Done ────────────────────────────────────────────────────────────────
log "═══════════════════════════════════════════════════════════════════"
log " init_postgres complete!"
log "═══════════════════════════════════════════════════════════════════"
log "  Credentials: ${CREDS_FILE}  (mode 0600)"
log "  Database:    ${DB_NAME}@${POSTGRES_HOST}:${POSTGRES_PORT}"
log "  Roles:       ${MIGRATOR_USER} (DDL), ${APP_USER} (CRUD)"
log "  Generated:   ${GENERATED_AT}"
log "═══════════════════════════════════════════════════════════════════"
log "Done."
