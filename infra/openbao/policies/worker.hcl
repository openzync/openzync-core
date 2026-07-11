# ──────────────────────────────────────────────────────────────────────────────
# OpenZync Background Worker — OpenBao ACL Policy
# ──────────────────────────────────────────────────────────────────────────────
# Grants the ARQ worker (via AppRole "openzync-worker") permission to:
#   1. Read system-level config from the system/ namespace.
#   2. Read org-level config keys (read-only — the worker never writes config).
# ──────────────────────────────────────────────────────────────────────────────

# ── System namespace: read-only config access ────────────────────────────────
path "config/data/*" {
  capabilities = ["read", "list"]
  namespace    = "system/"
}

path "config/metadata/*" {
  capabilities = ["list"]
  namespace    = "system/"
}

# ── Org-level config: read-only within any org_* namespace ──────────────────
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
