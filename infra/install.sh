#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# OpenZync — production one-liner installer (full stack, single host)
# ─────────────────────────────────────────────────────────────────────────────
# One-liner (runs from a checkout — compose files ship with the repos):
#   git clone <openzync-core-url> openzync-core && ./openzync-core/infra/install.sh
#   ./openzync-core/infra/install.sh [--yes] [--dir DIR] [--version TAG] [install]
#
# Scope: backend = infra/docker-compose.backend.yml, frontend =
# ../openzync-frontend/deploy/docker-compose.yml referenced by its GHCR
# image only — frontend source is never vendored here (AGPLv3/MIT boundary).
#
# ⚠️ api/worker/frontend tags float on :latest — a re-pull can move the
# digest under you. Pass --version TAG to pin; the script pulls :TAG and
# re-tags it to :latest locally so the compose files resolve to that digest.
#
# TLS is OUT OF SCOPE — HTTP on loopback only. Terminate TLS upstream
# (reverse proxy / load balancer) in production.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
# note: BASH_COMMAND is intentionally NOT echoed here — it can carry secrets.
trap 'die "failed at line ${LINENO}"' ERR

SCRIPT_VERSION="1.0.0"
DEFAULT_DIR="${HOME}/.openzync"
DEFAULT_API_PORT="8000"
DEFAULT_FRONTEND_PORT="3000"

# Public on ghcr.io — docker pull needs no auth.
API_IMAGE="ghcr.io/openzync/openzync-core/api"
WORKER_IMAGE="ghcr.io/openzync/openzync-core/worker"
FRONTEND_IMAGE="ghcr.io/openzync/frontend"

YES=0
INSTALL_DIR="$DEFAULT_DIR"
IMAGE_TAG="latest"
SUBCOMMAND="install"

# Globals filled by prompts (install) or install.env (status/uninstall).
OVERRIDE_BACKEND=""
OVERRIDE_FRONTEND=""
FRONTEND_ON=1
USE_LOCAL_DB=0
DB_URL=""
CORS=""
HOSTS=""
API_PORT="$DEFAULT_API_PORT"
FRONTEND_PORT="$DEFAULT_FRONTEND_PORT"
BACKEND_COMPOSE=""
FRONTEND_COMPOSE=""

log() { printf '[install] %s %s\n' "$(date -Iseconds)" "$*"; }
die() { printf '[install] FATAL: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: install.sh [--yes] [--dir DIR] [--version TAG] [install|--uninstall|--status]

  install      prompt for choices, write secrets, pull images, compose up (default)
  --uninstall  compose down (asks before removing volumes or the install dir)
  --status     container + /health + /ready probe report
  --yes        non-interactive: accept every default (CI)
  --dir DIR    install dir for .env + pointers (default ~/.openzync)
  --version T  image tag to deploy (default latest)
EOF
}

# ── Args ─────────────────────────────────────────────────────────────────────
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h | --help) usage; exit 0 ;;
            -y | --yes) YES=1; shift ;;
            --dir) INSTALL_DIR="${2:?--dir needs a value}"; shift 2 ;;
            --dir=*) INSTALL_DIR="${1#--dir=}"; shift ;;
            --version)
                IMAGE_TAG="${2:?--version needs a tag (e.g. --version v1.2.3)}"; shift 2 ;;
            --version=*) IMAGE_TAG="${1#--version=}"; shift ;;
            --uninstall | uninstall) SUBCOMMAND="uninstall"; shift ;;
            --status | status) SUBCOMMAND="status"; shift ;;
            install) SUBCOMMAND="install"; shift ;;
            *) die "unknown argument: $1 (see --help)" ;;
        esac
    done
}

# ── Prompt (defaults win under --yes; reads from tty when piped) ─────────────
prompt() {
    local text="$1" def="$2" ans
    if [[ "$YES" -eq 1 ]]; then printf '%s\n' "$def"; return 0; fi
    if [[ -r /dev/tty ]]; then read -r -p "[install] ${text} [${def}]: " ans </dev/tty || true
    else read -r -p "[install] ${text} [${def}]: " ans || true; fi
    printf '%s\n' "${ans:-$def}"
}

