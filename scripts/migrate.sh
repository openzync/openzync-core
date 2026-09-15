#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — Local Alembic wrapper (dev only)
# ──────────────────────────────────────────────────────────────────────────────
# Resolves OZ_DATABASE_URL from the running OpenBao `system` secret, exports
# it, then execs alembic. This keeps `make migrate` off the stale
# alembic.ini fallback password (openzync:openzync) and on the real
# migrator/app credentials OpenBao holds.
#
# Bootstrap credentials (OZ_OPENBAO_ADDR, OZ_OPENBAO_ROLE_ID,
# OZ_OPENBAO_SECRET_ID) must already be exported in the environment — e.g.
# via `set -a && source .env && set +a` after `make dev` seeds .env.
# They are never defaulted here and there is no alembic.ini fallback:
# any resolution failure exits nonzero BEFORE alembic runs.
#
# Deps: bash, python3 (stdlib + repo core.openbao only), alembic.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

log() { echo "[migrate] $(date -Iseconds) $*"; }
trap 'log "FATAL: unexpected error on line $LINENO (rc=$?)"; exit 1' ERR

# ── 1. Bootstrap credentials must be exported ─────────────────────────────────
if [ -z "${OZ_OPENBAO_ADDR:-}" ] || [ -z "${OZ_OPENBAO_ROLE_ID:-}" ] || [ -z "${OZ_OPENBAO_SECRET_ID:-}" ]; then
    log "FATAL: missing OpenBao bootstrap credentials (need OZ_OPENBAO_ADDR, OZ_OPENBAO_ROLE_ID, OZ_OPENBAO_SECRET_ID exported)."
    log "FATAL: export them first (e.g. 'set -a && source .env && set +a') or run 'make dev' once to seed .env."
    exit 1
fi

# ── 2. Resolve OZ_DATABASE_URL via python3 (defence-in-depth: the secret
#       value never passes through shell interpolation, only stdout capture) ──
log "Resolving OZ_DATABASE_URL from OpenBao ..."
OZ_DATABASE_URL=$(python3 <<'PYEOF'
import asyncio
import os
import sys

from core.openbao import OpenBaoClient


async def _main() -> None:
    addr = os.environ.get("OZ_OPENBAO_ADDR", "")
    role_id = os.environ.get("OZ_OPENBAO_ROLE_ID", "")
    secret_id = os.environ.get("OZ_OPENBAO_SECRET_ID", "")
    if not addr or not role_id or not secret_id:
        print(
            "FATAL: missing OpenBao bootstrap credentials "
            "(need OZ_OPENBAO_ADDR, OZ_OPENBAO_ROLE_ID, "
            "OZ_OPENBAO_SECRET_ID exported)",
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        async with OpenBaoClient(addr, role_id, secret_id) as bao:
            config = await bao.read_system_config()
    except Exception as exc:
        print(f"FATAL: cannot read OpenBao system secret: {exc}", file=sys.stderr)
        raise SystemExit(1)
    url = config.get("OZ_DATABASE_URL")
    if not url:
        print(
            "FATAL: OZ_DATABASE_URL missing or empty in OpenBao system secret",
            file=sys.stderr,
        )
        raise SystemExit(1)
    sys.stdout.write(str(url))


asyncio.run(_main())
PYEOF
)
export OZ_DATABASE_URL

if [ -z "${OZ_DATABASE_URL:-}" ]; then
    log "FATAL: resolved OZ_DATABASE_URL is empty — refusing to run alembic."
    exit 1
fi

# Sanity: print the URL with the password redacted (never echo the secret).
log "OZ_DATABASE_URL set: $(printf '%s' "$OZ_DATABASE_URL" | sed 's|://[^:]*:[^@]*@|://***:***@|')"

# ── 3. Exec alembic as PID 1 ──────────────────────────────────────────────────
# exec replaces the shell so signals reach alembic directly.
log "Executing: alembic $*"
exec alembic "$@"
