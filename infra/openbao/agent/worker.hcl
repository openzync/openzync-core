# ──────────────────────────────────────────────────────────────────────
# OpenZync — OpenBao Agent config for the Worker service
# ──────────────────────────────────────────────────────────────────────
# Sidecar for the 'worker' service. Authenticates to OpenBao via AppRole
# 'openzync-worker' (read-only policy: infra/openbao/policies/worker.hcl)
# and renders the system config to /openbao/agent/system.env.
# entrypoint_worker.sh sources that file before exec'ing the ARQ worker,
# so every KEY=VALUE becomes an env var.
#
# init_openbao.sh writes worker-role_id + worker-secret_id (mode 0600)
# into the shared /openbao-bootstrap/ volume; compose mounts them here.
# secret_id is read once then deleted; tokens auto-renew before expiry.
# ──────────────────────────────────────────────────────────────────────

auto_auth {
  method "approle" {
    mount_path = "auth/approle"
    config = {
      role_id_file_path   = "/openbao-bootstrap/worker-role_id"
      secret_id_file_path = "/openbao-bootstrap/worker-secret_id"
      remove_secret_id_file_after_reading = true
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

template_config {
  static_secret_render_interval = "5m"
  exit_on_retry_failure         = true
}

template {
  destination          = "/openbao/agent/system.env"
  perms                = "0600"
  error_on_missing_key = true
  contents = <<EOT
{{- with secret "system/config/data/system" -}}
{{- range $k, $v := .Data.data }}
{{ $k | upper }}={{ $v }}
{{ end -}}
{{- end }}
EOT
}

vault {
  address = "http://openbao:8200"
  retry {
    backoff     = "exponential"
    max_retries = 10
  }
}
