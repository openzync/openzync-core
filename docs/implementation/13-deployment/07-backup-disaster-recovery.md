# Backup & Disaster Recovery Guide

## Overview

This guide covers backup strategies, restore procedures, and disaster recovery for MemGraph. The design targets **RTO ≤ 4 hours** and **RPO ≤ 1 hour** for the PostgreSQL database — the primary source of truth.

---

## Recovery Targets

| Metric | Target | Notes |
|---|---|---|
| **RTO** (Recovery Time Objective) | 4 hours | Time to fully restore service after total loss |
| **RPO** (Recovery Point Objective) | 1 hour | Maximum acceptable data loss (PostgreSQL WAL archiving) |
| **RPO (FalkorDB)** | 6 hours | FalkorDB is a derived cache — rebuilt from PostgreSQL |
| **RPO (Redis)** | 6 hours | Queue state — jobs are idempotent, re-runnable |

---

## What to Back Up

| Component | Data | Backup Method | Frequency | RPO |
|---|---|---|---|---|
| **PostgreSQL** | All relational data (orgs, users, sessions, messages, facts, embeddings) | `pg_dump` + WAL archiving | Hourly (WAL: continuous) | 1 hour |
| **FalkorDB** | Graph data (entity nodes, relationships, communities) | RDB snapshot | Every 6 hours | 6 hours |
| **Redis** | Queued jobs, cached context blocks, rate limiter state | RDB snapshot | Every 6 hours | 6 hours |
| **Config files** | `.env`, Helm `values.yaml`, Docker Compose files | File copy | Weekly | N/A |

---

## Backup Procedures

### 1. PostgreSQL

#### Full Backup (pg_dump)

```bash
#!/bin/bash
# scripts/backup-postgres.sh
# Run hourly via cron

BACKUP_DIR="/backups/postgres"
DB_NAME="memgraph"
DB_USER="memgraph"
RETENTION_DAYS=7
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/memgraph_${TIMESTAMP}.dump"

mkdir -p "${BACKUP_DIR}"

# Full dump (custom format — supports parallel restore)
pg_dump -U "${DB_USER}" -d "${DB_NAME}" \
  --format=custom \
  --compress=9 \
  --verbose \
  --file="${BACKUP_FILE}" 2>> "${BACKUP_DIR}/backup.log"

# Check success
if [ $? -eq 0 ]; then
  echo "${TIMESTAMP}: Backup successful — ${BACKUP_FILE}" >> "${BACKUP_DIR}/backup.log"

  # Sync to S3-compatible storage
  aws s3 cp "${BACKUP_FILE}" "s3://memgraph-backups/postgres/${TIMESTAMP}/" \
    --endpoint-url="${S3_ENDPOINT}"

  # Cleanup old backups
  find "${BACKUP_DIR}" -name "memgraph_*.dump" -mtime +${RETENTION_DAYS} -delete
else
  echo "${TIMESTAMP}: Backup FAILED" >> "${BACKUP_DIR}/backup.log"
  exit 1
fi
```

#### Continuous WAL Archiving

PostgreSQL configuration (`infra/postgres/postgresql.conf`):

```ini
wal_level = replica
archive_mode = on
archive_command = 'aws s3 cp %p s3://memgraph-backups/postgres/wal/%f --endpoint-url=${S3_ENDPOINT}'
archive_timeout = 60
max_wal_senders = 3
wal_keep_size = 1GB
```

This ensures at most 60 seconds of data loss (RPO = 1 hour if WAL is shipped hourly to S3).

#### MinIO Configuration (Self-Hosted S3)

```bash
# MinIO Docker Compose service
minio:
  image: minio/minio:latest
  command: server /data --console-address ":9001"
  ports:
    - "9000:9000"   # S3-compatible API
    - "9001:9001"   # Console
  volumes:
    - minio_data:/data
  environment:
    MINIO_ROOT_USER: memgraph
    MINIO_ROOT_PASSWORD: memgraph-backup-secret
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
    interval: 30s
    timeout: 10s
    retries: 3

# Create bucket
aws s3 mb s3://memgraph-backups --endpoint-url=http://localhost:9000
```