confirm() {
    local text="$1" def="$2" ans
    ans="$(prompt "$text (y/n)" "$def")"
    [[ "$ans" =~ ^[Yy] ]]
}

# ── Idempotency guards (idioms mirror scripts/dev_preflight.sh) ──────────────
container_exists() { docker ps -a --format '{{.Names}}' | grep -qx "$1"; }
container_running() { docker ps --format '{{.Names}}' | grep -qx "$1"; }
ensure_volume() { docker volume inspect "$1" >/dev/null 2>&1 || docker volume create "$1" >/dev/null; }

# Portable listener probe (ss → lsof → nc → /dev/tcp); ss covers IPv4+IPv6.
port_in_use() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -tln 2>/dev/null | grep -Eq "[:.]${port}[[:space:]]"
    elif command -v lsof >/dev/null 2>&1; then
        lsof -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1
    elif command -v nc >/dev/null 2>&1; then
        nc -z 127.0.0.1 "$port" >/dev/null 2>&1
    else
        (exec 3<>"/dev/tcp/127.0.0.1/${port}") >/dev/null 2>&1
    fi
}

# ── OS + prerequisites (distro repos only — no third-party Docker CE repo) ───
detect_pm() {
    if [[ "$(uname -s)" == "Darwin" ]]; then printf 'brew\n'; return 0; fi
    local id="" like=""
    if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        source /etc/os-release; id="${ID:-}"; like="${ID_LIKE:-}"
    fi
    case " $id $like " in
        *" debian"* | *" ubuntu"*) printf 'apt\n' ;;
        *" fedora"* | *" rhel"* | *" centos"*) printf 'dnf\n' ;;
        *" arch"*) printf 'pacman\n' ;;
        *) die "unsupported OS (ID=${id:-unknown}) — install curl git openssl python3 docker + compose v2 manually" ;;
    esac
}

install_prereqs() {
    local pm="$1" sudo=""
    if [[ "$(id -u)" -ne 0 ]]; then sudo="sudo"; fi
    case "$pm" in
        apt) $sudo apt-get update -qq && $sudo apt-get install -y -qq curl git openssl python3 docker.io docker-compose-plugin ;;
        dnf) $sudo dnf install -y curl git openssl python3 docker docker-compose-plugin ;;
        pacman) $sudo pacman -Sy --noconfirm curl git openssl python docker docker-compose ;;
        brew) brew install curl git openssl python3 docker docker-compose ;;
    esac
    # note: brew ships the docker CLI only — start Docker Desktop or Colima.
}

ensure_prereqs() {
    local missing=() c
    for c in curl git openssl python3 docker; do
        command -v "$c" >/dev/null 2>&1 || missing+=("$c")
    done
    if ! docker compose version >/dev/null 2>&1; then missing+=("docker-compose-plugin"); fi
    if [[ "${#missing[@]}" -eq 0 ]]; then log "prerequisites ok"; return 0; fi
    log "missing: ${missing[*]}"
    if [[ "$YES" -eq 1 ]] || confirm "Install missing packages via system repo?" "y"; then
        install_prereqs "$(detect_pm)"
    else
        die "cannot continue without: ${missing[*]}"
    fi
    for c in curl git openssl python3 docker; do
        command -v "$c" >/dev/null 2>&1 || die "install did not provide $c — install it manually"
    done
    docker compose version >/dev/null 2>&1 || die "docker compose v2 still missing — install it manually"
    if ! docker info >/dev/null 2>&1; then
        if [[ "$(uname -s)" != "Darwin" ]] && command -v systemctl >/dev/null 2>&1; then
            log "starting docker daemon ..."
            if [[ "$(id -u)" -ne 0 ]]; then
                sudo systemctl start docker
            else
                systemctl start docker
            fi
        fi
        docker info >/dev/null 2>&1 || die "docker daemon unreachable — start it and re-run"
    fi
}

