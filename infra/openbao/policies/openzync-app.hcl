# ──────────────────────────────────────────────────────────────────────────────
# OpenZync API Server — OpenBao ACL Policy
# ──────────────────────────────────────────────────────────────────────────────
# Grants the API server (via AppRole "openzync-app" in system/ namespace):
#   1. Read and write system-level config (config/data/*).
#   2. Create, read, update, and delete child namespaces (org_<uuid>/).
#   3. Enable and configure secrets engines within child namespaces.
#   4. Read and write org-level config keys (+/config/data/*).
# ──────────────────────────────────────────────────────────────────────────────

# ── Config access (system + org — all relative to system/ namespace) ─────────
# The AppRole token is scoped to the system/ namespace (AppRole auth is
# enabled inside system/).  All paths are relative to system/:
#   config/data/system       → system/config/data/system
#   config/data/org_<uuid>/  → system/config/data/org_<uuid>/
#   org_<uuid>/config/data/  → system/org_<uuid>/config/data/   (+ glob)
path "config/data/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

path "config/metadata/*" {
  capabilities = ["list"]
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

# ── Enable secrets engines — within system/ + inside child namespaces ────────
# The "+" glob matches a single namespace segment (e.g. "org_<uuid>") within
# the system/ namespace, so the API can enable KV/transit in org sub-namespaces.
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