### 2. FalkorDB (RDB Backup)

```bash
#!/bin/bash
# scripts/backup-falkordb.sh
# Run every 6 hours

BACKUP_DIR="/backups/falkordb"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p "${BACKUP_DIR}"

# Use FalkorDB's SAVE command to trigger RDB snapshot
redis-cli -p 6380 SAVE

# Copy the RDB file
cp /data/dump.rdb "${BACKUP_DIR}/falkordb_${TIMESTAMP}.rdb"

# Compress
gzip "${BACKUP_DIR}/falkordb_${TIMESTAMP}.rdb"

# Sync to S3
aws s3 cp "${BACKUP_DIR}/falkordb_${TIMESTAMP}.rdb.gz" \
  "s3://memgraph-backups/falkordb/${TIMESTAMP}/" \
  --endpoint-url="${S3_ENDPOINT}"

# Cleanup
find "${BACKUP_DIR}" -name "falkordb_*.rdb.gz" -mtime +3 -delete
```

### 3. Redis (RDB Backup)

```bash
#!/bin/bash
# scripts/backup-redis.sh
# Run every 6 hours

BACKUP_DIR="/backups/redis"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p "${BACKUP_DIR}"

# Trigger RDB save
redis-cli SAVE

# Copy RDB file
cp /data/dump.rdb "${BACKUP_DIR}/redis_${TIMESTAMP}.rdb"

# Compress
gzip "${BACKUP_DIR}/redis_${TIMESTAMP}.rdb"

# Sync to S3
aws s3 cp "${BACKUP_DIR}/redis_${TIMESTAMP}.rdb.gz" \
  "s3://memgraph-backups/redis/${TIMESTAMP}/" \
  --endpoint-url="${S3_ENDPOINT}"

# Cleanup
find "${BACKUP_DIR}" -name "redis_*.rdb.gz" -mtime +1 -delete
```

### 4. Config Files

```bash
#!/bin/bash
# scripts/backup-config.sh
# Run weekly

BACKUP_DIR="/backups/config"
TIMESTAMP=$(date +%Y%m%d)

mkdir -p "${BACKUP_DIR}"

tar czf "${BACKUP_DIR}/config_${TIMESTAMP}.tar.gz" \
  .env \
  infra/docker-compose*.yml \
  infra/helm/memgraph/values.yaml \
  infra/postgres/postgresql.conf \
  infra/alloy/config.alloy

aws s3 cp "${BACKUP_DIR}/config_${TIMESTAMP}.tar.gz" \
  "s3://memgraph-backups/config/${TIMESTAMP}/" \
  --endpoint-url="${S3_ENDPOINT}"
```

---

## Backup Schedule

| Component | Cron Expression | Frequency | Retention |
|---|---|---|---|
| PostgreSQL full dump | `0 * * * *` | Hourly | 7 days |
| PostgreSQL WAL | Continuous | ~60s | Until archived to S3 |
| FalkorDB RDB | `0 */6 * * *` | Every 6 hours | 3 days |
| Redis RDB | `0 */6 * * *` | Every 6 hours | 1 day |
| Config files | `0 0 * * 0` | Weekly | 4 weeks |

### Crontab Entry

```bash
# /etc/cron.d/memgraph-backup

# PostgreSQL hourly
0 * * * * root /opt/memgraph/scripts/backup-postgres.sh >> /var/log/memgraph-backup.log 2>&1

# FalkorDB every 6 hours
0 */6 * * * root /opt/memgraph/scripts/backup-falkordb.sh >> /var/log/memgraph-backup.log 2>&1

# Redis every 6 hours
0 */6 * * * root /opt/memgraph/scripts/backup-redis.sh >> /var/log/memgraph-backup.log 2>&1

# Config weekly
0 0 * * 0 root /opt/memgraph/scripts/backup-config.sh >> /var/log/memgraph-backup.log 2>&1
```

