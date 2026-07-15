#!/bin/sh
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — OpenBao Bootstrap Initialisation Script
# ──────────────────────────────────────────────────────────────────────────────
# Run once on a fresh OpenBao data volume.
# Idempotent: safe to re-run — uses a marker file to skip completed setup.
#
# What this script does:
#   1. Initialises + unseals OpenBao (or unseals an existing instance).
#   2. Creates the 'system' namespace and mounts KV v2 at system/config.
#   3. Writes a SINGLE combined system secret to system/config/data/system
#      containing all OZ_* env vars (UPPERCASE keys) — EXCEPT DATABASE_URL.
#      The database URL is intentionally NOT written here, because postgres
#      may not be up yet at this point. It is appended later by
#      scripts/write_db_to_openbao.sh once postgres is healthy.
#   4. Enables AppRole auth, writes ACL policies, creates the
#      'openzync-app' and 'openzync-worker' AppRoles, and enables the
#      Transit engine with the standard encryption keys.
#   5. Writes the AppRole role_id and secret_id to four files in
#      /bao-init/ (the shared volume with the api/worker Agent sidecars):
#         api-role_id, api-secret_id, worker-role_id, worker-secret_id
#      The OpenBao Agent reads these files on startup, authenticates, and
#      renders system/config/data/system as /openbao/agent/system.env.
#      secret_id files are deleted on first read by the Agent.
#
# Dependencies (all expected in openbao/openbao Docker image):
#   - bao CLI
#   - python3
# ──────────────────────────────────────────────────────────────────────────────
set -e
set -u

# ── Paths ────────────────────────────────────────────────────────────────────
BAO_INIT_DIR="${BAO_INIT_DIR:-/bao-init}"
INIT_MARKER="${BAO_INIT_DIR}/init-complete"
UNSEAL_KEYS_FILE="${BAO_INIT_DIR}/unseal-keys.json"
ROOT_TOKEN_FILE="${BAO_INIT_DIR}/root-token"
BAO_ADDR="${BAO_ADDR:-http://localhost:8200}"

# ── Logging helper ───────────────────────────────────────────────────────────
log() { echo "[init_openbao] $(date -Iseconds) $*"; }

# ── Helpers: JSON field extraction (relies on python3) ────────────────────────
# Usage: extract_json_field <file> <dot.separated.path>
#   e.g. extract_json_field keys.json root_token
#   e.g. extract_json_field keys.json unseal_keys_b64.0
extract_json_field() {
    _file="$1"
    _path="$2"
    python3 -c "
import json, sys

with open('${_file}') as _f:
    _d = json.load(_f)

parts = '${_path}'.split('.')
current = _d
for part in parts:
    if isinstance(current, list):
        current = current[int(part)]
    else:
        current = current[part]
print(current)
"
}

# ── 1. Check marker file ─────────────────────────────────────────────────────
if [ -f "${INIT_MARKER}" ]; then
    log "Marker ${INIT_MARKER} exists — initialisation already complete. Exiting."
    exit 0
fi

# Verify required tools
for _cmd in bao python3; do
    if ! command -v "${_cmd}" >/dev/null 2>&1; then
        log "FATAL: Required tool '${_cmd}' is not available."
        exit 1
    fi
done

# ── 2. Wait for OpenBao to become reachable ──────────────────────────────────
log "Waiting for OpenBao at ${BAO_ADDR} to become reachable ..."
_i=1
while [ "${_i}" -le 60 ]; do
    # `bao status` exits 0 (unsealed), 1 (not init), 2 (sealed), or crashes
    # We check if the server returned valid JSON (reachable) vs connection error
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

# ── 3. Determine initialisation status ───────────────────────────────────────
INIT_STATUS=$(bao status -format=json 2>/dev/null | python3 -c \
    "import sys,json; print(json.load(sys.stdin).get('initialized', False))") \
    || true

