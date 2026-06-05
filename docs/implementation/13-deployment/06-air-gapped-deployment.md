# Air-Gapped Deployment Guide

## Overview

MemGraph supports fully air-gapped (offline) deployment with no dependency on external SaaS endpoints. This guide covers prerequisites, image shipping, model management, and verification for environments without internet access.

---

## Prerequisites

Before transitioning to the air-gapped environment, prepare on an internet-connected machine:

### 1. Docker Registry Mirror

```bash
# On the air-gapped host, run a local Docker registry
docker run -d -p 5000:5000 --restart=always --name registry registry:2

# On the internet-connected build machine, tag and push images
docker tag memgraph-api:0.1.0 localhost:5000/memgraph-api:0.1.0
docker push localhost:5000/memgraph-api:0.1.0
```

Alternatively, export/import image tarballs:

```bash
# On build machine: save images
docker save memgraph-api:0.1.0 memgraph-worker:0.1.0 memgraph-mcp:0.1.0 \
  pgvector/pgvector:pg15 falkordb/falkordb:latest redis:7-alpine \
  grafana/alloy:latest node:20-alpine \
  -o memgraph-images.tar

# Transfer to air-gapped host
scp memgraph-images.tar user@air-gapped-host:/tmp/

# On air-gapped host: load images
docker load -i /tmp/memgraph-images.tar
```

### 2. PyPI Mirror

Set up a local PyPI mirror (e.g., `pypi-mirror` or `devpi`) and configure pip:

```bash
# On the build machine, download all dependencies
pip download -r requirements.txt -d ./pypi-packages/

# Transfer to air-gapped host and install from local
pip install --no-index --find-links=./pypi-packages/ -r requirements.txt
```

Or use a PyPI mirror server:

```bash
# pip config
pip config set global.index-url http://pypi-mirror:8080/simple/
pip config set global.trusted-host pypi-mirror
```

### 3. npm Mirror (for Dashboard)

```bash
# Set npm to use local mirror
npm config set registry http://npm-mirror:4873/
```

---

## Pre-Built Docker Images

### Image List

| Image | Source | Size (approx) |
|---|---|---|
| `memgraph-api:0.1.0` | Local build | ~500 MB |
| `memgraph-worker:0.1.0` | Local build | ~500 MB |
| `memgraph-mcp:0.1.0` | Local build | ~400 MB |
| `pgvector/pgvector:pg15` | Docker Hub | ~400 MB |
| `falkordb/falkordb:latest` | Docker Hub | ~200 MB |
| `redis:7-alpine` | Docker Hub | ~30 MB |
| `grafana/alloy:latest` | Docker Hub | ~200 MB |
| `node:20-alpine` | Docker Hub | ~130 MB |
| `bitnami/pgbouncer:latest` | Docker Hub | ~50 MB |
| `traefik:v3.1` | Docker Hub | ~100 MB |
| `bitnami/redis-sentinel:7.2` | Docker Hub | ~50 MB |
| `nats:latest` | Docker Hub | ~30 MB |
| **Total** | | **~2.6 GB** |

### Export/Import Script

```bash
#!/bin/bash
# save-images.sh — Run on internet-connected build machine

IMAGES=(
  "memgraph-api:0.1.0"
  "memgraph-worker:0.1.0"
  "memgraph-mcp:0.1.0"
  "pgvector/pgvector:pg15"
  "falkordb/falkordb:latest"
  "redis:7-alpine"
  "grafana/alloy:latest"
  "node:20-alpine"
  "bitnami/pgbouncer:latest"
  "traefik:v3.1"
  "bitnami/redis-sentinel:7.2"
)

echo "Saving ${#IMAGES[@]} images to memgraph-images.tar..."
docker save "${IMAGES[@]}" -o memgraph-images.tar
echo "Done. File size: $(du -h memgraph-images.tar | cut -f1)"
```

```bash
#!/bin/bash
# load-images.sh — Run on air-gapped host

echo "Loading images from memgraph-images.tar..."
docker load -i memgraph-images.tar
echo "Done."
```

---

## Ollama Models

### Pre-Download and Ship

Ollama models are large (4–40 GB each). Pre-download on the build machine and ship the model files.

```bash
# On build machine: pull models
ollama pull llama3.2
ollama pull nomic-embed-text
ollama pull llama3.2:3b  # Smaller, faster alternative

# Export model files
# Ollama stores models in ~/.ollama/models/
tar czf ollama-models.tar.gz -C ~/.ollama/models/ .

# Transfer (may require external drive for large models)
scp ollama-models.tar.gz user@air-gapped-host:/tmp/
```

### Model Sizes

