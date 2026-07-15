# OpenZync — OpenBao Agent config for the API service
# Sidecar authenticates as AppRole 'openzync-app' and renders the
# system-level config to /openbao/agent/system.env (env-file format)
# which entrypoint_api.sh sources via `set -a; . ...; set +a`.
# role_id is cached; secret_id is NOT deleted after reading (bootstrap volume is read-only).
# init_openbao.sh must write all system keys as a single flat JSON
# object under the KV v2 key 'system' at system/config/data/system.

auto_auth {
  method "approle" {
    mount_path = "auth/approle"
    config = {
      # Files are written by scripts/init_openbao.sh (sections 13+14) at
      # /bao-init/{api,worker}-{role_id,secret_id} — mounted at
      # /openbao-bootstrap/ in this sidecar's volume. The api Agent reads
      # the api-* pair; the worker Agent (worker.hcl) reads the worker-*
      # pair. The secret_id file is NOT deleted after reading because the
      # bootstrap volume is mounted read-only (least privilege) — the
      # secret_id is single-use anyway and the agent renews the session
      # token once authenticated.
      role_id_file_path   = "/openbao-bootstrap/api-role_id"
      secret_id_file_path = "/openbao-bootstrap/api-secret_id"
    }
  }
  sinks {
    sink "file" {
      type   = "file"
      config = {
        path = "/openbao/agent/.token"
        mode = 384  # 0600 octal
      }
    }
  }
}

# Render system config to tmpfs as env-file format
template_config {
  static_secret_render_interval = "5m"
  exit_on_retry_failure         = true
}

template {
  destination          = "/openbao/agent/system.env"
  perms                = "0600"
  error_on_missing_key = true
  contents = <<EOT
{{- with secret "config/data/system" -}}
{{- range $k, $v := .Data.data }}
{{ $k }}={{ $v }}
{{ end -}}
{{- end }}
EOT
}

vault {
  address   = "http://openbao:8200"
  namespace = "system/"
  retry {
    backoff     = "exponential"
    max_retries = 10
  }
}

# Security model — fail-fast, deny-by-default:
# system.env is 0600 on tmpfs; .token sink is 0600;
# error_on_missing_key + exit_on_retry_failure fail loudly on render error;
# tokens auto-renew; bootstrap volume is mounted read-only.