# ── Secrets (exactly the 4 bootstrap vars — nothing else lands in .env) ──────
gen_hex() { openssl rand -hex 32; }
gen_b64() { openssl rand -base64 32; }
gen_url() { python3 -c 'import secrets,sys;print(secrets.token_urlsafe(int(sys.argv[1])))' "$1"; }

# Reuse the existing value unless the operator asks for a fresh one.
secret_value() {
    local name="$1" current="$2" gen="$3" ans
    if [[ -z "$current" ]]; then eval "$gen"; return 0; fi
    if [[ "$YES" -eq 1 ]]; then printf '%s\n' "$current"; return 0; fi
    if [[ -r /dev/tty ]]; then read -r -p "[install] Reuse existing ${name}? [Y/n]: " ans </dev/tty || true
    else read -r -p "[install] Reuse existing ${name}? [Y/n]: " ans || true; fi
    if [[ "${ans:-Y}" =~ ^[Yy]$ ]]; then printf '%s\n' "$current"; else eval "$gen"; fi
}

# Secrets/pointer files are written 0600 — warn (don't fail) if loosened
# before sourcing, since `source` executes file contents in this shell.
warn_unless_0600() {
    local f="$1" mode=""
    mode="$(stat -c %a "$f" 2>/dev/null || stat -f %Lp "$f" 2>/dev/null || true)"
    if [[ -n "$mode" ]] && [[ "$mode" != "600" ]]; then
        log "WARN: ${f} has mode ${mode} (expected 600)"
    fi
}

load_existing_env() {
    if [[ -f "$ENV_FILE" ]]; then
        warn_unless_0600 "$ENV_FILE"
        # shellcheck disable=SC1090
        set -a; source "$ENV_FILE"; set +a
    fi
}