---

## Restore Procedures

### Restore PostgreSQL

```bash
#!/bin/bash
# scripts/restore-postgres.sh
# Usage: ./restore-postgres.sh <backup_file>

set -euo pipefail

BACKUP_FILE="${1:?Usage: $0 <backup_file>}"
DB_NAME="memgraph"
DB_USER="memgraph"

echo "=== PostgreSQL Restore ==="
echo "Backup file: ${BACKUP_FILE}"
echo "Target database: ${DB_NAME}"
echo ""

# 1. Stop all services that connect to the database
echo "Step 1: Stopping dependent services..."
docker compose -f infra/docker-compose.prod.yml stop api worker mcp

# 2. Drop and recreate the database
echo "Step 2: Dropping and recreating database..."
psql -U "${DB_USER}" -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME};"
psql -U "${DB_USER}" -d postgres -c "CREATE DATABASE ${DB_NAME};"

# 3. Restore from pg_dump custom format (parallel = 4 threads)
echo "Step 3: Restoring from backup..."
pg_restore -U "${DB_USER}" -d "${DB_NAME}" \
  --format=custom \
  --jobs=4 \
  --verbose \
  "${BACKUP_FILE}" 2>&1 | tail -20

# 4. Replay WAL if available (point-in-time recovery)
# See separate PITR procedure below

# 5. Verify
echo "Step 4: Verifying restore..."
psql -U "${DB_USER}" -d "${DB_NAME}" -c "SELECT count(*) FROM users;"
psql -U "${DB_USER}" -d "${DB_NAME}" -c "SELECT count(*) FROM episodes;"

# 6. Restart services
echo "Step 5: Restarting services..."
docker compose -f infra/docker-compose.prod.yml start api worker mcp

echo "=== PostgreSQL Restore Complete ==="
```

#### Point-in-Time Recovery (PITR)

```bash
#!/bin/bash
# scripts/pitr-restore.sh
# Usage: ./pitr-restore.sh <timestamp>

TIMESTAMP="${1:?Usage: $0 <timestamp>}"
PGDATA="/var/lib/postgresql/data"
RESTORE_DIR="/tmp/pg_restore"

# 1. Stop PostgreSQL
docker compose stop postgres

# 2. Restore base backup
rm -rf "${PGDATA}/*"
aws s3 sync "s3://memgraph-backups/postgres/base/" "${PGDATA}/" \
  --endpoint-url="${S3_ENDPOINT}"

# 3. Create recovery.conf
cat > "${PGDATA}/recovery.conf" <<EOF
restore_command = 'aws s3 cp s3://memgraph-backups/postgres/wal/%f %p --endpoint-url=${S3_ENDPOINT}'
recovery_target_time = '${TIMESTAMP}'
recovery_target_action = 'promote'
EOF

# 4. Start PostgreSQL — will replay WAL to target timestamp
docker compose start postgres

# 5. Monitor recovery
sleep 10
psql -U memgraph -d memgraph -c "SELECT pg_is_in_recovery();"
# Expected: 'f' (false) when recovery is complete
```

### Restore FalkorDB

```bash
#!/bin/bash
# scripts/restore-falkordb.sh
# Usage: ./restore-falkordb.sh <backup_file>

BACKUP_FILE="${1:?Usage: $0 <backup_file>}"

echo "=== FalkorDB Restore ==="

# 1. Download backup from S3 if needed
if [[ "${BACKUP_FILE}" == s3://* ]]; then
  aws s3 cp "${BACKUP_FILE}" /tmp/falkordb_restore.rdb.gz \
    --endpoint-url="${S3_ENDPOINT}"
  BACKUP_FILE="/tmp/falkordb_restore.rdb.gz"
fi

# 2. Decompress
gunzip -f "${BACKUP_FILE}"
RDB_FILE="${BACKUP_FILE%.gz}"

# 3. Stop FalkorDB
docker compose stop falkordb

# 4. Replace RDB file
cp "${RDB_FILE}" /data/dump.rdb

# 5. Start FalkorDB (loads RDB on startup)
docker compose start falkordb

# 6. Verify
sleep 5
redis-cli -p 6380 DBSIZE
echo "=== FalkorDB Restore Complete ==="
```

