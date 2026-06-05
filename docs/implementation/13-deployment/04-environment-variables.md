# Environment Variables Reference

## Overview

All MemGraph configuration is done via environment variables. Every service reads from environment variables and has sensible defaults for local development.

---

## Database

| Variable | Default | Required | Service | Description |
|---|---|---|---|---|
| `DATABASE_URL` | — | **Yes** | api, worker, mcp | PostgreSQL DSN with async driver. Format: `postgresql+asyncpg://user:password@host:port/dbname` |
| `DATABASE_POOL_SIZE` | `10` | No | api, worker, mcp | SQLAlchemy connection pool size. Set to `5` when using PgBouncer. |
| `DATABASE_MAX_OVERFLOW` | `5` | No | api, worker, mcp | Max overflow connections above pool size. |
| `DATABASE_POOL_TIMEOUT` | `30` | No | api, worker, mcp | Seconds to wait for a connection from the pool. |
| `DB_PASSWORD` | — | Conditional | infra | PostgreSQL password for production deployments. Used in docker-compose.prod.yml. |

## Graph Database

| Variable | Default | Required | Service | Description |
|---|---|---|---|---|
| `GRAPH_BACKEND` | `falkordb` | No | api, worker | Graph backend: `falkordb` or `neo4j`. |
| `FALKORDB_URL` | `redis://localhost:6380` | No | api, worker | FalkorDB connection URL (Redis wire protocol). |
| `NEO4J_URI` | — | Conditional | api, worker | Neo4j connection URI. Required if `GRAPH_BACKEND=neo4j`. Format: `bolt://user:password@host:7687`. |
| `FALKORDB_PASSWORD` | — | No | api, worker | FalkorDB password (if password-protected). |

## Redis

| Variable | Default | Required | Service | Description |
|---|---|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | No | api, worker, mcp | Redis connection URL. For Sentinel HA: `redis-sentinel://host:26379/mymaster/0`. |
| `REDIS_SENTINEL_MASTER` | `mymaster` | No | api, worker | Redis Sentinel master name. |
| `REDIS_SENTINEL_PASSWORD` | — | No | api, worker | Redis Sentinel password (if configured). |
| `REDIS_PASSWORD` | — | No | api, worker | Redis password (if configured). |

## LLM Backend

| Variable | Default | Required | Service | Description |
|---|---|---|---|---|
| `LLM_BACKEND` | `openai` | No | api, worker, mcp | LLM provider: `openai`, `azure`, `ollama`. |
| `OPENAI_API_KEY` | — | Conditional | api, worker | OpenAI API key. Required if `LLM_BACKEND=openai` or `LLM_BACKEND=azure`. |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | No | api, worker | OpenAI API base URL. Change for proxies or Azure. |
| `OPENAI_MODEL` | `gpt-4o-mini` | No | api, worker | Default LLM model for enrichment and classification tasks. |
| `OPENAI_ORG_ID` | — | No | api, worker | OpenAI organization ID (for org-level billing). |
| `AZURE_API_KEY` | — | Conditional | api, worker | Azure OpenAI API key. Required if `LLM_BACKEND=azure`. |
| `AZURE_API_BASE` | — | Conditional | api, worker | Azure OpenAI endpoint. Required if `LLM_BACKEND=azure`. |
| `AZURE_API_VERSION` | `2024-08-01-preview` | No | api, worker | Azure OpenAI API version. |
| `AZURE_DEPLOYMENT_NAME` | — | Conditional | api, worker | Azure deployment name. Required if `LLM_BACKEND=azure`. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Conditional | api, worker | Ollama server URL. Required if `LLM_BACKEND=ollama`. |
| `OLLAMA_MODEL` | `llama3.2` | No | api, worker | Default Ollama model for enrichment. |

## Embeddings

| Variable | Default | Required | Service | Description |
|---|---|---|---|---|
| `EMBEDDING_BACKEND` | `openai` | No | api, worker | Embedding provider: `openai`, `ollama`. |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | No | api, worker | Embedding model identifier. |
| `EMBEDDING_DIM` | `1536` | No | api, worker | Embedding vector dimensions. Must match the model. `text-embedding-3-small` = 1536, `nomic-embed-text` = 768. |
| `EMBEDDING_BATCH_SIZE` | `100` | No | api, worker | Max texts per embedding API call. |
| `EMBEDDING_RETRY_MAX` | `3` | No | api, worker | Max retries on embedding API failure. |
| `EMBEDDING_RETRY_BACKOFF` | `2.0` | No | api, worker | Exponential backoff factor (seconds). |