write_env() {
    local seal="$1" pgpw="$2" secret="$3" hook="$4"
    umask 077
    {
        printf '# OpenZync production bootstrap secrets — generated by infra/install.sh. 0600.\n'
        printf 'BAO_STATIC_SEAL_KEY=%s\n' "$seal"
        printf 'POSTGRES_PASSWORD=%s\n' "$pgpw"
        printf 'OZ_SECRET_KEY=%s\n' "$secret"
        printf 'OZ_WEBHOOK_SIGNING_SECRET=%s\n' "$hook"
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
}

# Quote for safe re-sourcing from install.env.
esc() { printf "'%s'" "${1//\'/\'\\\'\'}"; }

write_pointers() {
    umask 077
    {
        printf '# OpenZync install pointers — written by infra/install.sh. 0600.\n'
        printf 'FRONTEND_ENABLED=%s\n' "$FRONTEND_ON"
        printf 'USE_LOCAL_DB=%s\n' "$USE_LOCAL_DB"
        printf 'OZ_DATABASE_URL=%s\n' "$(esc "$DB_URL")"
        printf 'OZ_CORS_ORIGINS=%s\n' "$(esc "$CORS")"
        printf 'OZ_HOSTS_ALLOWED=%s\n' "$(esc "$HOSTS")"
        printf 'API_PORT=%s\n' "$API_PORT"
        printf 'FRONTEND_PORT=%s\n' "$FRONTEND_PORT"
        printf 'IMAGE_TAG=%s\n' "$IMAGE_TAG"
        printf 'BACKEND_COMPOSE=%s\n' "$(esc "$BACKEND_COMPOSE")"
        printf 'FRONTEND_COMPOSE=%s\n' "$(esc "$FRONTEND_COMPOSE")"
    } > "$POINTERS_FILE"
    chmod 600 "$POINTERS_FILE"
}

# ── Compose helpers ──────────────────────────────────────────────────────────
compose_backend() {
    # shellcheck disable=SC2086
    OZ_DATABASE_URL="$DB_URL" OZ_CORS_ORIGINS="$CORS" OZ_HOSTS_ALLOWED="$HOSTS" \
        docker compose --env-file "$ENV_FILE" -f "$BACKEND_COMPOSE" $OVERRIDE_BACKEND "$@"
}

compose_frontend() {
    # shellcheck disable=SC2086
    docker compose --env-file "$ENV_FILE" -f "$FRONTEND_COMPOSE" $OVERRIDE_FRONTEND "$@"
}

wait_healthy() {
    local base="http://127.0.0.1:${API_PORT}" i
    log "waiting for ${base}/health (up to ~120s) ..."
    for i in $(seq 1 60); do
        if curl -sf "${base}/health" >/dev/null 2>&1; then break; fi
        if [[ "$i" -eq 60 ]]; then die "api never healthy — run: docker logs openzync-api"; fi
        sleep 2
    done
    log "waiting for ${base}/ready (up to ~120s) ..."
    for i in $(seq 1 60); do
        if curl -sf "${base}/ready" >/dev/null 2>&1; then break; fi
        if [[ "$i" -eq 60 ]]; then die "api never ready — run: docker logs openzync-api"; fi
        sleep 2
    done
    log "api healthy + ready"
}

pull_images() {
    docker pull "${API_IMAGE}:${IMAGE_TAG}"
    docker pull "${WORKER_IMAGE}:${IMAGE_TAG}"
    if [[ "$FRONTEND_ON" -eq 1 ]]; then docker pull "${FRONTEND_IMAGE}:${IMAGE_TAG}"; fi
    if [[ "$IMAGE_TAG" != "latest" ]]; then
        # Compose files pin :latest — re-tag so they resolve to this digest.
        docker tag "${API_IMAGE}:${IMAGE_TAG}" "${API_IMAGE}:latest"
        docker tag "${WORKER_IMAGE}:${IMAGE_TAG}" "${WORKER_IMAGE}:latest"
        if [[ "$FRONTEND_ON" -eq 1 ]]; then docker tag "${FRONTEND_IMAGE}:${IMAGE_TAG}" "${FRONTEND_IMAGE}:latest"; fi
    fi
}

# ── Subcommands ──────────────────────────────────────────────────────────────
do_install() {
    log "openzync installer v${SCRIPT_VERSION}"
    ensure_prereqs
    mkdir -p "$INSTALL_DIR"

    # Local compose files only — nothing is fetched at runtime except pulls.
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
    BACKEND_COMPOSE="${REPO_ROOT}/infra/docker-compose.backend.yml"
    FRONTEND_COMPOSE="$( { cd "${REPO_ROOT}/../openzync-frontend/deploy" && pwd; } 2>/dev/null || true)/docker-compose.yml"
    if [[ ! -f "$BACKEND_COMPOSE" ]]; then
        SRC_DIR="${INSTALL_DIR}/src"
        CORE_REF="${OZ_INSTALL_REF:-master}"
        mkdir -p "$SRC_DIR"
        if [[ ! -f "${SRC_DIR}/openzync-core/infra/docker-compose.backend.yml" ]]; then
            log "backend compose not found — cloning openzync-core (${CORE_REF}) into ${SRC_DIR}/openzync-core ..."
            git clone --depth 1 -b "$CORE_REF" https://github.com/openzync/openzync-core.git "${SRC_DIR}/openzync-core" || die "failed to clone openzync-core (${CORE_REF}) — check network/git and re-run"
        else
            git -C "${SRC_DIR}/openzync-core" pull --ff-only >/dev/null 2>&1 || log "WARN: failed to update ${SRC_DIR}/openzync-core — continuing with existing checkout"
        fi
        [[ -f "${SRC_DIR}/openzync-core/infra/docker-compose.backend.yml" ]] || die "backend compose still missing after checkout — expected ${SRC_DIR}/openzync-core/infra/docker-compose.backend.yml"
        REPO_ROOT="${SRC_DIR}/openzync-core"
        BACKEND_COMPOSE="${REPO_ROOT}/infra/docker-compose.backend.yml"
        FRONTEND_COMPOSE="$( { cd "${REPO_ROOT}/../openzync-frontend/deploy" && pwd; } 2>/dev/null || true)/docker-compose.yml"
        if [[ ! -f "${FRONTEND_COMPOSE:-/nonexistent}" ]] && [[ ! -f "${SRC_DIR}/openzync-frontend/deploy/docker-compose.yml" ]]; then
            log "frontend checkout not found — cloning openzync-frontend (${CORE_REF}) into ${SRC_DIR}/openzync-frontend ..."
            git clone --depth 1 -b "$CORE_REF" https://github.com/openzync/openzync-frontend.git "${SRC_DIR}/openzync-frontend" >/dev/null 2>&1 || log "WARN: failed to clone openzync-frontend — continuing without frontend"
        elif [[ -f "${SRC_DIR}/openzync-frontend/deploy/docker-compose.yml" ]]; then
            git -C "${SRC_DIR}/openzync-frontend" pull --ff-only >/dev/null 2>&1 || log "WARN: failed to update ${SRC_DIR}/openzync-frontend — continuing with existing checkout"
        fi
        if [[ ! -f "${FRONTEND_COMPOSE:-/nonexistent}" ]] && [[ -f "${SRC_DIR}/openzync-frontend/deploy/docker-compose.yml" ]]; then
            FRONTEND_COMPOSE="${SRC_DIR}/openzync-frontend/deploy/docker-compose.yml"
        fi
    fi

    # ── Interactive choices (every choice prompted; sane defaults) ──
    local front_ans db_ans front_env
    front_ans="$(prompt 'Install frontend dashboard?' 'y')"
    FRONTEND_ON=0; if [[ "$front_ans" =~ ^[Yy] ]]; then FRONTEND_ON=1; fi
    if [[ ! -f "${FRONTEND_COMPOSE:-/nonexistent}" ]]; then
        log "WARN: frontend compose not found next to checkout — frontend disabled"
        FRONTEND_ON=0; FRONTEND_COMPOSE=""
    fi
    db_ans="$(prompt 'Use bundled local Postgres (local-db profile)?' 'n')"
    USE_LOCAL_DB=0; if [[ "$db_ans" =~ ^[Yy] ]]; then USE_LOCAL_DB=1; fi
    if [[ "$USE_LOCAL_DB" -eq 1 ]]; then DB_URL=""
    else DB_URL="$(prompt 'External OZ_DATABASE_URL' "${OZ_DATABASE_URL:-}")"
    fi
    CORS="$(prompt 'OZ_CORS_ORIGINS' "${OZ_CORS_ORIGINS:-http://localhost:3000}")"
    HOSTS="$(prompt 'OZ_HOSTS_ALLOWED' "${OZ_HOSTS_ALLOWED:-localhost:8000}")"
    API_PORT="$(prompt 'Host port for API (8000)' "$DEFAULT_API_PORT")"
    FRONTEND_PORT="$(prompt 'Host port for frontend (3000)' "$DEFAULT_FRONTEND_PORT")"
    [[ "$API_PORT" =~ ^[0-9]+$ ]] || die "API port must be numeric, got: ${API_PORT}"
    [[ "$FRONTEND_PORT" =~ ^[0-9]+$ ]] || die "frontend port must be numeric, got: ${FRONTEND_PORT}"
    if [[ "$USE_LOCAL_DB" -eq 0 ]] && [[ -z "$DB_URL" ]]; then
        log "WARN: empty OZ_DATABASE_URL — the system secret will skip DATABASE_URL"
    fi

    # ── Secrets: never overwrite an existing .env without confirmation ──
    ENV_FILE="${INSTALL_DIR}/.env"
    load_existing_env
    if [[ -f "$ENV_FILE" ]] && [[ "$YES" -eq 0 ]]; then
        if ! confirm "Overwrite existing ${ENV_FILE} with new secrets?" "n"; then
            log "keeping existing .env — regenerating only empty/missing values on request"
        fi
    fi
    SEAL="$(secret_value BAO_STATIC_SEAL_KEY "${BAO_STATIC_SEAL_KEY:-}" 'gen_hex')"
    PGPW="$(secret_value POSTGRES_PASSWORD "${POSTGRES_PASSWORD:-}" 'gen_b64')"
    SKEY="$(secret_value OZ_SECRET_KEY "${OZ_SECRET_KEY:-}" 'gen_url 48')"
    HOOK="$(secret_value OZ_WEBHOOK_SIGNING_SECRET "${OZ_WEBHOOK_SIGNING_SECRET:-}" 'gen_url 32')"
    write_env "$SEAL" "$PGPW" "$SKEY" "$HOOK"
    log "wrote 4 bootstrap vars to ${ENV_FILE} (0600)"

    POINTERS_FILE="${INSTALL_DIR}/install.env"
    OVERRIDE_BACKEND=""
    OVERRIDE_FRONTEND=""
    # Per-stack port overrides: the backend file touches only `api` and the
    # frontend file only `frontend`, so neither stack sees an alien service
    # from the other compose file.
    if [[ "$API_PORT" != "$DEFAULT_API_PORT" ]]; then
        printf 'services:\n  api:\n    ports: ["127.0.0.1:%s:8000"]\n' "$API_PORT" \
            > "${INSTALL_DIR}/docker-compose.ports.backend.yml"
        OVERRIDE_BACKEND="-f ${INSTALL_DIR}/docker-compose.ports.backend.yml"
    fi
    if [[ "$FRONTEND_ON" -eq 1 ]] && [[ "$FRONTEND_PORT" != "$DEFAULT_FRONTEND_PORT" ]]; then
        printf 'services:\n  frontend:\n    ports: ["127.0.0.1:%s:3000"]\n' "$FRONTEND_PORT" \
            > "${INSTALL_DIR}/docker-compose.ports.frontend.yml"
        OVERRIDE_FRONTEND="-f ${INSTALL_DIR}/docker-compose.ports.frontend.yml"
    fi
    write_pointers

    # ── Port guards: fail fast with a fix hint (same style as dev_preflight) ──
    if port_in_use "$API_PORT"; then
        die "port 127.0.0.1:${API_PORT} is already in use — fix: docker stop <container>"
    fi
    if [[ "$FRONTEND_ON" -eq 1 ]] && port_in_use "$FRONTEND_PORT"; then
        die "port 127.0.0.1:${FRONTEND_PORT} is already in use — fix: docker stop <container>"
    fi

    # Named volumes exist before compose (safe re-run; compose would also make them).
    ensure_volume openbao-data; ensure_volume openbao-init-data
    ensure_volume api-secrets; ensure_volume worker-secrets
    ensure_volume redis-data; ensure_volume falkordb-data
    if [[ "$USE_LOCAL_DB" -eq 1 ]]; then ensure_volume postgres-data; fi

    # Frontend compose ships `env_file: .env` — seed an empty one if absent.
    if [[ "$FRONTEND_ON" -eq 1 ]]; then
        front_env="$(dirname "$FRONTEND_COMPOSE")/.env"
        if [[ ! -f "$front_env" ]]; then
            if [[ "$YES" -eq 1 ]] || confirm "Create empty ${front_env} (required by frontend compose)?" "y"; then
                umask 077; : > "$front_env"; chmod 600 "$front_env"
            else
                die "frontend compose requires ${front_env} — create it and re-run"
            fi
        fi
    fi

    pull_images

    if [[ "$USE_LOCAL_DB" -eq 1 ]]; then PROFILE_ARGS=(--profile local-db)
    else PROFILE_ARGS=(); fi
    # shellcheck disable=SC2206
    compose_backend "${PROFILE_ARGS[@]}" up -d
    if [[ "$FRONTEND_ON" -eq 1 ]]; then compose_frontend up -d; fi

    wait_healthy

    cat <<EOF
[install] Done.
  API:      http://127.0.0.1:${API_PORT}  (/health, /ready)
  OpenBao:  http://127.0.0.1:8200
EOF
    if [[ "$FRONTEND_ON" -eq 1 ]]; then
        printf '[install] Frontend: http://127.0.0.1:%s\n' "$FRONTEND_PORT"
    fi
    cat <<EOF
[install] Next steps:
  status:   $0 --dir ${INSTALL_DIR} --status
  logs:     docker logs openzync-api   # worker: docker logs openzync-worker
  uninstall:$0 --dir ${INSTALL_DIR} --uninstall
  secrets:  ${INSTALL_DIR}/.env (0600 — back it up; losing BAO_STATIC_SEAL_KEY loses all secrets)
  note:     HTTP only — terminate TLS at your reverse proxy.
EOF
}

do_status() {
    POINTERS_FILE="${INSTALL_DIR}/install.env"
    if [[ -f "$POINTERS_FILE" ]]; then
        warn_unless_0600 "$POINTERS_FILE"
        # shellcheck disable=SC1090
        set -a; source "$POINTERS_FILE"; set +a
    fi
    API_PORT="${API_PORT:-$DEFAULT_API_PORT}"
    FRONTEND_PORT="${FRONTEND_PORT:-$DEFAULT_FRONTEND_PORT}"
    local base="http://127.0.0.1:${API_PORT}"
    docker ps --filter name=openzync --format '{{.Names}}: {{.Status}}'
    if curl -sf "${base}/health" >/dev/null 2>&1; then echo "api /health: UP"; else echo "api /health: DOWN"; fi
    if curl -sf "${base}/ready" >/dev/null 2>&1; then echo "api /ready: UP"; else echo "api /ready: DOWN"; fi
    if [[ "${FRONTEND_ENABLED:-1}" -eq 1 ]]; then
        if curl -sf "http://127.0.0.1:${FRONTEND_PORT}" >/dev/null 2>&1; then echo "frontend: UP"; else echo "frontend: DOWN"; fi
    else
        echo "frontend: disabled"
    fi
}

do_uninstall() {
    POINTERS_FILE="${INSTALL_DIR}/install.env"
    if [[ -f "$POINTERS_FILE" ]]; then
        warn_unless_0600 "$POINTERS_FILE"
        # shellcheck disable=SC1090
        set -a; source "$POINTERS_FILE"; set +a
    fi
    ENV_FILE="${INSTALL_DIR}/.env"
    BACKEND_COMPOSE="${BACKEND_COMPOSE:-}"
    FRONTEND_COMPOSE="${FRONTEND_COMPOSE:-}"
    OVERRIDE_BACKEND=""
    OVERRIDE_FRONTEND=""
    if [[ -f "${INSTALL_DIR}/docker-compose.ports.backend.yml" ]]; then
        OVERRIDE_BACKEND="-f ${INSTALL_DIR}/docker-compose.ports.backend.yml"
    fi
    if [[ -f "${INSTALL_DIR}/docker-compose.ports.frontend.yml" ]]; then
        OVERRIDE_FRONTEND="-f ${INSTALL_DIR}/docker-compose.ports.frontend.yml"
    fi
    [[ -n "$BACKEND_COMPOSE" ]] && [[ -f "$BACKEND_COMPOSE" ]] || die "no install pointers found in ${INSTALL_DIR}"
    if [[ "${USE_LOCAL_DB:-0}" -eq 1 ]]; then PROFILE_ARGS=(--profile local-db)
    else PROFILE_ARGS=(); fi
    # shellcheck disable=SC2206
    compose_backend "${PROFILE_ARGS[@]}" down
    if [[ "${FRONTEND_ENABLED:-0}" -eq 1 ]] && [[ -f "$FRONTEND_COMPOSE" ]]; then
        compose_frontend down
    fi
    if [[ "$YES" -eq 1 ]] || confirm "Remove named volumes (deletes ALL data)?" "n"; then
        # shellcheck disable=SC2206
        compose_backend "${PROFILE_ARGS[@]}" down -v
    fi
    if [[ "$YES" -eq 0 ]] && confirm "Remove install dir ${INSTALL_DIR}?" "n"; then
        case "${INSTALL_DIR}" in
            "" | "/" | "${HOME}") die "refusing to remove install dir '${INSTALL_DIR}'" ;;
        esac
        rm -rf "$INSTALL_DIR"
    fi
    log "uninstalled (volumes preserved unless you confirmed removal)"
}

main() {
    parse_args "$@"
    ENV_FILE="${INSTALL_DIR}/.env"
    POINTERS_FILE="${INSTALL_DIR}/install.env"
    case "$SUBCOMMAND" in
        install) do_install ;;
        status) do_status ;;
        uninstall) do_uninstall ;;
    esac
}

main "$@"