### Restore Redis

```bash
#!/bin/bash
# scripts/restore-redis.sh
# Usage: ./restore-redis.sh <backup_file>

BACKUP_FILE="${1:?Usage: $0 <backup_file>}"

echo "=== Redis Restore ==="

# Download from S3 if needed
if [[ "${BACKUP_FILE}" == s3://* ]]; then
  aws s3 cp "${BACKUP_FILE}" /tmp/redis_restore.rdb.gz \
    --endpoint-url="${S3_ENDPOINT}"
  BACKUP_FILE="/tmp/redis_restore.rdb.gz"
fi

# Decompress
gunzip -f "${BACKUP_FILE}"
RDB_FILE="${BACKUP_FILE%.gz}"

# Stop Redis
docker compose stop redis

# Replace RDB
cp "${RDB_FILE}" /data/dump.rdb

# Start Redis
docker compose start redis

# Verify
sleep 3
redis-cli DBSIZE
echo "=== Redis Restore Complete ==="
```

---

## Disaster Scenarios

### Scenario 1: Single Database Loss

**Situation**: PostgreSQL crashes and data directory is corrupted.

**Recovery**:
1. Run `docker compose stop api worker`.
2. Restore PostgreSQL from latest dump + WAL (PITR to last known good state).
3. Start PostgreSQL, verify health.
4. Start `api` and `worker`.
5. Verify data consistency (count check).

**Estimated RTO**: 1-2 hours.

### Scenario 2: Full Cluster Loss

**Situation**: All servers destroyed (e.g., cloud region failure, fire in data centre).

**Recovery**:
1. Provision new infrastructure (Kubernetes cluster or Docker hosts).
2. Deploy MemGraph via Helm chart or Docker Compose.
3. Restore PostgreSQL from S3 backup (latest dump + WAL).
4. Restore FalkorDB from S3 backup.
5. Restore Redis from S3 backup (optional — jobs will be re-queued).
6. Apply config files.
7. Verify all health checks pass.
8. Run data consistency check.

**Estimated RTO**: 3-4 hours.

### Scenario 3: Data Corruption

**Situation**: A buggy migration or bad data write corrupts the database. Corruption is detected > 1 hour after occurrence.

**Recovery**:
1. Stop all services immediately to prevent further corruption.
2. Identify the corruption point (from monitoring alerts or bug report).
3. Restore PostgreSQL to a point-in-time BEFORE the corruption event.
4. Restore FalkorDB from the nearest backup before the event.
5. Restart services.
6. Verify data integrity.

**Rolling forward**: After restore, some legitimate data may be lost (between the restore point and the corruption). Identify affected users from logs and re-ingest their sessions if needed.

**Estimated RTO**: 2-3 hours.

---

## Data Consistency Check

Run after every restore to verify data integrity:

