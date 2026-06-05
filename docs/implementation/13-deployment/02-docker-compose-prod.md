# Production Docker Compose Guide

## Overview

The production Docker Compose environment extends the development setup with high-availability, security, and performance features: PgBouncer connection pooling, Redis Sentinel for HA, Traefik reverse proxy with TLS, and Grafana Alloy for observability.

---

## File Location

`infra/docker-compose.prod.yml`

---

## Additional Production Services

| Service | Purpose |
|---|---|
| `pgbouncer` | PostgreSQL connection pooling (transaction mode) |
| `redis-sentinel` | Redis HA with sentinel + 2 replicas |
| `traefik` | Reverse proxy, TLS termination, Let's Encrypt |
| `alloy` | Grafana Alloy (metrics/logs/traces collection) |

---

## Full Compose File

```yaml
version: "3.9"

x-logging: &default-logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"
    tag: "{{.Name}}"

x-resources: &default-resources
  reservations:
    cpus: "0.5"
    memory: "256M"
  limits:
    cpus: "2"
    memory: "1G"

secrets:
  db_password:
    file: ./secrets/db_password.txt
  jwt_secret:
    file: ./secrets/jwt_secret.txt
  openai_api_key:
    file: ./secrets/openai_api_key.txt
  alloy_api_key:
    file: ./secrets/alloy_api_key.txt

services:
  # ─── Traefik Reverse Proxy ────────────────────────────────────
  traefik:
    image: traefik:v3.1
    command:
      - "--providers.docker=true"
      - "--providers.docker.exposedbydefault=false"
      - "--entrypoints.websecure.address=:443"
      - "--entrypoints.web.address=:80"
      - "--entrypoints.web.http.redirections.entrypoint.to=websecure"
      - "--entrypoints.web.http.redirections.entrypoint.scheme=https"
      - "--certificatesresolvers.letsencrypt.acme.tlschallenge=true"
      - "--certificatesresolvers.letsencrypt.acme.email=admin@example.com"
      - "--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json"
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - "/var/run/docker.sock:/var/run/docker.sock:ro"
      - traefik_letsencrypt:/letsencrypt
    networks:
      - frontend
    logging: *default-logging
    restart: always

  # ─── API Gateway ──────────────────────────────────────────────
  api:
    build:
      context: .
      dockerfile: services/api/Dockerfile
    deploy:
      replicas: 2
    env_file:
      - .env
    environment:
      - DATABASE_URL=postgresql+asyncpg://memgraph:${DB_PASSWORD}@pgbouncer:6432/memgraph
      - REDIS_URL=redis-sentinel://redis-sentinel:26379/mymaster/0
      - FALKORDB_URL=redis://falkordb:6380
      - OTEL_EXPORTER_OTLP_ENDPOINT=http://alloy:4317
      - SECRET_KEY_FILE=/run/secrets/jwt_secret
      - OPENAI_API_KEY_FILE=/run/secrets/openai_api_key
      - LOG_LEVEL=INFO
      - ENVIRONMENT=production
      - DATABASE_POOL_SIZE=5  # Smaller pool — goes through PgBouncer
    secrets:
      - jwt_secret
      - openai_api_key
    depends_on:
      pgbouncer:
        condition: service_healthy
      redis-sentinel:
        condition: service_healthy
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.api.rule=Host(`api.memgraph.example.com`)"
      - "traefik.http.routers.api.entrypoints=websecure"
      - "traefik.http.routers.api.tls.certresolver=letsencrypt"
      - "traefik.http.services.api.loadbalancer.server.port=8000"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 30s
    networks:
      - backend
    logging: *default-logging
    restart: always

  # ─── ARQ Worker ───────────────────────────────────────────────
  worker:
    build:
      context: .
      dockerfile: services/worker/Dockerfile
    deploy:
      replicas: 2
    env_file:
      - .env
    environment:
      - DATABASE_URL=postgresql+asyncpg://memgraph:${DB_PASSWORD}@pgbouncer:6432/memgraph
      - REDIS_URL=redis-sentinel://redis-sentinel:26379/mymaster/0
      - FALKORDB_URL=redis://falkordb:6380
      - OTEL_EXPORTER_OTLP_ENDPOINT=http://alloy:4317
      - OPENAI_API_KEY_FILE=/run/secrets/openai_api_key
      - LOG_LEVEL=INFO
      - ENVIRONMENT=production
      - DATABASE_POOL_SIZE=5
    secrets:
      - openai_api_key
    depends_on:
      pgbouncer:
        condition: service_healthy
      redis-sentinel:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "python", "-c", "import redis; r=redis.Redis.from_url('redis://redis-sentinel:26379'); r.ping()"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    networks:
      - backend
    logging: *default-logging
    restart: always

  # ─── MCP Server ───────────────────────────────────────────────
  mcp:
    build:
      context: .
      dockerfile: services/mcp/Dockerfile
    env_file:
      - .env
    environment:
      - DATABASE_URL=postgresql+asyncpg://memgraph:${DB_PASSWORD}@pgbouncer:6432/memgraph
      - REDIS_URL=redis-sentinel://redis-sentinel:26379/mymaster/0
      - FALKORDB_URL=redis://falkordb:6380
      - LOG_LEVEL=INFO
      - ENVIRONMENT=production
    depends_on:
      pgbouncer:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8001/health"]
      interval: 30s
      timeout: 5s
      retries: 3
    networks:
      - backend
    logging: *default-logging
    restart: always

  # ─── PgBouncer (Connection Pooling) ───────────────────────────
  pgbouncer:
    image: bitnami/pgbouncer:latest
    environment:
      - PGBOUNCER_DATABASE=memgraph
      - PGBOUNCER_HOST=postgres
      - PGBOUNCER_PORT=5432
      - PGBOUNCER_USERNAME=memgraph
      - PGBOUNCER_PASSWORD_FILE=/run/secrets/db_password
      - PGBOUNCER_POOL_MODE=transaction
      - PGBOUNCER_DEFAULT_POOL_SIZE=25
      - PGBOUNCER_MAX_CLIENT_CONN=100
      - PGBOUNCER_SERVER_IDLE_TIMEOUT=300
    secrets:
      - db_password
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "pgbouncer", "--version"]
      interval: 15s
      timeout: 5s
      retries: 5
    ports:
      - "6432:6432"  # PgBouncer port (not 5432 — distinguishes from direct access)
    networks:
      - backend
    logging: *default-logging
    restart: unless-stopped

  # ─── PostgreSQL (Primary) ─────────────────────────────────────
  postgres:
    image: pgvector/pgvector:pg15
    environment:
      POSTGRES_USER: memgraph
      POSTGRES_PASSWORD_FILE: /run/secrets/db_password
      POSTGRES_DB: memgraph
    secrets:
      - db_password
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./infra/postgres/postgresql.conf:/etc/postgresql/postgresql.conf
      - ./infra/postgres/init.sql:/docker-entrypoint-initdb.d/init.sql
    command:
      - "-c"
      - "config_file=/etc/postgresql/postgresql.conf"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U memgraph"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s
    networks:
      - backend
    logging: *default-logging
    restart: unless-stopped
    deploy:
      resources:
        reservations:
          cpus: "1.0"
          memory: "1G"
        limits:
          cpus: "4"
          memory: "4G"

  # ─── PostgreSQL Replica (optional, uncomment for HA) ──────────
  # postgres-replica:
  #   image: pgvector/pgvector:pg15
  #   environment:
  #     POSTGRES_USER: memgraph
  #     POSTGRES_PASSWORD_FILE: /run/secrets/db_password
  #     POSTGRES_DB: memgraph
  #   secrets:
  #     - db_password
  #   volumes:
  #     - pgdata_replica:/var/lib/postgresql/data
  #   depends_on:
  #     postgres:
  #       condition: service_healthy
  #   networks:
  #     - backend
  #   logging: *default-logging
  #   restart: unless-stopped

  # ─── FalkorDB ─────────────────────────────────────────────────
  falkordb:
    image: falkordb/falkordb:latest
    volumes:
      - falkordb_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "-p", "6380", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
    networks:
      - backend
    logging: *default-logging
    restart: unless-stopped
    deploy:
      resources:
        reservations:
          cpus: "0.5"
          memory: "512M"
        limits:
          cpus: "2"
          memory: "2G"

  # ─── Redis Sentinel ───────────────────────────────────────────
  redis-sentinel:
    image: bitnami/redis-sentinel:7.2
    environment:
      - REDIS_SENTINEL_QUORUM=2
      - REDIS_SENTINEL_DOWN_AFTER_MILLISECONDS=5000
      - REDIS_SENTINEL_FAILOVER_TIMEOUT=10000
      - REDIS_SENTINEL_MASTER_NAME=mymaster
    depends_on:
      redis-master:
        condition: service_healthy
      redis-replica-1:
        condition: service_healthy
      redis-replica-2:
        condition: service_healthy
    ports:
      - "26379:26379"
    networks:
      - backend
    logging: *default-logging
    restart: always

  redis-master:
    image: redis:7-alpine
    command: redis-server --appendonly yes
    volumes:
      - redis_master_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - backend
    logging: *default-logging
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: "1G"

  redis-replica-1:
    image: redis:7-alpine
    command: redis-server --appendonly yes --replicaof redis-master 6379
    volumes:
      - redis_replica_1_data:/data
    depends_on:
      redis-master:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - backend
    logging: *default-logging
    restart: unless-stopped

  redis-replica-2:
    image: redis:7-alpine
    command: redis-server --appendonly yes --replicaof redis-master 6379
    volumes:
      - redis_replica_2_data:/data
    depends_on:
      redis-master:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - backend
    logging: *default-logging
    restart: unless-stopped

  # ─── Admin Dashboard ──────────────────────────────────────────
  dashboard:
    build:
      context: .
      dockerfile: apps/dashboard/Dockerfile
    env_file:
      - .env
    environment:
      - NEXT_PUBLIC_API_URL=https://api.memgraph.example.com
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.dashboard.rule=Host(`dashboard.memgraph.example.com`)"
      - "traefik.http.routers.dashboard.entrypoints=websecure"
      - "traefik.http.routers.dashboard.tls.certresolver=letsencrypt"
      - "traefik.http.services.dashboard.loadbalancer.server.port=3000"
    depends_on:
      api:
        condition: service_healthy
    networks:
      - frontend
      - backend
    logging: *default-logging
    restart: always

  # ─── Grafana Alloy ────────────────────────────────────────────
  alloy:
    image: grafana/alloy:latest
    command:
      - run
      - /etc/alloy/config.alloy
      - --storage.path=/var/lib/alloy/data
      - --server.http.listen-addr=0.0.0.0:12345
    ports:
      - "12345:12345"  # Admin interface (internal only)
    volumes:
      - ./infra/alloy/config.alloy:/etc/alloy/config.alloy
      - /var/lib/docker/containers:/var/lib/docker/containers:ro
      - /var/lib/alloy:/var/lib/alloy
    environment:
      - LOKI_ENDPOINT=http://loki:3100/loki/api/v1/push
      - MIMIR_ENDPOINT=http://mimir:9009/api/v1/push
      - TEMPO_ENDPOINT=http://tempo:4317
    networks:
      - backend
    logging: *default-logging
    restart: always
    deploy:
      resources:
        limits:
          memory: "512M"

volumes:
  pgdata:
  falkordb_data:
  redis_master_data:
  redis_replica_1_data:
  redis_replica_2_data:
  traefik_letsencrypt:

networks:
  backend:
    driver: bridge
    internal: true  # No external access to backend
  frontend:
    driver: bridge
```

