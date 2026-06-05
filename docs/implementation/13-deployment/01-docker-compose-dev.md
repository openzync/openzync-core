# Development Docker Compose Guide

## Overview

The development Docker Compose environment runs all MemGraph services locally for development and testing. It includes local defaults for all services, persistent volumes, and an optional Ollama service for local LLM testing.

---

## File Location

`infra/docker-compose.yml`

---

## Services

| Service | Image | Purpose | Port |
|---|---|---|---|
| `api` | `memgraph-api` (local build) | FastAPI gateway | `8000` |
| `worker` | `memgraph-worker` (local build) | ARQ background workers | — |
| `postgres` | `pgvector/pgvector:pg15` | PostgreSQL + pgvector | `5432` |
| `falkordb` | `falkordb/falkordb:latest` | Graph database | `6380` |
| `redis` | `redis:7-alpine` | Job queue + cache | `6379` |
| `dashboard` | Local build | Next.js admin dashboard | `3000` |
| `alloy` | `grafana/alloy:latest` | Metrics + logs + traces collector | — |

---

## Full Compose File

```yaml
version: "3.9"

x-logging: &default-logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"

services:
  # ─── API Gateway ──────────────────────────────────────────────
  api:
    build:
      context: .
      dockerfile: services/api/Dockerfile
    ports:
      - "8000:8000"
    env_file:
      - .env
    environment:
      - DATABASE_URL=postgresql+asyncpg://memgraph:memgraph@postgres:5432/memgraph
      - REDIS_URL=redis://redis:6379/0
      - FALKORDB_URL=redis://falkordb:6380
      - OTEL_EXPORTER_OTLP_ENDPOINT=http://alloy:4317
      - LOG_LEVEL=DEBUG
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      falkordb:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 30s
    networks:
      - backend
    logging: *default-logging

  # ─── ARQ Worker ───────────────────────────────────────────────
  worker:
    build:
      context: .
      dockerfile: services/worker/Dockerfile
    env_file:
      - .env
    environment:
      - DATABASE_URL=postgresql+asyncpg://memgraph:memgraph@postgres:5432/memgraph
      - REDIS_URL=redis://redis:6379/0
      - FALKORDB_URL=redis://falkordb:6380
      - OTEL_EXPORTER_OTLP_ENDPOINT=http://alloy:4317
      - LOG_LEVEL=DEBUG
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      falkordb:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "python", "-c", "import redis; r=redis.Redis.from_url('redis://redis:6379/0'); r.ping()"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    networks:
      - backend
    logging: *default-logging

  # ─── PostgreSQL + pgvector ────────────────────────────────────
  postgres:
    image: pgvector/pgvector:pg15
    environment:
      POSTGRES_USER: memgraph
      POSTGRES_PASSWORD: memgraph
      POSTGRES_DB: memgraph
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./infra/postgres/init.sql:/docker-entrypoint-initdb.d/init.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U memgraph"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s
    networks:
      - backend
    logging: *default-logging

  # ─── FalkorDB ─────────────────────────────────────────────────
  falkordb:
    image: falkordb/falkordb:latest
    ports:
      - "6380:6380"
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

  # ─── Redis ────────────────────────────────────────────────────
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
    networks:
      - backend
    logging: *default-logging

  # ─── Admin Dashboard ──────────────────────────────────────────
  dashboard:
    build:
      context: .
      dockerfile: apps/dashboard/Dockerfile
    ports:
      - "3000:3000"
    environment:
      - NEXT_PUBLIC_API_URL=http://localhost:8000
      - DATABASE_URL=postgresql+asyncpg://memgraph:memgraph@postgres:5432/memgraph
    depends_on:
      postgres:
        condition: service_healthy
    networks:
      - frontend
      - backend
    logging: *default-logging

  # ─── Grafana Alloy ────────────────────────────────────────────
  alloy:
    image: grafana/alloy:latest
    command:
      - run
      - /etc/alloy/config.alloy
      - --storage.path=/var/lib/alloy/data
      - --server.http.listen-addr=0.0.0.0:12345
    ports:
      - "12345:12345"  # Alloy UI (admin interface)
    volumes:
      - ./infra/alloy/config.alloy:/etc/alloy/config.alloy
      - /var/lib/alloy/data:/var/lib/alloy/data
    networks:
      - backend
    logging: *default-logging

  # ─── Ollama (optional — for local LLM testing) ────────────────
  # Uncomment to enable local LLM inference with Ollama.
  # Download models: docker compose exec ollama ollama pull llama3.2
  # ollama:
  #   image: ollama/ollama:latest
  #   ports:
  #     - "11434:11434"
  #   volumes:
  #     - ollama_data:/root/.ollama
  #   healthcheck:
  #     test: ["CMD", "ollama", "list"]
  #     interval: 30s
  #     timeout: 10s
  #     retries: 3
  #     start_period: 120s
  #   networks:
  #     - backend
  #   logging: *default-logging

volumes:
  pgdata:
  falkordb_data:
  redis_data:
  # ollama_data:  # Uncomment when Ollama is enabled

networks:
  backend:
    driver: bridge
    internal: false  # Allow API access from host
  frontend:
    driver: bridge
```

---

## Environment File (`.env`)

