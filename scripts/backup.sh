#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# OpenZync — PostgreSQL backup script
# ──────────────────────────────────────────────────────────────────────────────
# Creates compressed pg_dump archives and rotates old backups.
#
# Designed to be run from a cron job on the VPS:
#   0 3 * * * /home/deploy/openzync/scripts/backup.sh >> /var/log/openzync-backup.log 2>&1
#
# Dependencies:
#   - Docker (runs pg_dump via the running postgres container)
#   - The postgres container must be running and healthy
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Configurable defaults (override via env) ──────────────────────────────────
BACKUP_DIR="${BACKUP_DIR:-/backups}"
DB_CONTAINER="${DB_CONTAINER:-openzync-postgres-1}"
DB_USER="${DB_USER:-openzync}"
DB_NAME="${DB_NAME:-openzync}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/openzync_${TIMESTAMP}.dump"

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

info() { echo -e "${GREEN}[INFO]${NC}  $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; }

# ── Ensure backup directory exists ────────────────────────────────────────────
mkdir -p "$BACKUP_DIR"

# ── Check container is running ────────────────────────────────────────────────
if ! docker ps --format '{{.Names}}' | grep -q "^${DB_CONTAINER}$"; then
    err "Container '$DB_CONTAINER' is not running. Aborting."
    exit 1
fi

# ── Create backup ─────────────────────────────────────────────────────────────
info "Starting backup of database '$DB_NAME' on container '$DB_CONTAINER'..."

docker exec "$DB_CONTAINER" pg_dump \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    --compress=9 \
    --format=custom \
    --no-owner \
    --no-acl \
    > "$BACKUP_FILE"

# Verify backup is valid
if [ ! -s "$BACKUP_FILE" ]; then
    err "Backup file is empty or was not created. Aborting."
    rm -f "$BACKUP_FILE"
    exit 1
fi

BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
info "Backup created: $BACKUP_FILE ($BACKUP_SIZE)"

# ── Rotate old backups ────────────────────────────────────────────────────────
info "Removing backups older than $RETENTION_DAYS days..."
find "$BACKUP_DIR" -name "openzync_*.dump" -mtime "+${RETENTION_DAYS}" -delete
info "Rotation complete."

# ── Optional: Upload to remote storage ────────────────────────────────────────
# Uncomment and configure if you want off-site backups:
# if [ -n "${AWS_S3_BUCKET:-}" ]; then
#     aws s3 cp "$BACKUP_FILE" "s3://${AWS_S3_BUCKET}/openzync/${TIMESTAMP}.dump" --storage-class STANDARD_IA
#     info "Backup uploaded to S3: s3://${AWS_S3_BUCKET}/openzync/${TIMESTAMP}.dump"
# fi

info "Backup finished successfully."