# ── 4. Initialise if needed, otherwise unseal ────────────────────────────────
if [ "${INIT_STATUS}" = "False" ]; then
    # ── 4a. Fresh initialisation ────────────────────────────────────────────
    log "OpenBao is NOT initialised — running 'bao operator init' ..."

    mkdir -p "${BAO_INIT_DIR}"

    bao operator init \
        -format=json \
        -key-shares=5 \
        -key-threshold=3 \
        > "${UNSEAL_KEYS_FILE}"
    chmod 600 "${UNSEAL_KEYS_FILE}"
    log "Unseal keys saved to ${UNSEAL_KEYS_FILE}"

    # Extract root token
    extract_json_field "${UNSEAL_KEYS_FILE}" root_token > "${ROOT_TOKEN_FILE}"
    chmod 600 "${ROOT_TOKEN_FILE}"
    log "Root token saved to ${ROOT_TOKEN_FILE}"

    # Unseal with 3 of 5 keys
    log "Unsealing OpenBao with 3 of 5 key shares ..."
    for _i in 0 1 2; do
        _key=$(extract_json_field "${UNSEAL_KEYS_FILE}" "unseal_keys_b64.${_i}")
        bao operator unseal "${_key}"
    done
    log "OpenBao unsealed successfully."

else
    # ── 4b. Already initialised — unseal from saved keys ────────────────────
    log "OpenBao is already initialised — unsealing with saved keys ..."

    if [ ! -f "${UNSEAL_KEYS_FILE}" ]; then
        log "FATAL: OpenBao is initialised but ${UNSEAL_KEYS_FILE} not found."
        exit 1
    fi

    for _i in 0 1 2; do
        _key=$(extract_json_field "${UNSEAL_KEYS_FILE}" "unseal_keys_b64.${_i}")
        bao operator unseal "${_key}"
    done
    log "OpenBao unsealed successfully."

    if [ ! -f "${ROOT_TOKEN_FILE}" ]; then
        log "FATAL: ${ROOT_TOKEN_FILE} not found — cannot authenticate."
        exit 1
    fi
fi

# ── 5. Authenticate with root token ──────────────────────────────────────────
BAO_TOKEN=$(cat "${ROOT_TOKEN_FILE}")
export BAO_TOKEN
log "Authenticated with root token."

# ── 6. Create system namespace ───────────────────────────────────────────────
# 'bao namespace create' errors with "namespace already exists" if re-run.
log "Creating namespace 'system' ..."
bao namespace create system 2>/dev/null \
    || log "Namespace 'system' already exists — continuing."

# ── 7. Enable KV v2 secrets engine at system/config ─────────────────────────
log "Enabling KV v2 at system/config ..."
bao secrets enable \
    -namespace=system/ \
    -path=config \
    kv-v2 2>/dev/null \
    || log "KV v2 already mounted at system/config — continuing."