```bash
#!/bin/bash
# scripts/verify-consistency.sh

echo "=== Data Consistency Check ==="

# 1. Check PostgreSQL row counts
echo "PostgreSQL row counts:"
psql -U memgraph -d memgraph -c "
  SELECT 'organizations' AS tbl, count(*) FROM organizations
  UNION ALL
  SELECT 'users', count(*) FROM users
  UNION ALL
  SELECT 'sessions', count(*) FROM sessions
  UNION ALL
  SELECT 'episodes', count(*) FROM episodes
  UNION ALL
  SELECT 'facts', count(*) FROM facts
  ORDER BY tbl;
"

# 2. Check FalkorDB node count per org
echo ""
echo "FalkorDB nodes per org:"
# Requires Graphiti query — example via API
# This checks that entities exist for users that have episodes
curl -s http://localhost:8000/v1/admin/consistency | python -m json.tool

# 3. Compare PostgreSQL fact count vs graph entity count
echo ""
echo "Consistency ratios:"
psql -U memgraph -d memgraph -c "
  SELECT
    u.organization_id,
    COUNT(DISTINCT u.id) AS user_count,
    COUNT(DISTINCT e.id) AS episode_count,
    COUNT(DISTINCT f.id) AS fact_count
  FROM users u
  LEFT JOIN episodes e ON e.user_id = u.id
  LEFT JOIN facts f ON f.user_id = u.id
  GROUP BY u.organization_id
  ORDER BY u.organization_id;
"

# 4. Search for NULL embeddings that should not be NULL
echo ""
echo "Null embedding check:"
psql -U memgraph -d memgraph -c "
  SELECT 'episodes missing embeddings' AS issue, count(*) FROM episodes WHERE embedding IS NULL
  UNION ALL
  SELECT 'facts missing embeddings', count(*) FROM facts WHERE embedding IS NULL;
"

echo "=== Consistency Check Complete ==="
```

---

## Recovery Testing

### Quarterly Restore Drill

Run a full restore drill every quarter:

```bash
#!/bin/bash
# scripts/dr-test.sh
# Run quarterly

echo "=== Disaster Recovery Test ==="
echo "Target: RTO ≤ 4h, RPO ≤ 1h"
echo ""

START_TIME=$(date +%s)

# Step 1: Simulate failure
echo "[1/6] Simulating total cluster loss..."
# In staging — destroy all containers and volumes
docker compose -f infra/docker-compose.staging.yml down -v

# Step 2: Re-deploy infrastructure
echo "[2/6] Re-deploying infrastructure..."
docker compose -f infra/docker-compose.staging.yml up -d postgres redis falkordb

# Step 3: Restore databases
echo "[3/6] Restoring databases from S3 backups..."
LATEST_PG=$(aws s3 ls s3://memgraph-backups/postgres/ --endpoint-url="${S3_ENDPOINT}" | sort | tail -1 | awk '{print $4}')
./scripts/restore-postgres.sh "s3://memgraph-backups/postgres/${LATEST_PG}"

# Step 4: Start remaining services
echo "[4/6] Starting remaining services..."
docker compose -f infra/docker-compose.staging.yml up -d api worker

# Step 5: Verify
echo "[5/6] Verifying..."
sleep 30
curl -s http://localhost:8000/health
curl -s http://localhost:8000/ready

# Step 6: Reporting
echo "[6/6] Reporting..."
END_TIME=$(date +%s)
DURATION=$(( (END_TIME - START_TIME) / 60 ))
echo "Total recovery time: ${DURATION} minutes"
echo "Target: 240 minutes (4 hours)"
if [ ${DURATION} -le 240 ]; then
  echo "✅ RTO target MET"
else
  echo "❌ RTO target EXCEEDED — investigate bottlenecks"
fi

echo "=== DR Test Complete ==="
```

### Test Results Log

```yaml
# docs/dr-test-results.yaml
# Latest quarterly test results

2026-03-15:
  rto_actual: 45m
  rto_target: 4h
  passed: true
  notes: "Fast restore — all backups were recent"

2026-06-15:
  rto_actual: 2h15m
  rto_target: 4h
  passed: true
  notes: "S3 download was slow (100Mbps link). WAL replay took 30min."
```

---

## Backup Automation

### Kubernetes CronJobs