---

## Key Production Differences

### Connection Pooling (PgBouncer)

| Setting | Value | Rationale |
|---|---|---|
| Pool mode | `transaction` | Releases connection after each transaction — good for async apps |
| `default_pool_size` | `25` | Limits concurrent DB connections |
| `max_client_conn` | `100` | Max clients waiting for a pool slot |

**Application code change**: API and worker connect to `pgbouncer:6432` (port 6432), not `postgres:5432`. The `DATABASE_POOL_SIZE` in the application is reduced to 5 since PgBouncer manages the actual connection count.

### Redis HA (Sentinel)

```
redis-master  →  primary (read/write)
redis-replica-1  →  replica (read-only, failover candidate)
redis-replica-2  →  replica (read-only, failover candidate)
redis-sentinel   →  monitors cluster, performs failover
```

**Connection URL format**:
```
redis-sentinel://redis-sentinel:26379/mymaster/0
```

The application uses `redis-py` with Sentinel support:

```python
from redis.sentinel import Sentinel

sentinel = Sentinel([("redis-sentinel", 26379)])
master = sentinel.master_for("mymaster")
slave = sentinel.slave_for("mymaster")
```

### Secrets Management

Secrets are stored in files under `./secrets/` and mounted into containers via Docker secrets:

```
secrets/
├── db_password.txt
├── jwt_secret.txt
├── openai_api_key.txt
└── alloy_api_key.txt
```