## Context Assembly

| Variable | Default | Required | Service | Description |
|---|---|---|---|---|
| `CONTEXT_CACHE_TTL` | `30` | No | api | Context block Redis TTL in seconds. |
| `CONTEXT_MAX_DEPTH` | `2` | No | api | Maximum BFS traversal depth from user node. |
| `CONTEXT_MAX_TOKENS` | `4096` | No | api | Maximum tokens in assembled context block. |
| `CONTEXT_TOP_K_VECTOR` | `20` | No | api | Top-K results from vector similarity search. |
| `CONTEXT_TOP_K_BM25` | `10` | No | api | Top-K results from BM25 full-text search. |
| `CONTEXT_TOP_K_GRAPH` | `10` | No | api | Top-K results from graph BFS traversal. |
| `CONTEXT_RRF_K` | `60` | No | api | RRF constant (higher = more weight to top ranks). |

## Worker

| Variable | Default | Required | Service | Description |
|---|---|---|---|---|
| `MAX_WORKERS` | `4` | No | worker | Total ARQ worker concurrency across all queues. |
| `WORKER_QUEUE_HIGH_CONCURRENCY` | `4` | No | worker | Concurrent job count for the `high` priority queue. |
| `WORKER_QUEUE_LOW_CONCURRENCY` | `2` | No | worker | Concurrent job count for the `low` priority queue. |
| `WORKER_RETRY_MAX` | `3` | No | worker | Max retries for failed worker tasks. |
| `WORKER_RETRY_BACKOFF` | `5.0` | No | worker | Base backoff in seconds (multiplied by attempt). |
| `WORKER_LLM_TIMEOUT` | `60` | No | worker | LLM API timeout per worker task in seconds. |
| `WORKER_DEAD_LETTER_TTL` | `604800` | No | worker | TTL for dead-letter queue entries in seconds (default 7d). |

## Authentication & Security

| Variable | Default | Required | Service | Description |
|---|---|---|---|---|
| `SECRET_KEY` | — | **Yes** | api | JWT signing secret. Must be at least 32 characters. |
| `JWT_SECRET` | — | **Yes** | api | Alias for SECRET_KEY (whichever is set). |
| `JWT_ALGORITHM` | `HS256` | No | api | JWT signing algorithm. |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | No | api | Dashboard JWT access token expiry. |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | No | api | Dashboard JWT refresh token expiry. |
| `API_KEY_PREFIX` | `mg_` | No | api | Prefix for generated API keys. |
| `RATE_LIMIT_PER_MINUTE` | `1000` | No | api | Default API rate limit per key per minute. |
| `RATE_LIMIT_AUTH_FAILURES` | `10` | No | api | Max failed auth attempts per IP per minute. |
| `MAX_MESSAGE_SIZE_BYTES` | `65536` | No | api | Max message content length (64KB default). |

## Observability

| Variable | Default | Required | Service | Description |
|---|---|---|---|---|
| `LOG_LEVEL` | `INFO` | No | all | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `ENVIRONMENT` | `development` | No | all | Deployment environment: `development`, `staging`, `production`. Controls log format. |
| `SERVICE_NAME` | — | No | all | Override service name in logs (defaults to `api`, `worker`, or `mcp`). |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | No | all | OpenTelemetry OTLP gRPC endpoint. |
| `OTEL_SERVICE_NAME` | — | No | all | Override OpenTelemetry service name. |
| `OTEL_SAMPLER` | `parentbased_always_on` | No | all | OpenTelemetry sampler type. |
| `OTEL_SAMPLER_ARG` | — | No | all | Sampler argument (e.g. ratio for `TraceIdRatioBased`). |

## Multi-Tenancy

| Variable | Default | Required | Service | Description |
|---|---|---|---|---|
| `DEFAULT_ORG_QUOTA_USERS` | `1000` | No | api | Default max users per organization. |
| `DEFAULT_ORG_QUOTA_GRAPH_NODES` | `50000` | No | api | Default max graph nodes per organization. |
| `DEFAULT_ORG_QUOTA_API_CALLS_PER_MONTH` | `100000` | No | api | Default monthly API call limit per organization. |

## Server

