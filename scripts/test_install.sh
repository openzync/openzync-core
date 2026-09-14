#!/usr/bin/env bash
# ==============================================================================
# OpenZync installer test suite (sandboxed, docker-based, health-only).
#
# Usage:
#   bash scripts/test_install.sh                       # all cases T1-T9
#   CASE=T1,T2 bash scripts/test_install.sh            # subset filter
#   bash scripts/test_install.sh --keep-on-failure     # preserve scene on FAIL
#   make test-install CASE=T4,T6
#
# Safety: sandboxed ONLY (--dir /tmp/oz-install-test/N, API 18080,
# frontend 13000, throwaway PG 15432). Never ~/.openzync or default ports.
# Every endpoint assertion is GET /health == 200 (never /ready, /v1/*, CORS).
# Non-interactive driving: prompts are fed via `printf ... | setsid ...`
# (setsid detaches /dev/tty so install.sh prompt() falls back to stdin).
# Secrets are generated only, never logged.
# ==============================================================================
set -uo pipefail

# ── Config (env knobs mirror scripts/e2e_test.sh) ─────────────────────────────
INSTALL_SH="${INSTALL_SH:-infra/install.sh}"
SANDBOX_BASE="${SANDBOX_BASE:-/tmp/oz-install-test}"
API_PORT="${SANDBOX_API_PORT:-18080}"
FRONTEND_PORT="${SANDBOX_FRONT_PORT:-13000}"
PG_PORT="${THROWAWAY_PG_PORT:-15432}"
PG_NAME="${PG_NAME:-oz-install-test-pg}"
PG_IMAGE="${PG_IMAGE:-pgvector/pgvector:pg15}"
PG_PASSWORD="${PG_PASSWORD:-oz-suite-pw-only}"
STEP_DELAY="${STEP_DELAY:-2}"
CASE="${CASE:-}"          # e.g. CASE=T4,T6 — empty means run all
KEEP_ON_FAILURE=0

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
PASS="${GREEN}✓ PASS${NC}"
FAIL="${RED}✗ FAIL${NC}"
WARN="${YELLOW}⚠ WARN${NC}"

# ── State ─────────────────────────────────────────────────────────────────────
PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0
STEP=0
VOL_SNAP=""

# ── Helpers ───────────────────────────────────────────────────────────────────
log() { printf '[test-install] %s %s\n' "$(date -Iseconds)" "$*"; }
die() { printf '[test-install] FATAL: %s\n' "$*" >&2; exit 1; }

# curl_retry: curl with retry for transient errors (mirrors e2e_test.sh).
curl_retry() {
  curl --retry 3 --retry-delay 2 --retry-all-errors -s "$@"
}

step_delay() {
  if [ "$STEP_DELAY" -gt 0 ] 2>/dev/null; then
    sleep "$STEP_DELAY"
  fi
}

step() {
  STEP=$((STEP + 1))
  echo -e "\n${YELLOW}[Step $STEP]${NC} $1"
  step_delay
}

ok() {
  echo -e "  ${PASS} $1"
  PASS_COUNT=$((PASS_COUNT + 1))
}

fail() {
  echo -e "  ${FAIL} $1"
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

warn() {
  echo -e "  ${WARN} $1"
  WARN_COUNT=$((WARN_COUNT + 1))
}

should_run() {
  [[ -z "$CASE" ]] && return 0
  [[ ",${CASE}," == *",$1,"* ]]
}

port_in_use() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1
}

# wait_for_health <base-url> <timeout-secs>: poll GET /health == 200.
wait_for_health() {
  local base="$1" timeout="$2" i=0
  while [[ "$i" -lt "$timeout" ]]; do
    if [[ "$(curl_retry -o /dev/null -w '%{http_code}' "${base}/health")" == "200" ]]; then
      return 0
    fi
    sleep 2
    i=$((i + 2))
  done
  return 1
}

openzync_containers() { docker ps -a --filter name=openzync --format '{{.Names}}'; }