# ── 8. Write combined system secret from environment variables ───────────────
# Writes a SINGLE flat object at system/config/data/system (KV v2 data path).
# Keys are lowercase snake_case; the Python openbao_settings.py maps
# them to OZ_ env var names via SYSTEM_KEY_MAPPING. DATABASE_URL is intentionally NOT
# written here — it is added later by scripts/write_db_to_openbao.sh once
# postgres is reachable. The Agent template ranges over .Data.data, so
# missing keys are simply absent from the rendered env file (the Agent
# will re-render once write_db_to_openbao.sh adds DATABASE_URL).
log "Writing combined system secret to system/config/data/system ..."
python3 <<- 'PYEOF'
	import os, subprocess, sys

	BAO_TOKEN = os.environ.get("BAO_TOKEN", "")
	NAMESPACE = "system/"
	SECRET_PATH = "config/data/system"

	# Map OZ_* env vars → lowercase snake_case secret keys (matching Python SYSTEM_KEY_MAPPING).
	# The OpenBao Agent template renders {{ $k }}={{ $v }} verbatim, so keys
	# MUST match the snake_case keys the Python SYSTEM_KEY_MAPPING
	# expects at runtime (e.g. "redis_url" → OZ_REDIS_URL).
	# DATABASE_URL is intentionally absent: write_db_to_openbao.sh appends
	# it to the same secret once postgres is up. Adding an empty
	# DATABASE_URL here would be overwritten by the next write anyway, so
	# we skip it entirely.
	KEY_MAPPING = {
	    "OZ_REDIS_URL":                    "redis_url",
	    "OZ_SECRET_KEY":                   "secret_key",
	    "OZ_PROMETHEUS_URL":               "prometheus_url",
	    "OZ_CORS_ORIGINS":                 "cors_origins",
	    "OZ_HOSTS_ALLOWED":                "hosts_allowed",
	    "OZ_ENVIRONMENT":                  "environment",
	    "OZ_LOG_LEVEL":                    "log_level",
	    "OZ_MAX_WORKERS":                  "max_workers",
	    "OZ_JWT_ACCESS_TOKEN_TTL_MINUTES": "jwt_access_token_ttl_minutes",
	    "OZ_JWT_REFRESH_TOKEN_TTL_DAYS":   "jwt_refresh_token_ttl_days",
	    "OZ_WEBHOOK_SIGNING_SECRET":       "webhook_signing_secret",
	    "OZ_FALKORDB_URL":                 "falkordb_url",
	    "OZ_FALKORDB_MAX_CONNECTIONS":     "falkordb_max_connections",
	    "OZ_FALKORDB_SOCKET_TIMEOUT":      "falkordb_socket_timeout",
	    "OZ_RATE_LIMIT_IP_MAX":            "rate_limit_ip_max",
	    "OZ_RATE_LIMIT_WINDOW_SEC":        "rate_limit_window_sec",
	    "OZ_PROMPT_CACHING_ENABLED":        "prompt_caching_enabled",
	    "OZ_PROMPT_CACHING_ANTHROPIC_MIN_TOKENS": "prompt_caching_anthropic_min_tokens",
	    "OZ_PROMPT_CACHING_ANTHROPIC_TTL":  "prompt_caching_anthropic_ttl",
	    "OZ_DATABASE_URL":                  "database_url",
	}

	# Build a flat dict of all keys present in the environment.
	# Missing env vars are skipped (not written as empty) — we never want
	# to inject a bogus empty value that could mask a real value written
	# later by write_db_to_openbao.sh.
	secret_data = {}
	for env_key, secret_key in KEY_MAPPING.items():
	    value = os.environ.get(env_key)
	    if not value:
	        print(f"  SKIP {secret_key}: env var {env_key} not set", file=sys.stderr)
	        continue
	    secret_data[secret_key] = value

	if not secret_data:
	    print("  FATAL: no system secrets to write", file=sys.stderr)
	    sys.exit(1)

	# Build argv: `bao kv put -namespace=system/ config/data/system key=val ...`
	# Each key=value is a SEPARATE argv item so that special characters
	# (e.g. '!', '$', spaces in URLs) are passed verbatim to bao and never
	# interpreted by the shell. Without this, bao kv put's internal
	# value parsing can mangle secrets containing shell metacharacters.
	cmd = [
	    "bao", "kv", "put",
	    "-namespace=" + NAMESPACE,
	    SECRET_PATH,
	]
	for k, v in secret_data.items():
	    cmd.append(f"{k}={v}")

	result = subprocess.run(
	    cmd,
	    capture_output=True, text=True,
	    env={**os.environ, "BAO_TOKEN": BAO_TOKEN},
	)
	if result.returncode != 0:
	    print(
	        f"  FATAL: failed to write system secret: {result.stderr.strip()}",
	        file=sys.stderr,
	    )
	    sys.exit(1)

	print(f"  Wrote {len(secret_data)} keys to {SECRET_PATH}:")
	for k in sorted(secret_data.keys()):
	    print(f"    - {k}")
PYEOF
log "System secret written."

# ── 9. Enable AppRole auth method ────────────────────────────────────────────
log "Enabling AppRole auth ..."
bao auth enable approle 2>/dev/null \
    || log "AppRole auth already enabled — continuing."