| Model | Size | Purpose | RAM Required |
|---|---|---|---|
| `llama3.2:3b` | ~2 GB | Fast entity extraction | 4 GB |
| `llama3.2` | ~4 GB | General purpose | 8 GB |
| `llama3.1:8b` | ~8 GB | Higher quality extraction | 16 GB |
| `nomic-embed-text` | ~274 MB | Local embeddings | 1 GB |
| `llama3:70b` | ~40 GB | Best quality, production | 64 GB+ |
| `mistral` | ~4.2 GB | Alternative LLM | 8 GB |

**Recommendation for air-gapped**: `llama3.2:3b` (LLM) + `nomic-embed-text` (embeddings) = ~2.3 GB total.

### Import on Air-Gapped Host

```bash
# Extract model files to Ollama's data directory
tar xzf ollama-models.tar.gz -C /root/.ollama/models/

# Start Ollama and verify
ollama serve &
ollama list
# Expected: llama3.2, nomic-embed-text

# Test inference
ollama run llama3.2 "Extract entities from: John works at Acme Corp."
```

---

## Air-Gapped Docker Compose

### `infra/docker-compose.air-gapped.yml`

```yaml
version: "3.9"

x-logging: &default-logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"

services:
  api:
    image: memgraph-api:0.1.0
    ports:
      - "8000:8000"
    env_file:
      - .env.air-gapped
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

  worker:
    image: memgraph-worker:0.1.0
    env_file:
      - .env.air-gapped
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

  postgres:
    image: pgvector/pgvector:pg15
    environment:
      POSTGRES_USER: memgraph
      POSTGRES_PASSWORD: memgraph
      POSTGRES_DB: memgraph
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

  falkordb:
    image: falkordb/falkordb:latest
    volumes:
      - falkordb_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "-p", "6380", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - backend
    logging: *default-logging

  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - backend
    logging: *default-logging

  ollama:
    image: ollama/ollama:latest
    ports:
      - "11434:11434"
    volumes:
      - ollama_data:/root/.ollama
    healthcheck:
      test: ["CMD", "ollama", "list"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    networks:
      - backend
    logging: *default-logging

  pgbouncer:
    image: bitnami/pgbouncer:latest
    environment:
      - PGBOUNCER_DATABASE=memgraph
      - PGBOUNCER_HOST=postgres
      - PGBOUNCER_PORT=5432
      - PGBOUNCER_USERNAME=memgraph
      - PGBOUNCER_PASSWORD=memgraph
      - PGBOUNCER_POOL_MODE=transaction
      - PGBOUNCER_DEFAULT_POOL_SIZE=25
    depends_on:
      postgres:
        condition: service_healthy
    ports:
      - "6432:6432"
    networks:
      - backend
    logging: *default-logging

  dashboard:
    image: memgraph-dashboard:0.1.0
    ports:
      - "3000:3000"
    environment:
      - NEXT_PUBLIC_API_URL=http://localhost:8000
    depends_on:
      api:
        condition: service_healthy
    networks:
      - backend
    logging: *default-logging

  alloy:
    image: grafana/alloy:latest
    command:
      - run
      - /etc/alloy/config.alloy
      - --storage.path=/var/lib/alloy/data
      - --server.http.listen-addr=0.0.0.0:12345
    volumes:
      - ./infra/alloy/config.air-gapped.alloy:/etc/alloy/config.alloy
      - /var/lib/alloy/data:/var/lib/alloy/data
    networks:
      - backend
    logging: *default-logging

volumes:
  pgdata:
  falkordb_data:
  redis_data:
  ollama_data:

networks:
  backend:
    driver: bridge
```

### `.env.air-gapped`

```bash
# MemGraph Air-Gapped Environment
ENVIRONMENT=production
LOG_LEVEL=INFO

DATABASE_URL=postgresql+asyncpg://memgraph:memgraph@pgbouncer:6432/memgraph
DATABASE_POOL_SIZE=5

REDIS_URL=redis://redis:6379/0
FALKORDB_URL=redis://falkordb:6380

GRAPH_BACKEND=falkordb
LLM_BACKEND=ollama
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=llama3.2

EMBEDDING_BACKEND=ollama
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIM=768

SECRET_KEY=air-gapped-production-secret-key

CONTEXT_CACHE_TTL=30
CONTEXT_MAX_DEPTH=2
```

### Alloy Config (No External Endpoints)

