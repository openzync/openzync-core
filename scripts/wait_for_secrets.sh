#!/bin/sh
# Shared: wait for the OpenBao Agent sidecar to render system.env.
# Sourced by entrypoint_api.sh / entrypoint_worker.sh (both define log()).
# Usage: wait_for_secrets [SECRETS_FILE] [TIMEOUT_SEC] — 0 ready, 1 timeout.
wait_for_secrets() {
  _w_file="${1:-/run/secrets/system.env}"
  _w_timeout="${2:-90}"
  log "Waiting for OpenBao Agent to render secrets to ${_w_file} ..."
  _w_i=0
  while [ "$_w_i" -lt "$_w_timeout" ]; do
    if [ -s "$_w_file" ] && grep -q '^OZ_DATABASE_URL=' "$_w_file"; then
      return 0
    fi
    _w_i=$((_w_i + 1))
    sleep 1
  done
  log "FATAL: timed out (${_w_timeout}s) waiting for ${_w_file} — sidecar may be down or AppRole auth failing"
  return 1
}
