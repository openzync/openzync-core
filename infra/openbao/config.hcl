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

# Static KV seal reads an AES-256-GCM key from the BAO_STATIC_SEAL_KEY
# environment variable.  The key must be a 64-character hex string
# (32 bytes encoded as hex).
seal "static_kv" {
  # BAO_STATIC_SEAL_KEY env var MUST be set on the container.
}

# mlock prevents memory from being swapped to disk.
disable_mlock = false

# Advertised addresses for cluster communication.
api_addr     = "http://0.0.0.0:8200"
cluster_addr = "https://0.0.0.0:8201"

# Log verbosity — align with the application's LOG_LEVEL.
log_level = "info"
