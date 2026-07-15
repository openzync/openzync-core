#!/bin/sh
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — OpenBao Bootstrap Initialisation Script
# ──────────────────────────────────────────────────────────────────────────────
# ALWAYS unseals OpenBao on restart, but bootstraps (namespace, KV, policies,
# AppRoles, Transit) only on the first run.
#
# The unseal step must run every time because OpenBao starts sealed on every
# container restart (Shamir seal).  The bootstrap steps are guarded by a marker
# file so they run exactly once.
#
# What this script does:
#   1. Waits for OpenBao to be reachable (always).
#   2. Initialises + unseals if first boot, or re-unseals from saved keys.
#   3. Authenticates with the root token.
#   4. Checks the marker file — if present, bootstrap is done, exits.
#   5. Creates the 'system' namespace and mounts KV v2 at system/config.
#   6. Writes a SINGLE combined system secret at system/config/system
#      containing all OZ_* env vars (UPPERCASE keys) — EXCEPT DATABASE_URL.
#      The database URL is added later by scripts/write_db_to_openbao.sh.
#   7. Enables AppRole auth, writes ACL policies, creates the
#      'openzync-app' and 'openzync-worker' AppRoles, and enables the
#      Transit engine with the standard encryption keys.
#   8. Writes the AppRole role_id and secret_id to four files in
#      /bao-init/ (the shared volume with the api/worker Agent sidecars):
#         api-role_id, api-secret_id, worker-role_id, worker-secret_id
#      secret_id files persist on disk (bootstrap volume is read-only to
#      agent sidecars, so cleanup is deferred — token auto-renewal means
#      the secret_id is never needed again after initial auth).
#   9. Writes the marker file so future runs skip bootstrap.
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
    if echo "${_output}" | python3 -c "import sys, json; json.load(sys.stdin)" >/dev/null 2>&1; then
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

# ── 6. Check marker file (unseal already done above) ────────────────────────
# On every restart after the first successful bootstrap, the marker exists and
# we stop here — the unseal at step 4 is all that's needed to make OpenBao
# operational.  The bootstrap steps below (namespace, KV, AppRole, Transit)
# only need to run once.
if [ -f "${INIT_MARKER}" ]; then
    log "Marker ${INIT_MARKER} exists — bootstrap already complete. Exiting."
    exit 0
fi

# ── 7. Create system namespace ───────────────────────────────────────────────
# 'bao namespace create' errors with "namespace already exists" if re-run.
log "Creating namespace 'system' ..."
bao namespace create system 2>/dev/null \
    || log "Namespace 'system' already exists — continuing."

# ── 8. Enable KV v2 secrets engine at system/config ─────────────────────────
log "Enabling KV v2 at system/config ..."
bao secrets enable \
    -namespace=system/ \
    -path=config \
    kv-v2 2>/dev/null \
    || log "KV v2 already mounted at system/config — continuing."