**Security requirements**:
- `secrets/` is in `.gitignore` — never commit secrets.
- File permissions: `chmod 600 secrets/*`.
- Use a secrets manager (Vault, 1Password) in production — files here are for demo/staging only.
- Applications read secrets from `/run/secrets/{name}`.

### Resource Limits

| Service | CPU Reservation | CPU Limit | Memory Reservation | Memory Limit |
|---|---|---|---|---|
| api | 0.5 | 2 | 256M | 1G |
| worker | 0.5 | 2 | 256M | 1G |
| postgres | 1.0 | 4 | 1G | 4G |
| falkordb | 0.5 | 2 | 512M | 2G |
| redis-master | — | — | — | 1G |
| alloy | — | — | — | 512M |

### Logging

```yaml
x-logging: &default-logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"
    tag: "{{.Name}}"
```

- `json-file` driver — readable by Alloy's log collector.
- `max-size=10m` — rotate logs every 10MB.
- `max-file=3` — keep 3 rotated files (30MB max per service).
- `tag={{.Name}}` — adds service name as Docker tag for log filtering.

### Restart Policies

| Service Type | Policy | Reason |
|---|---|---|
| Stateful (postgres, falkordb, redis) | `unless-stopped` | Don't restart on manual stop; restart on crash |
| Stateless (api, worker, traefik) | `always` | Always restart on crash or reboot |

