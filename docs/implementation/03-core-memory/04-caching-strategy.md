# Caching Strategy — Implementation Guide

> **Domain:** Core Memory
> **SRS Phase:** Phase 1 — Core Memory (Week 3)
> **Requirements:** CTX-06, PERF-01, PERF-02
> **Doc Dependencies:** [01-message-ingestion.md](01-message-ingestion.md), [02-context-assembly.md](02-context-assembly.md)

---

## 1. Overview

OpenZep uses Redis as its primary caching layer. The caching strategy follows the **cache-aside** pattern exclusively — no write-through, no write-behind. Caches reduce latency for repetitive queries (context blocks, API key lookups) and protect downstream services (PostgreSQL, Graphiti, embedding API) from redundant load.

### 1.1 Cache Layers

| Cache Type | Key Pattern | TTL | Invalidated By | Purpose |
|------------|-------------|-----|----------------|---------|
| **Context block** | `ctx:{org_id}:{user_id}:{query_hash}` | 30s (configurable) | New message ingestion for user | Avoid re-running BFS+vector+BM25 for identical queries |
| **Query embedding** | `emb:query:{sha256(query)}` | 1h | — | Avoid re-embedding the same query within the TTL |
| **API key** | `apikey:{key_hash}` | 5min | Manual revocation (key delete) | Avoid bcrypt verification + DB lookup on every request |
| **Graph query** | `graph:{org_id}:{user_id}:{query_hash}` | 10s (optional) | Data mutation | Avoid re-running Graphiti traversal for identical paths |

### 1.2 Cache Stability and Performance

| Metric | Target | Measurement |
|--------|--------|-------------|
| Context cache hit ratio | ≥70% for conversational queries | `memgraph_cache_hit_total / (hit + miss)` per cache type |
| Context cache p50 latency | ≤2ms | Redis GET timed |
| API key cache hit ratio | ≥99% | `memgraph_cache_hit_total{type="apikey"}` |
| Cache miss penalty (context) | ≤645ms | Hybrid retrieval + assembly time |
| Cache miss penalty (apikey) | ≤50ms | bcrypt verify + DB lookup |

---

## 2. Redis Namespace Conventions

All Redis keys are namespaced to prevent collisions between environments and cache types:

```
OpenZep:{env}:{cache_type}:{key_suffix}
```

Where:
- `{env}` = environment name (e.g., `dev`, `staging`, `prod`)
- `{cache_type}` = cache category (e.g., `ctx`, `apikey`, `emb`)
- `{key_suffix}` = cache-specific key payload

**Examples:**
```
OpenZep:prod:ctx:org_abc:user_123:a1b2c3d4...
OpenZep:prod:apikey:$2b$12$...
OpenZep:prod:emb:query:e5f6g7h8...
```

### 2.1 Environment Prefix Configuration

```python
from app.core.config import settings

REDIS_KEY_PREFIX = f"OpenZep:{settings.ENVIRONMENT}:"
```

The `ENVIRONMENT` setting defaults to `dev` in development and `prod` in production. This ensures dev and prod caches never collide when sharing a Redis instance.

---

## 3. Cache-Aside Pattern

All caches follow the same pattern:

```python
async def get_or_compute(
    redis: Redis,
    cache_key: str,
    ttl: int,
    compute_fn: Callable[[], Awaitable[dict]],
) -> tuple[dict, bool]:
    """Cache-aside: read from cache, fall back to compute function.

    Args:
        redis: Redis client instance.
        cache_key: Full Redis key (including namespace prefix).
        ttl: TTL in seconds.
        compute_fn: Async function that computes the value on miss.

    Returns:
        Tuple of (value_dict, cache_hit_bool).
    """
    # ── Read from cache ──
    cached = await redis.get(cache_key)
    if cached is not None:
        return json.loads(cached), True

    # ── Compute ──
    value = await compute_fn()

    # ── Store with TTL ──
    await redis.setex(cache_key, ttl, json.dumps(value))

    return value, False
```

**Never write-through.** The compute function is always the authoritative source. Redis is disposable — cache loss is not data loss.

---

## 4. Context Block Cache

### 4.1 Key Format

```
OpenZep:{env}:ctx:{org_id}:{user_id}:{query_hash}
```

Where `query_hash` is SHA-256 of the normalised query string (lowercased, stripped, whitespace-collapsed).

### 4.2 Value Format

```json
{
  "context": "-- Source: entity --\n...",
  "source_counts": {"graph": 5, "vector": 8, "bm25": 3},
  "total_items": 16,
  "created_at": "2026-06-05T10:30:00Z"
}
```