# ── Preflight: fail fast on shared-state or held sandbox ports ────────────────
preflight() {
  local existing=""
  existing="$(openzync_containers || true)"
  if [[ -n "$existing" ]]; then
    die "openzync-* containers already exist — fix: docker rm -f \$(docker ps -aq --filter name=openzync)"
  fi
  local p
  for p in "$API_PORT" "$FRONTEND_PORT" "$PG_PORT"; do
    if port_in_use "$p"; then
      die "sandbox port ${p} is held — fix: lsof -ti:${p} | xargs -r kill -9"
    fi
  done
  mkdir -p "$SANDBOX_BASE"
  VOL_SNAP="${SANDBOX_BASE}/volumes.before"
  docker volume ls -q 2>/dev/null | sort > "$VOL_SNAP" || true
  log "preflight ok (sandbox ${SANDBOX_BASE}, api :${API_PORT}, frontend :${FRONTEND_PORT}, pg :${PG_PORT})"
}

# cleanup runs on EXIT: uninstall every suite dir (n/n answers), remove the
# throwaway PG, drop ONLY suite-created volumes (snapshot diff). Skipped when
# --keep-on-failure was given and at least one case failed.
cleanup() {
  if [[ "$KEEP_ON_FAILURE" -eq 1 ]] && [[ "$FAIL_COUNT" -gt 0 ]]; then
    warn "keeping scene (--keep-on-failure): ${SANDBOX_BASE}"
    return 0
  fi
  local d
  for d in "${SANDBOX_BASE}"/*/; do
    [[ -d "$d" ]] || continue
    [[ -f "${d}/install.env" ]] || continue
    log "cleanup uninstall: ${d}"
    printf 'n\nn\n' | setsid bash "$INSTALL_SH" --dir "$d" --uninstall >/dev/null 2>&1 || true
  done
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$PG_NAME"; then
    docker rm -f "$PG_NAME" >/dev/null 2>&1 || true
  fi
  if [[ -f "$VOL_SNAP" ]]; then
    local v
    while read -r v; do
      [[ -n "$v" ]] || continue
      if ! grep -qx "$v" "$VOL_SNAP"; then
        docker volume rm "$v" >/dev/null 2>&1 || true
      fi
    done < <(docker volume ls -q 2>/dev/null | sort)
  fi
}
trap cleanup EXIT

# ── Cases ─────────────────────────────────────────────────────────────────────

# T1 arg validation (no docker needed): --help exit 0; --bogus non-zero + hint.
t1_arg_validation() {
  step "T1 arg validation"
  local out rc
  out="$(bash "$INSTALL_SH" --help 2>&1)"; rc="$?"
  if [[ "$rc" -eq 0 ]]; then ok "--help exits 0"; else fail "--help exits ${rc}"; fi
  out="$(bash "$INSTALL_SH" --bogus 2>&1)"; rc="$?"
  if [[ "$rc" -ne 0 ]] && [[ "$out" == *"Usage"* || "$out" == *"--help"* ]]; then
    ok "--bogus non-zero with usage hint"
  else
    fail "--bogus rc=${rc} (need non-zero + usage hint)"
  fi
}

# T2 status with no install: fresh empty dir -> all DOWN lines, exit 0.
t2_status_no_install() {
  step "T2 status with no install"
  local d="${SANDBOX_BASE}/2" out rc
  rm -rf "$d"; mkdir -p "$d"
  out="$(bash "$INSTALL_SH" --dir "$d" --status 2>&1)"; rc="$?"
  if [[ "$rc" -eq 0 ]]; then ok "status exits 0 on empty dir"; else fail "status exits ${rc}"; fi
  if [[ "$out" == *"/health: DOWN"* ]]; then ok "api /health: DOWN"; else fail "missing '/health: DOWN'"; fi
  if [[ "$out" == *"frontend: DOWN"* ]]; then ok "frontend: DOWN"; else fail "missing 'frontend: DOWN'"; fi
}

# T3 port-conflict fail-fast: occupy API port, install -> FATAL + zero containers.
t3_port_conflict() {
  step "T3 port-conflict fail-fast"
  local d="${SANDBOX_BASE}/3" out rc
  rm -rf "$d"; mkdir -p "$d"
  python3 -m http.server "$API_PORT" --bind 127.0.0.1 >/dev/null 2>&1 &
  local server_pid="$!"
  sleep 1
  if ! port_in_use "$API_PORT"; then fail "could not occupy :${API_PORT}"; kill "$server_pid" 2>/dev/null || true; return; fi
  out="$(printf 'y\ny\n\n\n%s\n%s\ny\n' "$API_PORT" "$FRONTEND_PORT" \
    | setsid bash "$INSTALL_SH" --dir "$d" 2>&1)"; rc="$?"
  kill "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
  if [[ "$rc" -ne 0 ]] && [[ "$out" == *"already in use"* ]]; then
    ok "FATAL already-in-use, exit ${rc}"
  else
    fail "expected FATAL already-in-use (rc=${rc})"
  fi
  if [[ -z "$(docker ps -a --filter name=openzync -q 2>/dev/null)" ]]; then
    ok "zero containers created"
  else
    fail "containers leaked: $(docker ps -a --filter name=openzync -q | tr '\n' ' ')"
  fi
}

# T4 full local-db stack with sandbox ports.
t4_full_local_db() {
  step "T4 full --yes local-db stack"
  local d="${SANDBOX_BASE}/4" out rc
  rm -rf "$d"; mkdir -p "$d"
  out="$(printf 'y\ny\n\n\n%s\n%s\ny\n' "$API_PORT" "$FRONTEND_PORT" \
    | setsid bash "$INSTALL_SH" --dir "$d" 2>&1)"; rc="$?"
  if [[ "$rc" -ne 0 ]]; then fail "install exited ${rc}"; return; fi
  local total up
  total="$(docker ps -a --filter name=openzync --format '{{.Names}}' | wc -l)"
  up="$(docker ps --filter name=openzync --format '{{.Names}}' | wc -l)"
  if [[ "$total" -gt 0 ]] && [[ "$up" -eq "$total" ]]; then
    ok "${up}/${total} containers up"
  else
    fail "only ${up}/${total} containers up"
  fi
  if wait_for_health "http://127.0.0.1:${API_PORT}" 120; then
    ok "GET /health == 200 within 120s"
  else
    fail "GET /health never 200"
  fi
  local mode names
  mode="$(stat -c %a "${d}/.env" 2>/dev/null || stat -f %Lp "${d}/.env")"
  names="$(grep -E '^[A-Z_]+=' "${d}/.env" | cut -d= -f1 | sort | tr '\n' ' ')"
  if [[ "$mode" == "600" ]] && [[ "$names" == "BAO_STATIC_SEAL_KEY OZ_SECRET_KEY OZ_WEBHOOK_SIGNING_SECRET POSTGRES_PASSWORD " ]]; then
    ok ".env mode 600 with exactly the 4 bootstrap vars"
  else
    fail ".env mode=${mode} vars=[${names}]"
  fi
  if grep -q "^API_PORT=${API_PORT}$" "${d}/install.env" && grep -q "^FRONTEND_PORT=${FRONTEND_PORT}$" "${d}/install.env"; then
    ok "install.env pointers match sandbox ports"
  else
    fail "install.env pointers mismatch"
  fi
  if [[ "$(curl_retry -o /dev/null -w '%{http_code}' "http://127.0.0.1:${FRONTEND_PORT}/")" == "200" ]]; then
    ok "frontend root URL 200"
  else
    fail "frontend root URL not 200"
  fi
}

# T5 status green after T4 (no /ready assertions).
t5_status_green() {
  step "T5 status green"
  local d="${SANDBOX_BASE}/4" out
  if [[ ! -f "${d}/install.env" ]]; then fail "T4 dir missing — run CASE=T4 first"; return; fi
  out="$(bash "$INSTALL_SH" --dir "$d" --status 2>&1)"
  if [[ "$out" == *"openzync-"* ]]; then ok "containers listed"; else fail "no containers listed"; fi
  if [[ "$out" == *"/health: UP"* ]]; then ok "/health: UP"; else fail "missing '/health: UP'"; fi
  if [[ "$out" == *"frontend: UP"* ]]; then ok "frontend UP"; else fail "missing 'frontend: UP'"; fi
}

# T6 idempotent re-run keeping .env: still green, seal byte-identical, /health 200.
t6_idempotent_rerun() {
  step "T6 idempotent re-run"
  local d="${SANDBOX_BASE}/4" seal_before seal_after out rc
  if [[ ! -f "${d}/.env" ]]; then fail "T4 dir missing — run CASE=T4 first"; return; fi
  seal_before="$(grep '^BAO_STATIC_SEAL_KEY=' "${d}/.env" | sha256sum | cut -d' ' -f1)"
  out="$(printf 'y\ny\n\n\n%s\n%s\nn\n\n\n\n\n\n' "$API_PORT" "$FRONTEND_PORT" \
    | setsid bash "$INSTALL_SH" --dir "$d" 2>&1)"; rc="$?"
  if [[ "$rc" -ne 0 ]]; then fail "re-run exited ${rc}"; return; fi
  seal_after="$(grep '^BAO_STATIC_SEAL_KEY=' "${d}/.env" | sha256sum | cut -d' ' -f1)"
  if [[ "$seal_before" == "$seal_after" ]]; then
    ok "BAO_STATIC_SEAL_KEY byte-identical"
  else
    fail "BAO_STATIC_SEAL_KEY changed on re-run"
  fi
  if wait_for_health "http://127.0.0.1:${API_PORT}" 120; then
    ok "still green, GET /health == 200"
  else
    fail "GET /health not 200 after re-run"
  fi
}

# Pick a DB host reachable from inside backend containers.
pick_db_host() {
  if docker run --rm "$PG_IMAGE" pg_isready -h host.docker.internal -p "$PG_PORT" -U postgres >/dev/null 2>&1; then
    printf 'host.docker.internal\n'; return 0
  fi
  local gw
  gw="$(docker network inspect bridge -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null)"
  if [[ -n "$gw" ]] && docker run --rm "$PG_IMAGE" pg_isready -h "$gw" -p "$PG_PORT" -U postgres >/dev/null 2>&1; then
    printf '%s\n' "$gw"; return 0
  fi
  return 1
}

# T7 external-DB profile: suite-owned pgvector/pg15 on :15432, URL via stdin.
t7_external_db() {
  step "T7 external-DB profile"
  local d="${SANDBOX_BASE}/7" db_host db_url out rc
  rm -rf "$d"; mkdir -p "$d"
  docker rm -f "$PG_NAME" >/dev/null 2>&1 || true
  docker run -d --name "$PG_NAME" -e POSTGRES_PASSWORD="$PG_PASSWORD" \
    -p "${PG_PORT}:5432" "$PG_IMAGE" >/dev/null || { fail "throwaway PG did not start"; return; }
  local i=0
  while ! docker exec "$PG_NAME" pg_isready -U postgres >/dev/null 2>&1; do
    sleep 2; i=$((i + 1))
    if [[ "$i" -ge 30 ]]; then fail "throwaway PG never ready"; return; fi
  done
  PGPASSWORD="$PG_PASSWORD" docker exec -e PGPASSWORD="$PG_PASSWORD" "$PG_NAME" \
    psql -U postgres -c "CREATE DATABASE oztest;" -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null \
    || { fail "could not seed throwaway PG"; return; }
  db_host="$(pick_db_host)" || { fail "no container-reachable route to throwaway PG"; return; }
  db_url="postgresql://postgres:${PG_PASSWORD}@${db_host}:${PG_PORT}/oztest"
  out="$(printf 'y\nn\n%s\n\n\n%s\n%s\ny\n' "$db_url" "$API_PORT" "$FRONTEND_PORT" \
    | setsid bash "$INSTALL_SH" --dir "$d" 2>&1)"; rc="$?"
  if [[ "$rc" -ne 0 ]]; then fail "install exited ${rc}"; return; fi
  if wait_for_health "http://127.0.0.1:${API_PORT}" 120; then
    ok "external-DB install green, GET /health == 200"
  else
    fail "GET /health never 200 (external DB)"
  fi
  docker rm -f "$PG_NAME" >/dev/null 2>&1 || true
}

# T8 backend-only: frontend answer n -> no frontend container, /health 200, disabled.
t8_backend_only() {
  step "T8 backend-only"
  local d="${SANDBOX_BASE}/8" out rc
  rm -rf "$d"; mkdir -p "$d"
  out="$(printf 'n\ny\n\n\n%s\n%s\n' "$API_PORT" "$FRONTEND_PORT" \
    | setsid bash "$INSTALL_SH" --dir "$d" 2>&1)"; rc="$?"
  if [[ "$rc" -ne 0 ]]; then fail "install exited ${rc}"; return; fi
  if [[ -z "$(docker ps -a --filter name=openzync-frontend --format '{{.Names}}' 2>/dev/null)" ]]; then
    ok "no frontend container"
  else
    fail "frontend container exists"
  fi
  if wait_for_health "http://127.0.0.1:${API_PORT}" 120; then
    ok "GET /health == 200"
  else
    fail "GET /health never 200"
  fi
  out="$(bash "$INSTALL_SH" --dir "$d" --status 2>&1)"
  if [[ "$out" == *"frontend: disabled"* ]]; then ok "status prints disabled"; else fail "missing 'frontend: disabled'"; fi
}

# T9 uninstall paths on the T4 dir: n/n preserves volumes + dir; then wipe
# only suite-created volumes (snapshot diff — never blind down -v).
t9_uninstall() {
  step "T9 uninstall paths"
  local d="${SANDBOX_BASE}/4" out rc
  if [[ ! -f "${d}/install.env" ]]; then fail "T4 dir missing — run CASE=T4 first"; return; fi
  out="$(printf 'n\nn\n' | setsid bash "$INSTALL_SH" --dir "$d" --uninstall 2>&1)"; rc="$?"
  if [[ "$rc" -ne 0 ]]; then fail "uninstall exited ${rc}"; return; fi
  if [[ -z "$(docker ps -a --filter name=openzync -q 2>/dev/null)" ]]; then
    ok "containers gone after n/n uninstall"
  else
    fail "containers remain after uninstall"
  fi
  if [[ -d "$d" ]] && [[ -f "${d}/.env" ]]; then
    ok "volumes + dir preserved"
  else
    fail "install dir not preserved"
  fi
  local v removed=0
  while read -r v; do
    [[ -n "$v" ]] || continue
    if ! grep -qx "$v" "$VOL_SNAP"; then
      docker volume rm "$v" >/dev/null 2>&1 && removed=$((removed + 1)) || true
    fi
  done < <(docker volume ls -q 2>/dev/null | sort)
  ok "full wipe removed ${removed} suite-created volume(s), shared names untouched"
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --keep-on-failure) KEEP_ON_FAILURE=1; shift ;;
      -h | --help) echo "Usage: test_install.sh [--keep-on-failure]  (CASE=T1,T2 filter via env)"; exit 0 ;;
      *) die "unknown argument: $1" ;;
    esac
  done
  [[ -f "$INSTALL_SH" ]] || die "installer not found: ${INSTALL_SH} (run from repo root)"
  preflight
  should_run T1 && t1_arg_validation
  should_run T2 && t2_status_no_install
  should_run T3 && t3_port_conflict
  should_run T4 && t4_full_local_db
  should_run T5 && t5_status_green
  should_run T6 && t6_idempotent_rerun
  should_run T7 && t7_external_db
  should_run T8 && t8_backend_only
  should_run T9 && t9_uninstall
  echo ""
  log "done: ${PASS_COUNT} passed, ${FAIL_COUNT} failed, ${WARN_COUNT} warnings"
  [[ "$FAIL_COUNT" -eq 0 ]]
}

main "$@"