| Variable | Default | Required | Service | Description |
|---|---|---|---|---|
| `API_HOST` | `0.0.0.0` | No | api, mcp | HTTP bind address. |
| `API_PORT` | `8000` | No | api | API HTTP port. |
| `MCP_PORT` | `8001` | No | mcp | MCP server HTTP port (SSE transport). |
| `MCP_STDIO_ENABLED` | `true` | No | mcp | Enable stdio transport for MCP. |
| `MCP_SSE_ENABLED` | `true` | No | mcp | Enable SSE transport for MCP. |
| `WORKER_METRICS_PORT` | `9101` | No | worker | Worker Prometheus metrics port. |
| `CORS_ORIGINS` | `*` | No | api | Comma-separated allowed CORS origins for local dev. Set to specific origins in production. |

## Application

| Variable | Default | Required | Service | Description |
|---|---|---|---|---|
| `VERSION` | `0.1.0` | No | all | Application version. Used in health endpoint and logs. |
| `TZ` | `UTC` | No | all | Timezone for logs and timestamps. |

---

## Required vs Conditional

| Condition | Variables Required |
|---|---|
| Always | `DATABASE_URL`, `SECRET_KEY` (or `JWT_SECRET`) |
| LLM backend = `openai` | `OPENAI_API_KEY` |
| LLM backend = `azure` | `AZURE_API_KEY`, `AZURE_API_BASE`, `AZURE_DEPLOYMENT_NAME` |
| LLM backend = `ollama` | `OLLAMA_BASE_URL` |
| Graph backend = `neo4j` | `NEO4J_URI` |
| Production (docker-compose.prod.yml) | `DB_PASSWORD` |

---

## Example .env Files

### Development Minimal

```bash
DATABASE_URL=postgresql+asyncpg://memgraph:memgraph@localhost:5432/memgraph
REDIS_URL=redis://localhost:6379/0
FALKORDB_URL=redis://localhost:6380
OPENAI_API_KEY=sk-...
SECRET_KEY=dev-secret
```

### Production (docker-compose.prod.yml)

```bash
ENVIRONMENT=production
LOG_LEVEL=INFO
DATABASE_URL=postgresql+asyncpg://memgraph:${DB_PASSWORD}@pgbouncer:6432/memgraph
DATABASE_POOL_SIZE=5
REDIS_URL=redis-sentinel://redis-sentinel:26379/mymaster/0
FALKORDB_URL=redis://falkordb:6380
LLM_BACKEND=openai
OPENAI_MODEL=gpt-4o-mini
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=1536
CONTEXT_CACHE_TTL=30
SECRET_KEY_FILE=/run/secrets/jwt_secret
OTEL_EXPORTER_OTLP_ENDPOINT=http://alloy:4317
CORS_ORIGINS=https://dashboard.memgraph.example.com
```

### Air-Gapped (Ollama)

```bash
DATABASE_URL=postgresql+asyncpg://memgraph:memgraph@postgres:5432/memgraph
REDIS_URL=redis://redis:6379/0
FALKORDB_URL=redis://falkordb:6380
LLM_BACKEND=ollama
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=llama3.2
EMBEDDING_BACKEND=ollama
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIM=768
SECRET_KEY=air-gapped-secret-key
```

---

## Validation on Startup

The application validates required environment variables at startup:

```python
# core/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Database
    database_url: str
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=5, ge=0)

    # Graph
    graph_backend: str = Field(default="falkordb", pattern="^(falkordb|neo4j)$")
    falkordb_url: str = Field(default="redis://localhost:6380")
    neo4j_uri: Optional[str] = None

    # LLM
    llm_backend: str = Field(default="openai", pattern="^(openai|azure|ollama)$")
    openai_api_key: Optional[str] = None
    ollama_base_url: Optional[str] = None

    @field_validator("openai_api_key")
    @classmethod
    def validate_llm_key(cls, v, info):
        """Validate that the required API key is set for the chosen backend."""
        values = info.data
        backend = values.get("llm_backend")
        if backend in ("openai", "azure") and not v:
            raise ValueError(f"OPENAI_API_KEY is required when LLM_BACKEND={backend}")
        return v
```

---

## Defaults Summary

| Category | # Variables | # Defaults | # Required |
|---|---|---|---|
| Database | 4 | 3 | 1 |
| Graph | 4 | 2 | 1 (conditional) |
| Redis | 4 | 2 | 0 |
| LLM | 9 | 4 | 2 (conditional) |
| Embeddings | 5 | 4 | 0 |
| Context | 7 | 7 | 0 |
| Worker | 7 | 6 | 0 |
| Auth | 8 | 5 | 2 |
| Observability | 6 | 4 | 0 |
| Multi-tenancy | 3 | 3 | 0 |
| Server | 8 | 5 | 0 |
| Application | 2 | 2 | 0 |
| **Total** | **67** | **47** | **~6 (3 always + 3 conditional)** |