The `created_at` field enables staleness tracking — if a cache entry is older than `CONTEXT_CACHE_TTL`, it's treated as expired even if the Redis TTL hasn't fired.

### 4.3 TTL

Default: **30 seconds** (configurable via `CONTEXT_CACHE_TTL`).

**Rationale:** Conversational agents tend to ask similar questions within a 30-second window during a single interaction. 30s is short enough that stale context is unlikely to cause issues, but long enough to absorb repeated queries.

### 4.4 Invalidation

**Invalidation trigger:** Every successful message ingestion deletes all context cache keys for the user.

```python
async def _invalidate_context_cache(
    redis: Redis,
    org_id: str,
    user_id: str,
) -> None:
    """Delete all context cache entries for a user after new ingestion.

    Uses Redis SCAN to iterate matching keys (non-blocking).
    """
    pattern = f"{REDIS_KEY_PREFIX}ctx:{org_id}:{user_id}:*"
    cursor = 0
    deleted = 0

    while True:
        cursor, keys = await redis.scan(
            cursor=cursor,
            match=pattern,
            count=100,
        )
        if keys:
            await redis.delete(*keys)
            deleted += len(keys)
        if cursor == 0:
            break

    if deleted > 0:
        logger.info("Invalidated context cache entries",
                    extra={"org_id": org_id, "user_id": user_id, "count": deleted})
```