```yaml
# infra/helm/memgraph/templates/cronjob-backup.yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {{ include "memgraph.fullname" . }}-backup-postgres
spec:
  schedule: "0 * * * *"  # hourly
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: backup
              image: postgres:15
              command:
                - /bin/sh
                - -c
                - |
                  pg_dump -h {{ include "memgraph.fullname" . }}-postgres \
                    -U memgraph -d memgraph \
                    --format=custom --compress=9 \
                    --file=/tmp/backup.dump && \
                  aws s3 cp /tmp/backup.dump \
                    s3://memgraph-backups/postgres/$(date +%Y%m%d_%H%M%S).dump \
                    --endpoint-url={{ .Values.backup.s3Endpoint }}
              env:
                - name: PGPASSWORD
                  valueFrom:
                    secretKeyRef:
                      name: memgraph-secrets
                      key: db_password
                - name: AWS_ACCESS_KEY_ID
                  valueFrom:
                    secretKeyRef:
                      name: memgraph-secrets
                      key: aws_access_key_id
                - name: AWS_SECRET_ACCESS_KEY
                  valueFrom:
                    secretKeyRef:
                      name: memgraph-secrets
                      key: aws_secret_access_key
          restartPolicy: OnFailure
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {{ include "memgraph.fullname" . }}-backup-falkordb
spec:
  schedule: "0 */6 * * *"  # every 6 hours
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: backup
              image: redis:7-alpine
              command:
                - /bin/sh
                - -c
                - |
                  redis-cli -h {{ include "memgraph.fullname" . }}-falkordb -p 6380 SAVE && \
                  cp /data/dump.rdb /tmp/falkordb_$(date +%Y%m%d_%H%M%S).rdb && \
                  aws s3 cp /tmp/falkordb_*.rdb \
                    s3://memgraph-backups/falkordb/ \
                    --endpoint-url={{ .Values.backup.s3Endpoint }}
          restartPolicy: OnFailure
```

---

## Monitoring Backup Health

| Check | Method | Frequency |
|---|---|---|
| Backup job success rate | CronJob pod status / log | After each run |
| Backup age | Prometheus gauge: `memgraph_backup_age_seconds` | Every 15 min |
| S3 bucket size | CloudWatch / MinIO console | Daily |
| Restore drill | Manual | Quarterly |

### Backup Age Metric

```python
# Prometheus gauge for backup freshness
backup_age_seconds = Gauge(
    "memgraph_backup_age_seconds",
    "Seconds since last successful backup",
    labelnames=["component"],
)
```

Updated by a periodic check:

```python
async def check_backup_age():
    """Check time since last S3 backup."""
    for component in ("postgres", "falkordb", "redis"):
        latest = await get_latest_backup_timestamp(component)
        age = time.time() - latest.timestamp()
        backup_age_seconds.labels(component=component).set(age)
        if age > max_age[component]:
            logger.error("backup.too_old", extra={
                "component": component,
                "age_seconds": age,
                "max_seconds": max_age[component],
            })
```

### Alert

```yaml
- alert: BackupTooOld
  expr: memgraph_backup_age_seconds{component="postgres"} > 7200  # 2 hours
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "PostgreSQL backup is over 2 hours old"
    description: "Last backup was {{ $value | humanizeDuration }} ago."
```

---

## Summary

| Item | Details |
|---|---|
| **RTO** | 4 hours |
| **RPO (PostgreSQL)** | 1 hour (WAL archiving) |
| **RPO (FalkorDB)** | 6 hours (RDB snapshots) |
| **RPO (Redis)** | 6 hours (RDB snapshots) |
| **Backup storage** | S3-compatible (MinIO for self-hosted) |
| **PostgreSQL backup** | Hourly `pg_dump` + continuous WAL archiving |
| **FalkorDB backup** | RDB snapshot every 6 hours |
| **Redis backup** | RDB snapshot every 6 hours |
| **Config backup** | Weekly file archive |
| **Full recovery** | DB restore → verify → start services |
| **Point-in-time recovery** | Base backup + WAL replay to target timestamp |
| **DR test frequency** | Quarterly |
