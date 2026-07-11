#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — Write Auto-Generated Database Credentials to OpenBao
# ──────────────────────────────────────────────────────────────────────────────
# One-shot init container for the `openbao-write-db` Compose service.
# Runs AFTER postgres-migrate (Alembic) and BEFORE api / worker start.
# Idempotent: safe to re-run — uses a marker file to skip completed setup.
#
# What this script does:
#   1. Waits for db-creds.json (from postgres-init)
#   2. Waits for root-token (from openbao-init)
#   3. Waits for OpenBao to be initialised + unsealed
#   4. Reads the existing combined `system` secret from OpenBao KV
#   5. Merges the new database_url into the existing data
#   6. Writes the merged result back as a single flat secret
#   7. Writes a marker file so re-runs become no-ops
#
# The OpenBao Agent (api.hcl / worker.hcl) reads system/config/data/system
# as a single secret and renders all keys to the env file. Adding the
# database_url to that same secret means api/worker will pick it up on
# the next render cycle.
#
# Dependencies (all expected in openbao/openbao Docker image):
#   - bao CLI
#   - python3
#   - jq (preferred for JSON manipulation, but python3 is the fallback)
# ──────────────────────────────────────────────────────────────────────────────
set -e
set -u
set -o pipefail

# ── Paths & defaults ─────────────────────────────────────────────────────────
BAO_INIT_DIR="${BAO_INIT_DIR:-/bao-init}"
DB_CREDS_FILE="${BAO_INIT_DIR}/db-creds.json"
ROOT_TOKEN_FILE="${BAO_INIT_DIR}/root-token"
WRITE_MARKER="${BAO_INIT_DIR}/db-creds-written"
BAO_ADDR="${BAO_ADDR:-http://openbao:8200}"
NAMESPACE="system/"
KV_SECRET_PATH="config/data/system"

# ── Logging helper ───────────────────────────────────────────────────────────
log() { echo "[write_db_to_openbao] $(date -Iseconds) $*"; }

# ── Error trap: log + exit 1, never write marker on failure ──────────────────
trap 'log "FATAL: script aborted on line ${LINENO} (exit code $?) — marker NOT written."; exit 1' ERR

# ── 1. Idempotency check (fast path for re-runs) ─────────────────────────────
if [ -f "${WRITE_MARKER}" ]; then
    log "Marker ${WRITE_MARKER} exists — database_url already written to OpenBao. Exiting."
    exit 0
fi

# ── 2. Verify required tools ─────────────────────────────────────────────────
for _cmd in bao python3; do
    if ! command -v "${_cmd}" >/dev/null 2>&1; then
        log "FATAL: Required tool '${_cmd}' is not available."
        exit 1
    fi
done

# ── 3. Wait for db-creds.json to appear (postgres-init may still be writing) ─
log "Waiting for ${DB_CREDS_FILE} to appear (postgres-init may still be writing) ..."
_i=1
while [ "${_i}" -le 60 ]; do
    if [ -f "${DB_CREDS_FILE}" ]; then
        log "Found ${DB_CREDS_FILE}."
        break
    fi
    if [ "${_i}" -eq 60 ]; then
        log "FATAL: ${DB_CREDS_FILE} did not appear within 60 attempts."
        exit 1
    fi
    _i=$((_i + 1))
    sleep 2
done

# ── 4. Wait for root-token to appear (openbao-init may still be writing) ─────
log "Waiting for ${ROOT_TOKEN_FILE} to appear ..."
_i=1
while [ "${_i}" -le 60 ]; do
    if [ -f "${ROOT_TOKEN_FILE}" ]; then
        log "Found ${ROOT_TOKEN_FILE}."
        break
    fi
    if [ "${_i}" -eq 60 ]; then
        log "FATAL: ${ROOT_TOKEN_FILE} did not appear within 60 attempts."
        exit 1
    fi
    _i=$((_i + 1))
    sleep 2
done

# ── 5. Wait for OpenBao to become reachable ──────────────────────────────────
log "Waiting for OpenBao at ${BAO_ADDR} to become reachable ..."
_i=1
while [ "${_i}" -le 60 ]; do
    _output=$(bao status -format=json 2>&1) || true
    if echo "${_output}" | python3 -c "import sys; json.load(sys.stdin)" >/dev/null 2>&1; then
        log "OpenBao is reachable."
        break
    fi
    if [ "${_i}" -eq 60 ]; then
        log "FATAL: OpenBao did not become reachable within 60 attempts."
        exit 1
    fi
    _i=$((_i + 1))
    sleep 2
done

# ── 6. Wait for OpenBao to be initialised AND unsealed ───────────────────────
log "Waiting for OpenBao to be initialised AND unsealed ..."
_i=1
while [ "${_i}" -le 60 ]; do
    if bao status -format=json 2>/dev/null | python3 -c "
import sys, json
s = json.load(sys.stdin)
if not s.get('initialized'):
    sys.exit(1)
if s.get('sealed'):
    sys.exit(1)
" 2>/dev/null; then
        log "OpenBao is initialised and unsealed."
        break
    fi
    if [ "${_i}" -eq 60 ]; then
        log "FATAL: OpenBao did not become initialised+unsealed within 60 attempts."
        exit 1
    fi
    _i=$((_i + 1))
    sleep 2
done

# ── 7. Authenticate with root token ──────────────────────────────────────────
BAO_TOKEN=$(cat "${ROOT_TOKEN_FILE}")
export BAO_TOKEN
log "Authenticated with root token."