# ── 9. Write combined system secret from environment variables ───────────────
# Writes a SINGLE flat object at system/config/system (logical KV v2 path).
# Keys are lowercase snake_case; the Python openbao_settings.py maps
# them to OZ_ env var names via SYSTEM_KEY_MAPPING. DATABASE_URL is intentionally NOT
# written here — it is added later by scripts/write_db_to_openbao.sh once
# postgres is reachable. The Agent template ranges over .Data.data, so
# missing keys are simply absent from the rendered env file (the Agent
# will re-render once write_db_to_openbao.sh adds DATABASE_URL).
log "Writing combined system secret to system/config/system ..."
python3 <<- 'PYEOF'
	import os, subprocess, sys

	BAO_TOKEN = os.environ.get("BAO_TOKEN", "")
	NAMESPACE = "system/"
	SECRET_PATH = "config/system"

	# Write OZ_* env vars with their original UPPERCASE names so the
	# Agent template can output them directly without any transform.
	# (The template engine in OpenBao 2.5 lacks the `upper` function.)
	KEY_MAPPING = {
	    "OZ_REDIS_URL":                    "OZ_REDIS_URL",
	    "OZ_SECRET_KEY":                   "OZ_SECRET_KEY",
	    "OZ_PROMETHEUS_URL":               "OZ_PROMETHEUS_URL",
	    "OZ_CORS_ORIGINS":                 "OZ_CORS_ORIGINS",
	    "OZ_HOSTS_ALLOWED":                "OZ_HOSTS_ALLOWED",
	    "OZ_ENVIRONMENT":                  "OZ_ENVIRONMENT",
	    "OZ_LOG_LEVEL":                    "OZ_LOG_LEVEL",
	    "OZ_MAX_WORKERS":                  "OZ_MAX_WORKERS",
	    "OZ_JWT_ACCESS_TOKEN_TTL_MINUTES": "OZ_JWT_ACCESS_TOKEN_TTL_MINUTES",
	    "OZ_JWT_REFRESH_TOKEN_TTL_DAYS":   "OZ_JWT_REFRESH_TOKEN_TTL_DAYS",
	    "OZ_WEBHOOK_SIGNING_SECRET":       "OZ_WEBHOOK_SIGNING_SECRET",
	    "OZ_FALKORDB_URL":                 "OZ_FALKORDB_URL",
	    "OZ_FALKORDB_MAX_CONNECTIONS":     "OZ_FALKORDB_MAX_CONNECTIONS",
	    "OZ_FALKORDB_SOCKET_TIMEOUT":      "OZ_FALKORDB_SOCKET_TIMEOUT",
	    "OZ_RATE_LIMIT_IP_MAX":            "OZ_RATE_LIMIT_IP_MAX",
	    "OZ_RATE_LIMIT_WINDOW_SEC":        "OZ_RATE_LIMIT_WINDOW_SEC",
	    "OZ_PROMPT_CACHING_ENABLED":        "OZ_PROMPT_CACHING_ENABLED",
	    "OZ_PROMPT_CACHING_ANTHROPIC_MIN_TOKENS": "OZ_PROMPT_CACHING_ANTHROPIC_MIN_TOKENS",
	    "OZ_PROMPT_CACHING_ANTHROPIC_TTL":  "OZ_PROMPT_CACHING_ANTHROPIC_TTL",
	    "OZ_DATABASE_URL":                  "OZ_DATABASE_URL",
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

	# Build argv: `bao kv put -namespace=system/ config/system key=val ...`
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

# ── 10. Enable AppRole auth method ───────────────────────────────────────────
log "Enabling AppRole auth ..."
bao auth enable approle 2>/dev/null \
    || log "AppRole auth already enabled — continuing."

# ── 11. Write ACL policies from mounted files ────────────────────────────────
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

# ── 12. Create AppRole roles ─────────────────────────────────────────────────
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

# ── 13. Enable Transit engine ────────────────────────────────────────────────
log "Enabling Transit engine at transit/ ..."
bao secrets enable -path=transit transit 2>/dev/null \
    || log "Transit engine already enabled at transit/ — continuing."

log "Creating Transit encryption keys ..."
for _key in org-api-key webhook-secret pii-encryption; do
    log "  Creating key '${_key}' (aes256-gcm96) ..."
    bao write -f transit/keys/"${_key}" type=aes256-gcm96 2>/dev/null \
        || log "  Key '${_key}' already exists — continuing."
done

# ── 14. Retrieve credentials ─────────────────────────────────────────────────
log "Retrieving AppRole credentials ..."

APP_ROLE_ID=$(bao read -field=role_id auth/approle/role/openzync-app/role-id)
APP_SECRET_ID=$(bao write -f -field=secret_id auth/approle/role/openzync-app/secret-id)

WORKER_ROLE_ID=$(bao read -field=role_id auth/approle/role/openzync-worker/role-id)
WORKER_SECRET_ID=$(bao write -f -field=secret_id auth/approle/role/openzync-worker/secret-id)

# ── 15. Write AppRole credentials for the OpenBao Agent sidecars ─────────────
# The api and worker containers run an OpenBao Agent sidecar that
# authenticates via AppRole. The Agent reads role_id and secret_id from
# files in the shared /bao-init volume (mounted at /openbao-bootstrap as
# read-only in the api/worker containers).  The secret_id is single-use;
# after the initial auth the Agent renews its session token automatically.
# The files persist on disk because the bootstrap volume is read-only to
# the sidecars (least privilege — the root token and unseal keys share
# this volume).
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

# ── 16. Output summary ───────────────────────────────────────────────────────
log "═══════════════════════════════════════════════════════════════════"
log " OpenBao bootstrap complete."
log "═══════════════════════════════════════════════════════════════════"
log "  System secret:  system/config/system"
log "  AppRole files:  ${BAO_INIT_DIR}/{api,worker}-{role_id,secret_id}"
log "  OpenBao addr:   ${BAO_ADDR}"
log ""
log "  Inspect (debug only — secret_id values are NOT printed by design):"
log "    bao kv get -namespace=system/ config/system"
log "    cat ${BAO_INIT_DIR}/api-role_id"
log "    cat ${BAO_INIT_DIR}/worker-role_id"
log "═══════════════════════════════════════════════════════════════════"

# ── 17. Revoke root token (optional, gated by env var) ──────────────────────
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

# ── 18. Write marker file ────────────────────────────────────────────────────
date > "${INIT_MARKER}"
log "Marker written to ${INIT_MARKER} — future runs will skip."
log "Done."