```bash
# MemGraph Development Environment
# Copy to .env and adjust as needed

# Application
ENVIRONMENT=development
LOG_LEVEL=DEBUG
VERSION=0.1.0-dev
SECRET_KEY=dev-secret-key-do-not-use-in-production
JWT_SECRET=dev-jwt-secret-do-not-use-in-production

# PostgreSQL (defaults match docker-compose.yml)
DATABASE_URL=postgresql+asyncpg://memgraph:memgraph@localhost:5432/memgraph
DATABASE_POOL_SIZE=10
DATABASE_MAX_OVERFLOW=5

# Redis
REDIS_URL=redis://localhost:6379/0

# FalkorDB (graph backend)
GRAPH_BACKEND=falkordb
FALKORDB_URL=redis://localhost:6380

# LLM Backend
LLM_BACKEND=openai
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=https://api.openai.com/v1

# Embeddings
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=1536

# Ollama (for local LLM testing)
# LLM_BACKEND=ollama
# OLLAMA_BASE_URL=http://localhost:11434
# OLLAMA_MODEL=llama3.2
# EMBEDDING_MODEL=nomic-embed-text
# EMBEDDING_DIM=768

# Context Assembly
CONTEXT_CACHE_TTL=30
CONTEXT_MAX_DEPTH=2
CONTEXT_MAX_TOKENS=4096

# Worker
MAX_WORKERS=2
WORKER_QUEUE_HIGH_CONCURRENCY=4
WORKER_QUEUE_LOW_CONCURRENCY=2

# Observability
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
OTEL_SAMPLER=parentbased_always_on
```

---

## Health Checks

Every service has a `HEALTHCHECK`:

| Service | Command | Interval | Timeout | Retries | Start Period |
|---|---|---|---|---|---|
| `api` | `curl -f http://localhost:8000/health` | 15s | 5s | 5 | 30s |
| `worker` | Redis ping via Python | 30s | 10s | 3 | 60s |
| `postgres` | `pg_isready` | 10s | 5s | 5 | 30s |
| `falkordb` | `redis-cli ping` | 10s | 5s | 5 | 10s |
| `redis` | `redis-cli ping` | 10s | 5s | 5 | 10s |
| `dashboard` | Default health check | — | — | — | — |

---

## Volume Mounts

| Volume | Mount Point | Service | Persistence |
|---|---|---|---|
| `pgdata` | `/var/lib/postgresql/data` | postgres | Database files |
| `falkordb_data` | `/data` | falkordb | Graph database files |
| `redis_data` | `/data` | redis | Cache + queue data |
| `ollama_data` | `/root/.ollama` | ollama (optional) | LLM models |

---

## Networks

| Network | Driver | Services | Purpose |
|---|---|---|---|
| `backend` | bridge | api, worker, postgres, falkordb, redis, alloy | Inter-service communication |
| `frontend` | bridge | dashboard | Dashboard access to API |

- `backend` is NOT marked as `internal` — allows API access from the host for SDK testing.
- `frontend` isolates the dashboard from backend internals.

---

## Getting Started

### First Run

```bash
# 1. Clone the repository
git clone https://github.com/thelinkai/memgraph.git
cd memgraph

# 2. Copy environment file
cp .env.example .env
# Edit .env to add your OPENAI_API_KEY

# 3. Start all services
docker compose -f infra/docker-compose.yml up -d

# 4. Run database migrations
docker compose -f infra/docker-compose.yml exec api alembic upgrade head

# 5. Verify health
curl http://localhost:8000/health

# 6. Create an organization and API key
curl -X POST http://localhost:8000/v1/admin/organizations \
  -H "Content-Type: application/json" \
  -d '{"name": "My Org"}'

# 7. Test a memory ingestion
curl -X POST http://localhost:8000/v1/users/test_user/memory \
  -H "Authorization: Bearer mg_test_<key>" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello, I am building an AI agent."}]}'
```

### Useful Commands

```bash
# View logs for a specific service
docker compose -f infra/docker-compose.yml logs -f api

# Restart a single service
docker compose -f infra/docker-compose.yml restart worker

# Scale workers (run 4 worker instances)
docker compose -f infra/docker-compose.yml up -d --scale worker=4

# Run a migration
docker compose -f infra/docker-compose.yml exec api alembic revision --autogenerate -m "description"

# Access PostgreSQL CLI
docker compose -f infra/docker-compose.yml exec postgres psql -U memgraph

# Enable Ollama (then pull a model)
docker compose -f infra/docker-compose.yml up -d ollama
docker compose exec ollama ollama pull llama3.2

# Reset everything (destroys volumes)
docker compose -f infra/docker-compose.yml down -v
```

---

## Using Ollama (Local LLM)

### Enable Ollama

1. Uncomment the `ollama` service in `docker-compose.yml`.
2. Uncomment `ollama_data` volume.
3. Start: `docker compose up -d ollama`
4. Pull a model:

```bash
docker compose exec ollama ollama pull llama3.2
# Or for embeddings:
docker compose exec ollama ollama pull nomic-embed-text
```

5. Set environment variables in `.env`:

```bash
LLM_BACKEND=ollama
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=llama3.2
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIM=768
```

### Limitations

- Local LLMs produce lower-quality entity extraction than `gpt-4o-mini`.
- Local embedding models have lower recall in hybrid search.
- Context assembly quality depends on the quality of extracted entities and embeddings.

---

## Port Mapping

| Port | Service | Notes |
|---|---|---|
| `8000` | API | Main REST API |
| `3000` | Dashboard | Next.js admin UI |
| `5432` | PostgreSQL | Direct DB access for debugging |
| `6380` | FalkorDB | Redis-protocol graph DB |
| `6379` | Redis | Job queue + cache |
| `11434` | Ollama | Local LLM (optional) |
| `12345` | Alloy | Alloy admin UI |
