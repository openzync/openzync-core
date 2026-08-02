# ──────────────────────────────────────────────────────────────────────────────
# OpenBao Server Configuration — Host-Dev (Shamir seal, single node)
# ──────────────────────────────────────────────────────────────────────────────
# Host-reachable variant used by the standalone dev container
# (openzync-dev-openbao) and scripts/dev_openbao_up.sh.
#
# Deliberate differences from config.hcl (compose static-seal variant):
#   - NO `seal` block → defaults to Shamir, required by `bao operator init`
#     -key-shares=5 -key-threshold=3 in scripts/init_openbao.sh. The static
#     seal in config.hcl would make operator init fail.
#   - listener binds 0.0.0.0 INSIDE the container (required for docker
#     port publishing — docker-proxy forwards to the container bridge IP,
#     which cannot reach a 127.0.0.1-bound listener). Host exposure is
#     restricted to loopback by the `-p 127.0.0.1:8200:8200` port mapping.
#   - api_addr/cluster_addr use host-loopback instead of the compose DNS name.
#
# WARNING: tls_disable is local dev only. Production MUST enable TLS and use a
# proper seal (Shamir, cloud KMS, or HSM).
# ──────────────────────────────────────────────────────────────────────────────

storage "raft" {
  path    = "/vault/data"
  node_id = "node1"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}

# Advertised addresses for cluster communication (Raft needs a concrete
# host, not 0.0.0.0).
api_addr     = "http://127.0.0.1:8200"
cluster_addr = "http://127.0.0.1:8201"

log_level = "info"
