# ──────────────────────────────────────────────────────────────────────────────
# OpenZync API Server — OpenBao ACL Policy
# ──────────────────────────────────────────────────────────────────────────────
# Grants the API server (via AppRole "openzync-app") permission to:
#   1. Read system-level config from the system/ namespace.
#   2. Create, read, update, and delete org-level namespaces.
#   3. Enable secrets engines within org namespaces.
#   4. Read and write org-level config keys.
# ──────────────────────────────────────────────────────────────────────────────

# ── System namespace: read-only config access ────────────────────────────────
path "sys/namespaces/system/" {
  capabilities = ["read", "list"]
}

path "config/data/*" {
  capabilities = ["read", "list"]
  namespace    = "system/"
}

path "config/metadata/*" {
  capabilities = ["list"]
  namespace    = "system/"
}

# ── Namespace management ────────────────────────────────────────────────────
path "sys/namespaces/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

# ── Enable secrets engines inside org namespaces ────────────────────────────
path "sys/mounts/*" {
  capabilities = ["create", "read", "update", "delete"]
}

# ── Org-level config: full CRUD within any org_* namespace ──────────────────
path "+/config/data/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

path "+/config/metadata/*" {
  capabilities = ["list"]
}

# ── Transit engine: encrypt, decrypt, and rewrap data ─────────────────────
# The app can encrypt and decrypt with any key but cannot manage keys
# (key creation/rotation is done by the bootstrap script).
path "transit/encrypt/*" {
  capabilities = ["create", "update"]
}

path "transit/decrypt/*" {
  capabilities = ["create", "update"]
}

path "transit/rewrap/*" {
  capabilities = ["create", "update"]
}

# Read-only key metadata (to check key exists, get key version info).
path "transit/keys/*" {
  capabilities = ["read", "list"]
}