```river
// config.air-gapped.alloy — No external connections
// All observability data is stored locally.

prometheus.scrape "memgraph_api" {
  targets    = [{"__address__" = "api:8000"}]
  metrics_path = "/metrics"
  scrape_interval = "15s"
  forward_to = [prometheus.remote_write.blackhole.receiver]
}

prometheus.scrape "memgraph_worker" {
  targets    = [{"__address__" = "worker:9101"}]
  scrape_interval = "15s"
  forward_to = [prometheus.remote_write.blackhole.receiver]
}

// Blackhole — metrics are captured but not forwarded (no Mimir)
prometheus.remote_write "blackhole" {
  endpoint {
    url = "http://blackhole:9095/api/v1/push"
  }
}
```

---

## Verification Steps

### 1. Startup Verification

```bash
# Start all services
docker compose -f infra/docker-compose.air-gapped.yml up -d

# Wait for all health checks to pass
docker compose -f infra/docker-compose.air-gapped.yml ps

# Expected state:
#   api        healthy
#   worker     healthy
#   postgres   healthy
#   falkordb   healthy
#   redis      healthy
#   ollama     healthy
#   pgbouncer  healthy
#   dashboard  healthy
#   alloy      healthy
```

### 2. Health Check

```bash
# API health
curl http://localhost:8000/health

# API readiness (checks all backends)
curl http://localhost:8000/ready

# Expected response:
# {"status": "ok", "checks": {"postgres": "ok", "redis": "ok", "falkordb": "ok"}}
```

### 3. Test with Ollama LLM

```bash
# Run the first migration
docker compose exec api alembic upgrade head

# Create an organization
curl -X POST http://localhost:8000/v1/admin/organizations \
  -H "Content-Type: application/json" \
  -d '{"name": "Air-Gapped Test"}'

# Get an API key (parse from response)
# Note: admin endpoints may need different auth
```

### 4. Test Memory Ingestion

```bash
# Create a user
curl -X POST http://localhost:8000/v1/users \
  -H "Content-Type: application/json" \
  -d '{"user_id": "test_user"}'

# Ingest memory
curl -X POST http://localhost:8000/v1/users/test_user/memory \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "Hi, I am building an AI agent for customer support."},
      {"role": "assistant", "content": "Great! What kind of support do you need?"}
    ]
  }'

# Wait for worker to process (check worker logs)
docker compose logs worker | tail -20
# Expected: entity extraction, embedding completed

# Retrieve context
curl -X GET "http://localhost:8000/v1/users/test_user/context?query=AI%20support"
# Expected: assembled context block about customer support AI agent
```

### 5. Test All Services

```bash
# Dashboard
curl http://localhost:3000
# Expected: dashboard HTML

# Graph query
curl -X GET "http://localhost:8000/v1/users/test_user/graph/nodes"
# Expected: list of extracted entity nodes

# Search
curl -X GET "http://localhost:8000/v1/users/test_user/search?query=support"
# Expected: ranked results
```

---

## Limitations

| Capability | Status in Air-Gapped | Workaround |
|---|---|---|
| LLM quality | Lower | Use larger local model (`llama3.1:8b`+) or quantised GPTQ models |
| Embedding quality | Lower | `nomic-embed-text` is good but not OpenAI quality |
| Community summarisation | Available | Quality depends on local LLM |
| Entity extraction | Available | Works well with `llama3.2:3b` for English, weaker for other languages |
| Dialect classification | Available | Lower accuracy without OpenAI |
| Cross-encoder re-ranker | Available with local models | Use `BAAI/bge-reranker-v2-m3` (requires ~2 GB) |
| LLM quality guarantees | No guarantee | Cannot use OpenAI — local LLM output is non-deterministic |
| External observability | Limited | Metrics stored locally, no remote Tempo/Loki/Mimir |
| Dashboard usage analytics | Limited | Cannot pull from hosted Grafana |

### Quality Comparison (OpenAI vs Ollama)

| Task | OpenAI (gpt-4o-mini) | Ollama (llama3.2:3b) | Ollama (llama3.1:8b) |
|---|---|---|---|
| Entity extraction F1 | ~0.92 | ~0.78 | ~0.85 |
| Fact extraction accuracy | ~0.90 | ~0.72 | ~0.82 |
| Classification accuracy | ~0.95 | ~0.80 | ~0.88 |
| Community summarisation | Excellent | Adequate | Good |
| Latency per call | ~1s | ~5s (CPU) / ~1s (GPU) | ~15s (CPU) / ~2s (GPU) |

### GPU Acceleration (Optional)

For better local LLM performance, add GPU support:

```yaml
# In docker-compose.air-gapped.yml — ollama service
  ollama:
    image: ollama/ollama:latest
    ports:
      - "11434:11434"
    volumes:
      - ollama_data:/root/.ollama
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

Requires `nvidia-container-toolkit` installed on the host.