# ── 10. Write ACL policies from mounted files ────────────────────────────────
POLICIES_DIR="${POLICIES_DIR:-/policies}"
if [ -d "${POLICIES_DIR}" ]; then
    for _policy_file in "${POLICIES_DIR}"/*.hcl; do
        [ -f "${_policy_file}" ] || continue
        _name=$(basename "${_policy_file}" .hcl)
        log "Writing policy '${_name}' from ${_policy_file} ..."
        bao policy write "${_name}" "${_policy_file}"
    done
else
    log "WARNING: ${POLICIES_DIR} not found — skipping policy write."
fi

# ── 11. Create AppRole roles ─────────────────────────────────────────────────
log "Creating AppRole 'openzync-app' ..."
bao write auth/approle/role/openzync-app \
    token_policies="openzync-app" \
    token_ttl="24h" \
    token_max_ttl="72h"

log "Creating AppRole 'openzync-worker' ..."
bao write auth/approle/role/openzync-worker \
    token_policies="openzync-worker" \
    token_ttl="72h" \
    token_max_ttl="168h"

# ── 12. Enable Transit engine ────────────────────────────────────────────────
log "Enabling Transit engine at transit/ ..."
bao secrets enable -path=transit transit 2>/dev/null \
    || log "Transit engine already enabled at transit/ — continuing."

log "Creating Transit encryption keys ..."
for _key in org-api-key webhook-secret pii-encryption; do
    log "  Creating key '${_key}' (aes256-gcm96) ..."
    bao write -f transit/keys/"${_key}" type=aes256-gcm96 2>/dev/null \
        || log "  Key '${_key}' already exists — continuing."
done

# ── 13. Retrieve credentials ─────────────────────────────────────────────────
log "Retrieving AppRole credentials ..."

APP_ROLE_ID=$(bao read -field=role_id auth/approle/role/openzync-app/role-id)
APP_SECRET_ID=$(bao write -f -field=secret_id auth/approle/role/openzync-app/secret-id)

WORKER_ROLE_ID=$(bao read -field=role_id auth/approle/role/openzync-worker/role-id)
WORKER_SECRET_ID=$(bao write -f -field=secret_id auth/approle/role/openzync-worker/secret-id)

# ── 14. Write AppRole credentials for the OpenBao Agent sidecars ─────────────
# The api and worker containers run an OpenBao Agent sidecar that
# authenticates via AppRole. The Agent reads role_id and secret_id from
# files in the shared /bao-init volume (mounted at /openbao-bootstrap in
# the api/worker containers). The Agent's
# `remove_secret_id_file_after_reading` flag deletes the secret_id file
# after the first successful auth — the file only needs to exist for the
# initial bootstrap.
log "Writing AppRole credentials to ${BAO_INIT_DIR}/ ..."
umask 077  # belt-and-suspenders: files start 0600 even if chmod misses one
printf '%s' "${APP_ROLE_ID}"      > "${BAO_INIT_DIR}/api-role_id"
chmod 0600 "${BAO_INIT_DIR}/api-role_id"
printf '%s' "${APP_SECRET_ID}"    > "${BAO_INIT_DIR}/api-secret_id"
chmod 0600 "${BAO_INIT_DIR}/api-secret_id"
printf '%s' "${WORKER_ROLE_ID}"   > "${BAO_INIT_DIR}/worker-role_id"
chmod 0600 "${BAO_INIT_DIR}/worker-role_id"
printf '%s' "${WORKER_SECRET_ID}" > "${BAO_INIT_DIR}/worker-secret_id"
chmod 0600 "${BAO_INIT_DIR}/worker-secret_id"
log "  ${BAO_INIT_DIR}/api-role_id"
log "  ${BAO_INIT_DIR}/api-secret_id          (deleted by Agent after first read)"
log "  ${BAO_INIT_DIR}/worker-role_id"
log "  ${BAO_INIT_DIR}/worker-secret_id        (deleted by Agent after first read)"

# ── 15. Output summary ───────────────────────────────────────────────────────
log "═══════════════════════════════════════════════════════════════════"
log " OpenBao bootstrap complete."
log "═══════════════════════════════════════════════════════════════════"
log "  System secret:  system/config/data/system"
log "  AppRole files:  ${BAO_INIT_DIR}/{api,worker}-{role_id,secret_id}"
log "  OpenBao addr:   ${BAO_ADDR}"
log ""
log "  Inspect (debug only — secret_id values are NOT printed by design):"
log "    bao kv get -namespace=system/ config/data/system"
log "    cat ${BAO_INIT_DIR}/api-role_id"
log "    cat ${BAO_INIT_DIR}/worker-role_id"
log "═══════════════════════════════════════════════════════════════════"

# ── 16. Revoke root token (optional, gated by env var) ──────────────────────
# The root token is kept by default for operational access. In production,
# set BAO_REVOKE_ROOT_TOKEN=true to revoke after bootstrap. The token file
# is preserved so a future regeneration can use recovered unseal keys.
if [ "${BAO_REVOKE_ROOT_TOKEN:-false}" = "true" ]; then
    log "Revoking root token (BAO_REVOKE_ROOT_TOKEN=true) ..."
    bao token revoke -self
    log "Root token revoked. Keep the unseal keys safe for recovery."
    date > "${BAO_INIT_DIR}/root-revoked"
    log "Revocation marker written to ${BAO_INIT_DIR}/root-revoked"
fi

# ── 17. Write marker file ────────────────────────────────────────────────────
date > "${INIT_MARKER}"
log "Marker written to ${INIT_MARKER} — future runs will skip."
log "Done."