# ── 8. Read DB credentials, construct DATABASE_URL, merge into system secret ─
#
# The structure of /bao-init/db-creds.json (written by init_postgres.sh):
#   {
#     "migrator": "<password>",
#     "app": "<password>",
#     "migrator_user": "openzync_migrator",
#     "app_user": "openzync_app",
#     "db_host": "postgres",
#     "db_port": 5432,
#     "db_name": "openzync",
#     "generated_at": "2026-..."
#   }
#
# The OpenBao KV v2 secret at system/config/data/system holds the merged
# system config; the Agent reads it and renders all keys to the env file.
log "Reading database credentials + merging into ${NAMESPACE}${KV_SECRET_PATH} ..."

python3 <<'PYEOF'
import json
import os
import subprocess
import sys

CREDS_FILE = "/bao-init/db-creds.json"
NAMESPACE = "system/"
SECRET_PATH = "config/data/system"

# ── 1. Read the db-creds.json (written by init_postgres.sh) ─────────────────
with open(CREDS_FILE) as f:
    creds = json.load(f)

for key in ("app", "app_user", "db_host", "db_port", "db_name"):
    if key not in creds:
        sys.exit("FATAL: db-creds.json missing required field: " + key)

database_url = (
    "postgresql+asyncpg://"
    + str(creds["app_user"])
    + ":"
    + str(creds["app"])
    + "@"
    + str(creds["db_host"])
    + ":"
    + str(creds["db_port"])
    + "/"
    + str(creds["db_name"])
)
print("[merge] database_url constructed (password redacted from logs)")

# ── 2. Read the existing `system` secret from OpenBao (if any) ──────────────
result = subprocess.run(
    ["bao", "kv", "get", "-namespace=" + NAMESPACE, "-format=json", SECRET_PATH],
    capture_output=True, text=True,
    env={**os.environ, "BAO_TOKEN": os.environ["BAO_TOKEN"]},
)
if result.returncode != 0:
    # Secret doesn't exist yet — start with an empty dict
    if "not found" in result.stderr.lower() or "no value found" in result.stderr.lower():
        print("[merge] No existing system secret — starting with empty dict")
        existing = {}
        version = 0
    else:
        sys.exit("FATAL: bao kv get failed: " + result.stderr.strip())
else:
    # KV v2 wraps the data: {"data": {"data": {<our keys>}, "metadata": {"version": N}}}
    parsed = json.loads(result.stdout)
    existing = parsed.get("data", {}).get("data", {})
    version = parsed.get("data", {}).get("metadata", {}).get("version", 0)
    print("[merge] Read existing system secret with " + str(len(existing)) + " keys (version " + str(version) + ")")

# ── 3. Merge: add/overwrite database_url ───────────────────────────────────
existing["database_url"] = database_url
print("[merge] Merged system secret now has " + str(len(existing)) + " keys")

# ── 4. Write the merged result back to OpenBao as a single flat secret ──────
# KV v2 syntax: bao kv put <path> key1=value1 key2=value2 ...
# We pass each key=value as a separate argv to avoid any shell-interpolation
# issues with passwords containing $ or &.
args = ["bao", "kv", "put", "-namespace=" + NAMESPACE]
# CAS with version=0 means "create only if doesn't exist" —
# that would block initial writes. Skip the flag to allow bootstrap.
if version > 0:
    args.append("-cas=" + str(version))
args.append(SECRET_PATH)
for k, v in existing.items():
    args.append(k + "=" + str(v))

result = subprocess.run(
    args,
    capture_output=True, text=True,
    env={**os.environ, "BAO_TOKEN": os.environ["BAO_TOKEN"]},
)
if result.returncode != 0:
    sys.exit("FATAL: bao kv put failed: " + result.stderr.strip())
print("[merge] Wrote merged system secret to OpenBao")

# ── 5. Verify the write by reading it back ───────────────────────────────────
result = subprocess.run(
    ["bao", "kv", "get", "-namespace=" + NAMESPACE, "-format=json", SECRET_PATH],
    capture_output=True, text=True,
    env={**os.environ, "BAO_TOKEN": os.environ["BAO_TOKEN"]},
)
if result.returncode != 0:
    sys.exit("FATAL: bao kv get (verify) failed: " + result.stderr.strip())

parsed = json.loads(result.stdout)
written = parsed.get("data", {}).get("data", {})
if "database_url" not in written:
    sys.exit("FATAL: database_url not present in verified secret")
if written["database_url"] != database_url:
    sys.exit("FATAL: written database_url does not match the constructed one")
print("[merge] Read-back verification succeeded — database_url is in the secret (version " + str(parsed.get("data", {}).get("metadata", {}).get("version", "unknown")) + ")")
PYEOF

# ── 9. Write marker file (only after every step succeeded) ──────────────────
date > "${WRITE_MARKER}"
chmod 600 "${WRITE_MARKER}"
log "Marker written to ${WRITE_MARKER} — future runs will skip."

# ── 10. Done ────────────────────────────────────────────────────────────────
log "═══════════════════════════════════════════════════════════════════"
log " openbao-write-db complete!"
log "═══════════════════════════════════════════════════════════════════"
log "  Merged database_url into: ${NAMESPACE}${KV_SECRET_PATH}"
log "  Marker:                   ${WRITE_MARKER}"
log ""
log "  The OpenBao Agent sidecars will pick up the new database_url on"
log "  their next render cycle (max 5 minutes per static_secret_render_interval)."
log "  Api and worker services depend on this container completing first,"
log "  so the very first render will already include the database_url."
log "═══════════════════════════════════════════════════════════════════"
log "Done."
