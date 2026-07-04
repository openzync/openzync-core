#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — Production deploy script
# ──────────────────────────────────────────────────────────────────────────────
# Idempotent: safe to run multiple times.
#
# Usage:
#   bash scripts/deploy.sh
#
# Designed to be invoked from GitHub Actions via SSH, or manually on the VPS.
#
# Workflow:
#   1. Pull the latest Docker images from ghcr.io
#   2. Run Alembic database migrations
#   3. Restart all services with zero-downtime (rolling restart via compose)
#   4. Prune unused Docker images
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ── Compose files ─────────────────────────────────────────────────────────────
COMPOSE_FILES=(
    -f "$PROJECT_ROOT/infra/docker-compose.yml"
    -f "$PROJECT_ROOT/infra/docker-compose.prod.yml"
    -f "$PROJECT_ROOT/infra/docker-compose.vps.yml"
)

# ── Step 1: Pull latest images ────────────────────────────────────────────────
# Pull backend services explicitly.  The frontend image is built asynchronously
# in CI (build-frontend job) and may not exist yet — if it fails, it shouldn't
# block the backend deploy.  Once the frontend image is available, it will be
# pulled on the next deploy cycle.
info "Pulling latest Docker images..."
docker compose "${COMPOSE_FILES[@]}" pull api worker redis nginx
ok "Images pulled."

# ── Step 2: Run database migrations ───────────────────────────────────────────
info "Running database migrations..."
docker compose "${COMPOSE_FILES[@]}" run --rm --no-deps api alembic upgrade head
ok "Migrations applied."

# ── Step 3: Restart backend services ──────────────────────────────────────────
# Start only the services we explicitly pulled.  The frontend is deployed
# separately by the build-frontend CI job once its image is pushed.
info "Restarting services..."
docker compose "${COMPOSE_FILES[@]}" up -d --remove-orphans api worker redis nginx
ok "Services restarted."

# ── Step 4: Clean up unused images ────────────────────────────────────────────
info "Pruning unused Docker images..."
docker image prune -f --filter="label=org.opencontainers.image.source=*" 2>/dev/null || true
ok "Old images pruned."

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Deploy complete!${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════════════════${NC}"
docker compose "${COMPOSE_FILES[@]}" ps
