# ──────────────────────────────────────────────────────────────────────────────
# OpenBao Server Configuration — Development / Docker Compose
# ──────────────────────────────────────────────────────────────────────────────
# Uses Integrated Storage (Raft) as the storage backend for simplicity.
# Static KV seal enables auto-unseal via the BAO_STATIC_SEAL_KEY env var.
#
# WARNING: tls_disable is set for local dev only.  Production deployments
# MUST enable TLS and use a proper seal (Shamir, cloud KMS, or HSM).
# ──────────────────────────────────────────────────────────────────────────────

storage "raft" {
  path    = "/vault/data"
  node_id = "node1"
}

listener "tcp" {
  address       = "0.0.0.0:8200"
  tls_disable   = true
}

# BAO_STATIC_SEAL_KEY env var auto-configures the static KV seal.
# File audit log — enabled at runtime via API (`bao audit enable file ...`)
# Mount /vault/logs as a volume in production to persist audit trail.
# audit "file" {
#   path         = "/vault/logs/audit.log"
#   log_raw      = false
#   format       = "json"
#   prefix       = "[audit]"
# }

# Advertised addresses for cluster communication.
api_addr     = "http://0.0.0.0:8200"
cluster_addr = "https://0.0.0.0:8201"

# Log verbosity — align with the application's LOG_LEVEL.
log_level = "info"