---

## Startup Order

1. **postgres** — must be healthy before PgBouncer starts.
2. **pgbouncer** — must be healthy before API/Worker starts.
3. **redis-master** → **redis-replica-1,2** → **redis-sentinel**.
4. **falkordb** — independent of other services.
5. **api**, **worker**, **mcp** — depend on PgBouncer and Redis Sentinel.
6. **alloy** — independent, started early for log collection.
7. **traefik** — started early for health check routing.
8. **dashboard** — depends on API.

---

## Deploying

```bash
# 1. Create secrets
mkdir -p secrets
echo -n "your-db-password" > secrets/db_password.txt
echo -n "your-jwt-secret" > secrets/jwt_secret.txt
echo -n "sk-..." > secrets/openai_api_key.txt
echo -n "your-alloy-key" > secrets/alloy_api_key.txt
chmod 600 secrets/*

# 2. Deploy
docker compose -f infra/docker-compose.prod.yml up -d

# 3. Run migrations
docker compose -f infra/docker-compose.prod.yml exec api alembic upgrade head

# 4. Verify
curl -f https://api.memgraph.example.com/health
```

### Scaling

```bash
# Scale API to 4 instances
docker compose -f infra/docker-compose.prod.yml up -d --scale api=4

# Scale workers to 6 instances
docker compose -f infra/docker-compose.prod.yml up -d --scale worker=6
```

---

## Monitoring Production

| Tool | Endpoint | Purpose |
|---|---|---|
| Alloy | `http://alloy:12345` | Alloy admin UI |
| Traefik | Port 80/443 | Access logs, service routing |
| Health | `https://api.memgraph.example.com/health` | API liveness |
| Ready | `https://api.memgraph.example.com/ready` | API readiness (checks DB, Redis) |

### Production Health Endpoint

```python
# FastAPI — /ready
@app.get("/ready")
async def readiness():
    """Check that all dependencies are reachable."""
    status = {"status": "ok", "checks": {}}

    # PostgreSQL
    try:
        await db.execute("SELECT 1")
        status["checks"]["postgres"] = "ok"
    except Exception:
        status["checks"]["postgres"] = "fail"
        status["status"] = "degraded"

    # Redis
    try:
        await redis.ping()
        status["checks"]["redis"] = "ok"
    except Exception:
        status["checks"]["redis"] = "fail"
        status["status"] = "degraded"

    # FalkorDB
    try:
        await falkordb.ping()
        status["checks"]["falkordb"] = "ok"
    except Exception:
        status["checks"]["falkordb"] = "fail"
        status["status"] = "degraded"

    return status
```
