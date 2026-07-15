# ──────────────────────────────────────────────────────────────────────────────
# OpenZync Background Worker — OpenBao ACL Policy
# ──────────────────────────────────────────────────────────────────────────────
# Grants the ARQ worker (via AppRole "openzync-worker" in system/ namespace):
#   1. Read system-level config (config/data/*).
#   2. Read org-level config keys (+/config/data/*) — read-only.
# ──────────────────────────────────────────────────────────────────────────────

# ── Config access (system + org — relative to system/ namespace) ─────────────
# The AppRole token is scoped to the system/ namespace. All policy paths are
# relative to system/ — config/data/* matches system/config/data/* from root.
path "config/data/*" {
  capabilities = ["read", "list"]
}

path "config/metadata/*" {
  capabilities = ["list"]
}

# ── KV v2 preflight check (required by OpenBao Agent template rendering) ─────
# The Agent calls sys/internal/ui/mounts/<path> to detect KV v2 mounts before
# attempting template rendering.  Without this, the Agent gets a 403 on the
# preflight check and never renders the secret.
path "sys/internal/ui/mounts/*" {
  capabilities = ["read", "list"]
}

# ── Org-level config: read-only (within system/ child namespaces) ───────────
path "+/config/data/*" {
  capabilities = ["read", "list"]
}

path "+/config/metadata/*" {
  capabilities = ["list"]
}

# ── Transit engine: worker can decrypt but NOT encrypt ────────────────────
# Workers need to decrypt org API keys and webhook secrets when processing
# background jobs, but should never create new encrypted data.
path "transit/decrypt/*" {
  capabilities = ["create", "update"]
}

# Read-only key metadata.
path "transit/keys/*" {
  capabilities = ["read", "list"]
}
