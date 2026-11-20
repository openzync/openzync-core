#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — Local Alembic wrapper (dev only)
# ──────────────────────────────────────────────────────────────────────────────
# Resolves OZ_DATABASE_URL from the environment (export it first, e.g.
# `set -a && source .env && set +a`), then execs alembic. This keeps
# `make migrate` off the stale alembic.ini fallback password
# (openzync:openzync) and on the real migrator/app credentials in .env.
#
# Any resolution failure exits nonzero BEFORE alembic runs.
#
# Deps: bash, alembic.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

log() { echo "[migrate] $(date -Iseconds) $*"; }
trap 'log "FATAL: unexpected error on line $LINENO (rc=$?)"; exit 1' ERR

# ── 1. Resolve OZ_DATABASE_URL from the environment ─────────────────────────
if [ -n "${OZ_DATABASE_URL:-}" ]; then
    :
elif [ -n "${DATABASE_URL:-}" ]; then
    OZ_DATABASE_URL="${DATABASE_URL}"
    export OZ_DATABASE_URL
else
    log "FATAL: neither OZ_DATABASE_URL nor DATABASE_URL is set."
    log "FATAL: export them first (e.g. 'set -a && source .env && set +a')."
    exit 1
fi

if [ -z "${OZ_DATABASE_URL:-}" ]; then
    log "FATAL: resolved OZ_DATABASE_URL is empty — refusing to run alembic."
    exit 1
fi

# Sanity: print the URL with the password redacted (never echo the secret).
log "OZ_DATABASE_URL set: $(printf '%s' "$OZ_DATABASE_URL" | sed 's|://[^:]*:[^@]*@|://***:***@|')"

# ── 2. Exec alembic as PID 1 ──────────────────────────────────────────────────
# exec replaces the shell so signals reach alembic directly.
log "Executing: alembic $*"
exec alembic "$@"