**⚠️ Cache stampede risk:** If 10 concurrent requests arrive while the cache is cold after invalidation, all 10 hit the DB simultaneously. Mitigated via distributed lock — see [02-context-assembly.md §8.5](02-context-assembly.md#85-cache-stampede).

### 4.5 Expected Hit Ratio

For conversational queries (agents asking about the same user within a session), we expect:

| Scenario | Hit Ratio | Notes |
|----------|-----------|-------|
| Agent asks "What are Alice's preferences?" → "Tell me more about preferences" | ~70% | Similar queries produce same hash after normalisation |
| Agent asks random unrelated queries | ~5% | Each query is unique |
| After message ingestion (invalidation) | 0% | Cache wiped for that user |

Target overall: **≥70% hit ratio for conversational use cases.**

---

## 5. Query Embedding Cache

### 5.1 Key Format

```
OpenZep:{env}:emb:query:{sha256_of_normalised_query}
```

### 5.2 Value Format

```json
[0.0123, -0.0456, ...]  # embedding vector as JSON array
```

### 5.3 TTL

Default: **1 hour** (3600 seconds).

**Rationale:** Query embeddings are model-dependent. If the embedding model changes mid-deployment, a fast cache expiry ensures embeddings are regenerated. 1h balances cache efficiency with responsiveness to model changes.

### 5.4 Usage

This cache lives in the `HybridRetriever._embed_query` method:

```python
async def _embed_query(self, query: str) -> list[float]:
    cache_key = f"{REDIS_KEY_PREFIX}emb:query:{sha256(query.encode()).hexdigest()}"
    cached = await self._redis.get(cache_key)
    if cached:
        return json.loads(cached)

    embedding = await self._call_embedding_api(query)
    await self._redis.setex(cache_key, settings.EMBEDDING_CACHE_TTL, json.dumps(embedding))
    return embedding
```

---

## 6. API Key Cache

### 6.1 Key Format

```
OpenZep:{env}:apikey:{sha256_of_raw_key}
```

We hash the raw API key (SHA-256) to compute the cache key. This avoids storing the raw key in Redis and matches the lookup pattern in the auth middleware.

### 6.2 Value Format

```json
{
  "key_id": "uuid",
  "organization_id": "uuid",
  "prefix": "mg_live_",
  "scopes": ["read", "write"],
  "created_at": "2026-06-05T10:00:00Z",
  "last_used_at": "2026-06-05T10:30:00Z"
}
```

### 6.3 TTL

Default: **5 minutes** (300 seconds).

**Rationale:** API keys rarely change. 5min TTL avoids bcrypt verification + DB lookup on every request while ensuring revoked keys are invalidated within 5 minutes.

### 6.4 Invalidation on Revocation

When an admin revokes an API key via `DELETE /admin/organizations/{org_id}/keys/{key_id}`:

```python
async def revoke_api_key(org_id: str, key_id: str, redis: Redis) -> None:
    """Revoke an API key: delete from DB and invalidate cache."""
    # Delete from DB
    key_record = await api_key_repo.delete(org_id, key_id)

    # Invalidate cache
    cache_key = f"{REDIS_KEY_PREFIX}apikey:{key_record.key_hash}"
    await redis.delete(cache_key)
```

This ensures immediate invalidation without waiting for the 5min TTL.

### 6.5 Auth Middleware Integration

```python
async def get_api_key_org(
    request: Request,
    redis: Redis = Depends(get_redis),
) -> str:
    """FastAPI dependency that validates the API key and returns org_id.

    Uses cache-aside: Redis → bcrypt verify + DB lookup on miss.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    raw_key = auth_header.removeprefix("Bearer ").strip()
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    cache_key = f"{REDIS_KEY_PREFIX}apikey:{key_hash}"

    # Cache hit
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)["organization_id"]

    # Cache miss: verify against DB
    key_record = await api_key_repo.get_by_hash(key_hash)
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    # bcrypt verify the raw key against stored hash
    if not bcrypt.checkpw(raw_key.encode(), key_record.key_hash.encode()):
        # ⚠️ Potential timing attack? bcrypt.checkpw is constant-time.
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Update last_used_at (async, don't block on this)
    await api_key_repo.touch_last_used(key_record.id)

    # Cache the result
    await redis.setex(cache_key, 300, json.dumps({
        "key_id": str(key_record.id),
        "organization_id": str(key_record.organization_id),
        "prefix": key_record.prefix,
    }))

    return str(key_record.organization_id)
```

---

## 7. Graph Query Cache (Optional)

### 7.1 Key Format

```
OpenZep:{env}:graph:{org_id}:{user_id}:{query_hash}
```

### 7.2 TTL

Default: **10 seconds** (configurable via `GRAPH_CACHE_TTL`).

**Rationale:** Graph BFS traversal is the most expensive retrieval step (200-300ms). Caching identical traversal results for 10s is safe because graph data changes slower than episode data.

### 7.3 When to Use

The graph query cache is **optional** — disabled by default. Enable via:

| Environment Variable | Description |
|---------------------|-------------|
| `GRAPH_CACHE_ENABLED=true` | Enable graph query caching |
| `GRAPH_CACHE_TTL=10` | Graph cache TTL in seconds |

Enable only if graph traversal latency exceeds 300ms budget regularly.

---

## 8. Cache Invalidation Summary

| Cache Type | Invalidated By | Timeliness |
|------------|---------------|------------|
| Context block | New message ingestion for user | Immediate (Redis DEL with SCAN) |
| Context block | TTL expiry (30s) | Eventual |
| Query embedding | TTL expiry (1h) | Eventual |
| API key | Manual revocation | Immediate (Redis DEL key) |
| API key | TTL expiry (5min) | Eventual |
| Graph query (optional) | TTL expiry (10s) | Eventual |

**No cache is invalidated by data mutations that happen asynchronously** (e.g., entity extraction, fact extraction). The reasoning: these enrichments add data rather than correcting it. If a user asks a query, gets context, and then an async enrichment adds a new fact, the cache will expire naturally within 30s. This is acceptable — the agent can re-query after the enrichment completes.

---

## 9. Metrics

### 9.1 Prometheus Metrics

All cache operations emit Prometheus counters for monitoring and alerting.

```python
from prometheus_client import Counter, Histogram


CACHE_HIT_TOTAL = Counter(
    "memgraph_cache_hit_total",
    "Cache hits by cache type",
    labelnames=["type"],  # context, apikey, embedding, graph
)

CACHE_MISS_TOTAL = Counter(
    "memgraph_cache_miss_total",
    "Cache misses by cache type",
    labelnames=["type"],
)

CACHE_OPERATION_DURATION = Histogram(
    "memgraph_cache_operation_duration_seconds",
    "Cache operation latency by type and operation",
    labelnames=["type", "operation"],  # operation: get, set, delete
    buckets=[0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1],
)
```

### 9.2 Decorator Pattern

```python
import functools
from prometheus_client import Counter


def track_cache_metrics(cache_type: str):
    """Decorator to track cache hit/miss metrics."""
    hits = CACHE_HIT_TOTAL.labels(type=cache_type)
    misses = CACHE_MISS_TOTAL.labels(type=cache_type)

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            result, is_hit = await func(*args, **kwargs)
            if is_hit:
                hits.inc()
            else:
                misses.inc()
            return result
        return wrapper
    return decorator
```

### 9.3 Dashboard Panels (Grafana)

| Panel | Query | Description |
|-------|-------|-------------|
| Cache hit rate (by type) | `rate(memgraph_cache_hit_total{type="context"}[5m]) / (rate(memgraph_cache_hit_total{type="context"}[5m]) + rate(memgraph_cache_miss_total{type="context"}[5m]))` | Hit ratio per cache type |
| Cache operation latency | `histogram_quantile(0.99, rate(memgraph_cache_operation_duration_seconds_bucket[5m]))` | p99 cache latency |
| Cache entries evicted | `rate(memgraph_cache_eviction_total[5m])` | Eviction rate (if applicable) |

---

## 10. Error Handling

### 10.1 Redis Connection Failure

If Redis is unavailable, caches degrade gracefully:

```python
async def get_cached_or_compute(
    redis: Redis | None,
    cache_key: str,
    ttl: int,
    compute_fn: Callable[[], Awaitable[dict]],
) -> tuple[dict, bool]:
    """Cache-aside with graceful degradation when Redis is down."""
    # ── Attempt cache read ──
    if redis is not None:
        try:
            cached = await redis.get(cache_key)
            if cached is not None:
                return json.loads(cached), True
        except (ConnectionError, TimeoutError) as e:
            logger.warning("Redis cache read failed, falling through to compute",
                           extra={"cache_key": cache_key, "error": str(e)})

    # ── Compute (always works, Redis is optional) ──
    value = await compute_fn()

    # ── Attempt cache write (best-effort) ──
    if redis is not None:
        try:
            await redis.setex(cache_key, ttl, json.dumps(value))
        except (ConnectionError, TimeoutError) as e:
            logger.warning("Redis cache write failed (non-fatal)",
                           extra={"cache_key": cache_key, "error": str(e)})

    return value, False
```

**Key principle:** Redis failures must never cause request failures. The system operates correctly without caching — only slower.

### 10.2 TTL vs Staleness Trade-off

| TTL | Hit Rate | Staleness Risk |
|-----|----------|---------------|
| 10s | ~50% | Low — context expires quickly |
| 30s (default) | ~70% | Medium — acceptable for conversational context |
| 60s | ~80% | Medium — risk of stale context for rapidly updating data |
| 300s | ~90% | High — not recommended for context blocks |

For API keys, 5min TTL is safe because revocation is a manual action — 5min delay is acceptable.

---

## 11. Configuration Reference

| Variable | Default | Applies To | Description |
|----------|---------|------------|-------------|
| `CONTEXT_CACHE_TTL` | `30` | Context block cache | TTL in seconds |
| `EMBEDDING_CACHE_TTL` | `3600` | Query embedding cache | TTL in seconds |
| `API_KEY_CACHE_TTL` | `300` | API key cache | TTL in seconds |
| `GRAPH_CACHE_ENABLED` | `False` | Graph query cache | Enable graph traversal caching |
| `GRAPH_CACHE_TTL` | `10` | Graph query cache | TTL in seconds |

---

## 12. Testing Guide

### 12.1 Unit Tests

```python
import pytest
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_cache_hit_returns_cached():
    """Verify cache hit returns computed value without calling compute_fn."""
    redis = AsyncMock()
    redis.get.return_value = json.dumps({"result": "cached"}).encode()

    compute_fn = AsyncMock()  # Should NOT be called

    value, is_hit = await get_cached_or_compute(
        redis=redis,
        cache_key="test:key",
        ttl=30,
        compute_fn=compute_fn,
    )

    assert is_hit is True
    assert value["result"] == "cached"
    compute_fn.assert_not_called()


@pytest.mark.asyncio
async def test_cache_miss_calls_compute():
    """Verify cache miss calls compute_fn and caches the result."""
    redis = AsyncMock()
    redis.get.return_value = None  # Miss

    async def compute_fn():
        return {"result": "computed"}

    value, is_hit = await get_cached_or_compute(
        redis=redis,
        cache_key="test:key",
        ttl=30,
        compute_fn=compute_fn,
    )

    assert is_hit is False
    assert value["result"] == "computed"
    redis.setex.assert_called_once_with("test:key", 30, json.dumps({"result": "computed"}))


@pytest.mark.asyncio
async def test_redis_down_graceful_degradation():
    """Verify Redis failure falls through to compute_fn."""
    redis = AsyncMock()
    redis.get.side_effect = ConnectionError("Redis unreachable")

    compute_called = False

    async def compute_fn():
        nonlocal compute_called
        compute_called = True
        return {"result": "computed"}

    value, is_hit = await get_cached_or_compute(
        redis=redis,
        cache_key="test:key",
        ttl=30,
        compute_fn=compute_fn,
    )

    assert is_hit is False
    assert compute_called is True
    assert value["result"] == "computed"


@pytest.mark.asyncio
async def test_context_cache_invalidation():
    """Verify context cache invalidation deletes all user's keys."""
    redis = AsyncMock()
    redis.scan.side_effect = [
        (5, [b"OpenZep:prod:ctx:org1:user1:hash_a", b"OpenZep:prod:ctx:org1:user1:hash_b"]),
        (0, [b"OpenZep:prod:ctx:org1:user1:hash_c"]),
    ]

    await _invalidate_context_cache(redis, "org1", "user1")

    # SCAN called at least twice (cursor loop)
    assert redis.scan.call_count >= 2
    # All matching keys deleted
    assert redis.delete.call_count >= 2  # Two batch deletes


@pytest.mark.asyncio
async def test_no_cache_redis_none():
    """Verify compute proceeds when Redis is None (no cache configured)."""
    value, is_hit = await get_cached_or_compute(
        redis=None,
        cache_key="test:key",
        ttl=30,
        compute_fn=lambda: {"result": "computed"},
    )

    assert is_hit is False
    assert value["result"] == "computed"
```

### 12.2 Integration Tests

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_context_cache_persistence(
    async_client: AsyncClient,
    auth_headers: dict,
    seed_user_with_memory: dict,
):
    """Verify context block is cached and returned on subsequent request."""
    params = {"query": "cached query test", "limit": 10}

    # First request — should miss cache
    resp1 = await async_client.get(
        f"/v1/users/{seed_user_with_memory['external_id']}/context",
        params=params,
        headers=auth_headers,
    )
    assert resp1.json()["metadata"]["cache_hit"] is False

    # Second request — should hit cache
    resp2 = await async_client.get(
        f"/v1/users/{seed_user_with_memory['external_id']}/context",
        params=params,
        headers=auth_headers,
    )
    assert resp2.json()["metadata"]["cache_hit"] is True
    assert resp2.json()["context"] == resp1.json()["context"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_context_cache_invalidation_on_ingestion(
    async_client: AsyncClient,
    auth_headers: dict,
    seed_user: dict,
):
    """Verify context cache is invalidated after message ingestion."""
    # First, ingest a message
    ingest_resp = await async_client.post(
        f"/v1/users/{seed_user['external_id']}/memory",
        json={"messages": [{"role": "user", "content": "My name is Alice."}]},
        headers=auth_headers,
    )
    assert ingest_resp.status_code == 202

    # Get context — populates cache
    ctx_resp1 = await async_client.get(
        f"/v1/users/{seed_user['external_id']}/context",
        params={"query": "Alice", "limit": 10},
        headers=auth_headers,
    )
    assert ctx_resp1.json()["metadata"]["cache_hit"] is False

    # Second get — cache hit
    ctx_resp2 = await async_client.get(
        f"/v1/users/{seed_user['external_id']}/context",
        params={"query": "Alice", "limit": 10},
        headers=auth_headers,
    )
    assert ctx_resp2.json()["metadata"]["cache_hit"] is True

    # Ingest more messages — should invalidate cache
    ingest_resp2 = await async_client.post(
        f"/v1/users/{seed_user['external_id']}/memory",
        json={"messages": [{"role": "assistant", "content": "Nice to meet you, Alice!"}]},
        headers=auth_headers,
    )
    assert ingest_resp2.status_code == 202

    # Third get — cache miss (invalidated by ingestion)
    ctx_resp3 = await async_client.get(
        f"/v1/users/{seed_user['external_id']}/context",
        params={"query": "Alice", "limit": 10},
        headers=auth_headers,
    )
    assert ctx_resp3.json()["metadata"]["cache_hit"] is False
```

---

## 13. Open Questions

| # | Question | Decision |
|---|----------|----------|
| OQ-1 | Should we implement cache warming on startup? | Not in P0. Cache warming is useful for API keys but adds startup complexity. The first few requests will be slow (cold cache) — acceptable for P0. |
| OQ-2 | Should context cache include query variants (stemming, synonyms)? | Not in P0. Identical queries after normalisation are cached. Synonym expansion would reduce hit rate and increase complexity. |
| OQ-3 | Should we use Redis Cluster for horizontal caching? | Stay with single-node Redis for P0/P1. Migrate to Redis Cluster or Elasticache if cache exceeds 10GB or throughput exceeds 50K ops/sec. |

---

*Document maintained by the OpenZep team. Update this document if caching patterns or Redis topology change.*
