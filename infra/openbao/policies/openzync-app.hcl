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
# NOTE: OpenBao does not support the "namespace" key in ACL policy paths.
#       Namespace-scoped paths are written as literal path prefixes.
path "system/config/data/*" {
  capabilities = ["read", "list"]
}

path "system/config/metadata/*" {
  capabilities = ["list"]
}

path "sys/namespaces/system/" {
  capabilities = ["read", "list"]
}

# ── Namespace management ────────────────────────────────────────────────────
path "sys/namespaces/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

# ── KV v2 preflight check (required by OpenBao Agent template rendering) ─────
# The Agent calls sys/internal/ui/mounts/<path> to detect KV v2 mounts before
# template rendering.  Without this, the Agent gets a 403 on preflight even if
# the path itself is ACL-granted.
path "sys/internal/ui/mounts/*" {
  capabilities = ["read", "list"]
}

# ── Enable secrets engines — root level and inside ANY namespace ────────────
# The "+" glob matches a single namespace segment (e.g. "org_<uuid>").
# Without this, root-level "sys/mounts/*" does not apply within namespaces.
path "sys/mounts/*" {
  capabilities = ["create", "read", "update", "delete"]
}

path "+/sys/mounts/*" {
  capabilities = ["create", "read", "update", "delete"]
}

# ── Org-level config: full CRUD within any org_* namespace ──────────────────
path "+/config/data/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

path "+/config/metadata/*" {
  capabilities = ["list"]
}

path "config/data/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

path "config/metadata/*" {
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
