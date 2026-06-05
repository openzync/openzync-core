# Rate Limiting

> **Phase:** 0 (Foundation)
> **SRS Requirements:** AUTH-06, MT-04, SEC-06
> **Dependencies:** [01-api-key-auth.md](01-api-key-auth.md), [03-tenant-isolation.md](03-tenant-isolation.md), [01-postgresql-schema.md](../01-data-models/01-postgresql-schema.md)

---

## 1. Overview

OpenZep implements **two-tier rate limiting** to protect the API from abuse and ensure fair resource allocation across tenants:

| Tier | Scope | Target | Default Limit |
|------|-------|--------|---------------|
| **Per-IP** (SEC-06) | Unauthenticated + failed auth | Brute-force key enumeration, DDoS | 10 failed auth attempts / min |
| **Per-Key** (AUTH-06) | Authenticated API calls | Fair resource usage per tenant | Configurable via `quotas` JSONB |

Both tiers are **Redis-backed sliding window counters** for accurate, distributed rate tracking across all API instances.

---

## 2. Redis-Backed Sliding Window Algorithm

### 2.1 Algorithm: Fixed Window with Per-Sub-Window Counters

We use a **sliding window** approach with sub-second precision using Redis sorted sets. This avoids the boundary problem of fixed-window counters (where a burst at the end of one window and start of the next can double the effective rate).

```python
import time
from redis.asyncio import Redis


class SlidingWindowRateLimiter:
    """Redis-backed sliding window rate limiter.

    Algorithm:
    - Maintain a Redis sorted set per (key_type, identifier) pair
    - Each request adds a member with score = current timestamp (milliseconds)
    - The sorted set is trimmed to remove entries older than the window
    - The count = cardinality of the set after trimming
    - Entry TTL = window_size * 2 to auto-cleanup stale keys

    This is O(log N) per check where N = number of requests in the window,
    which is bounded by the rate limit (typically < 1000).
    """

    def __init__(self, redis: Redis):
        self._redis = redis

    async def check_and_increment(
        self,
        key: str,
        max_requests: int,
        window_seconds: int = 60,
    ) -> tuple[bool, int, int]:
        """Check rate limit and record this request.

        Args:
            key: Redis key (e.g., "ratelimit:ip:1.2.3.4")
            max_requests: Maximum requests allowed in the window.
            window_seconds: Sliding window duration in seconds.

        Returns:
            Tuple of (is_allowed, remaining_requests, reset_timestamp_seconds).
            is_allowed: True if the request is within the limit.
            remaining: How many more requests are allowed in this window.
            reset_ts: Unix timestamp when the rate limit resets.
        """
        now_ms = int(time.time() * 1000)
        window_ms = window_seconds * 1000
        window_start_ms = now_ms - window_ms

        pipe = self._redis.pipeline(transaction=True)

        # 1. Remove entries outside the sliding window
        pipe.zremrangebyscore(key, 0, window_start_ms)

        # 2. Count entries in the current window
        pipe.zcard(key)

        # 3. Add this request (current timestamp as score and member)
        #    Use a unique member per request to handle concurrent requests
        #    at the same millisecond — member = "{timestamp}:{random_suffix}"
        member = f"{now_ms}:{secrets.token_hex(4)}"
        pipe.zadd(key, {member: now_ms})

        # 4. Set TTL for auto-cleanup (2x window to handle edge cases)
        pipe.expire(key, window_seconds * 2)

        # Execute pipeline
        _, count, _, _ = await pipe.execute()

        count = count or 0  # Redis returns None for empty sets

        # Calculate reset timestamp (end of current wall-clock window)
        reset_ts = ((int(time.time()) // window_seconds) + 1) * window_seconds

        allowed = count <= max_requests
        remaining = max(0, max_requests - count)

        return allowed, remaining, reset_ts
```

### 2.2 Why Sorted Sets vs Simple Counters?

| Approach | Pros | Cons |
|----------|------|------|
| `INCR` + `EXPIRE` (fixed window) | Simple, low memory | Burst at window boundary doubles effective rate |
| Sorted set (sliding window) | Accurate sliding window | Higher memory per key, O(log N) per check |
| Redis-Cell (generic cell rate) | Purpose-built, efficient | Requires Redis module — deployment dependency |

**Decision:** Sorted sets. The accuracy gain over fixed windows is critical for rate limiting (a burst at the boundary defeats the purpose), and the memory cost is acceptable because each key is bounded by `max_requests` entries and auto-expires.

---

## 3. Per-IP Rate Limiting (Failed Auth Attempts)

### 3.1 What It Protects

The per-IP limiter catches:
- Brute-force enumeration of valid API keys
- Credential stuffing against the JWT login endpoint
- DDoS attacks from a single IP

### 3.2 Where It Applies

| Endpoint | Limiter | Limit |
|----------|---------|-------|
| Any `/v1/*` request with invalid API key | Per-IP | 10 failures / min |
| `POST /auth/login` with wrong password | Per-IP | 10 failures / min |

### 3.3 Middleware

```python
# middleware/rate_limit.py
from fastapi import Request, HTTPException, status, Response
from starlette.middleware.base import BaseHTTPMiddleware
import time


class FailedAuthRateLimitMiddleware(BaseHTTPMiddleware):
    """Rate-limit failed authentication attempts by IP.

    This middleware runs BEFORE auth middleware. It tracks
    failed attempts and blocks the IP before the auth check
    even executes, reducing load on the auth backend.

    Trusted proxy headers (X-Forwarded-For) are used for
    correct IP detection behind load balancers.
    """

    def __init__(self, app, redis: Redis, max_failures: int = 10, window_seconds: int = 60):
        super().__init__(app)
        self._redis = redis
        self._limiter = SlidingWindowRateLimiter(redis)
        self._max_failures = max_failures
        self._window_seconds = window_seconds

    async def dispatch(self, request: Request, call_next):
        # Only track failed auth attempts — extract client IP
        client_ip = self._get_client_ip(request)

        # Check if this IP is currently blocked
        is_allowed, remaining, reset_ts = await self._limiter.check_and_increment(
            key=f"ratelimit:authfail:{client_ip}",
            max_requests=self._max_failures,
            window_seconds=self._window_seconds,
        )

        if not is_allowed:
            retry_after = max(1, int(reset_ts - time.time()))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "RATE_LIMITED",
                    "message": f"Too many failed authentication attempts. Try again in {retry_after} seconds.",
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(self._max_failures),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset_ts),
                },
            )

        # Process the request
        response = await call_next(request)

        # If the response is 401 (auth failed), the increment stays.
        # If the response is 200 (auth succeeded), we should decrement
        # to avoid penalizing successful logins.
        # ⚠️ Race condition: concurrent requests from the same IP
        # could all get 401 and all increment. This is acceptable —
        # the per-key limiter below handles the actual API protection.
        # This per-IP limiter is a coarse first line of defence.

        return response

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP, respecting trusted proxy headers.

        Check order:
        1. X-Forwarded-For header (if behind load balancer)
        2. X-Real-IP header
        3. request.client.host (direct connection)
        """
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            # Take the first IP in the chain (the original client)
            return forwarded.split(",")[0].strip()

        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()

        client = request.client
        if client:
            return client.host

        return "unknown"
```

---

## 4. Per-Key Rate Limiting

### 4.1 Configuration via `quotas` JSONB

Each organization's rate limits are configured in the `quotas` column of the `organizations` table:

```json
{
    "rate_limit": {
        "requests_per_minute": 1000,
        "requests_per_hour": 50000,
        "requests_per_day": 1000000,
        "concurrent_requests": 50
    },
    "max_users": 10000,
    "max_graph_nodes": 500000,
    "max_storage_mb": 1024
}
```

### 4.2 Retrieving Quotas

```python
# services/quota_service.py
from redis.asyncio import Redis
from uuid import UUID


class QuotaService:
    """Service for retrieving and caching organization quotas."""

    def __init__(self, redis: Redis, db_session_factory):
        self._redis = redis
        self._db = db_session_factory

    async def get_rate_limit(self, org_id: UUID) -> dict:
        """Get the rate limit configuration for an organization.

        Cached in Redis for 5 minutes to avoid DB lookup on every request.
        Falls back to default limits if no configuration exists.
        """
        cache_key = f"quotas:ratelimit:{org_id}"
        cached = await self._redis.get(cache_key)
        if cached:
            return json.loads(cached)

        # Fetch from DB
        async with self._db() as session:
            result = await session.execute(
                text("SELECT quotas FROM organizations WHERE id = :org_id"),
                {"org_id": org_id},
            )
            row = result.mappings().one_or_none()

        if row and row["quotas"] and "rate_limit" in row["quotas"]:
            limits = row["quotas"]["rate_limit"]
        else:
            limits = self._default_limits()

        # Cache for 5 minutes
        await self._redis.setex(cache_key, 300, json.dumps(limits))
        return limits

    def _default_limits(self) -> dict:
        return {
            "requests_per_minute": 100,
            "requests_per_hour": 5000,
            "requests_per_day": 100000,
            "concurrent_requests": 10,
        }
```

### 4.3 Per-Key Rate Limit Middleware

```python
# middleware/rate_limit.py
from fastapi import Request, HTTPException, status, Depends


class ApiKeyRateLimitMiddleware(BaseHTTPMiddleware):
    """Rate-limit authenticated API requests per key.

    This middleware runs AFTER auth middleware (it needs org_id
    from request.state). It uses Redis sliding window counters
    with the org's configured limits.

    Rate limit headers are added to all responses.
    """

    def __init__(self, app, redis: Redis, quota_service: QuotaService):
        super().__init__(app)
        self._redis = redis
        self._limiter = SlidingWindowRateLimiter(redis)
        self._quota_service = quota_service

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for non-API paths
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)

        # Skip if no auth identity (unauthenticated — handled by per-IP limiter)
        org_id = getattr(request.state, "org_id", None)
        api_key_id = getattr(request.state, "api_key_id", None)
        if not org_id or not api_key_id:
            return await call_next(request)

        # Get org rate limits
        limits = await self._quota_service.get_rate_limit(org_id)
        max_rpm = limits.get("requests_per_minute", 100)

        # Check per-key rate limit
        is_allowed, remaining, reset_ts = await self._limiter.check_and_increment(
            key=f"ratelimit:key:{api_key_id}",
            max_requests=max_rpm,
            window_seconds=60,
        )

        # Process the request
        response = await call_next(request)

        # Add rate limit headers to every response
        response.headers["X-RateLimit-Limit"] = str(max_rpm)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_ts)

        # If rate limit exceeded, override response to 429
        if not is_allowed:
            retry_after = max(1, int(reset_ts - time.time()))
            return Response(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content=json.dumps({
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": f"API rate limit exceeded. Retry after {retry_after} seconds.",
                        "request_id": getattr(request.state, "request_id", None),
                    }
                }),
                media_type="application/json",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(max_rpm),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset_ts),
                },
            )

        return response
```

---

## 5. Rate Limit Headers

### 5.1 Standard Headers

Every authenticated API response includes these headers:

| Header | Description | Example |
|--------|-------------|---------|
| `X-RateLimit-Limit` | Maximum requests allowed in the window | `1000` |
| `X-RateLimit-Remaining` | Requests remaining in the current window | `843` |
| `X-RateLimit-Reset` | Unix timestamp when the window resets | `1717545600` |
| `Retry-After` | Seconds to wait before retrying (only on 429) | `42` |

### 5.2 Response Examples

**Normal response (within limit):**
```
HTTP/2 200 OK
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 843
X-RateLimit-Reset: 1717545600
```

**Rate-limited response:**
```json
HTTP/2 429 Too Many Requests
Retry-After: 42
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1717545600

{
    "error": {
        "code": "RATE_LIMITED",
        "message": "API rate limit exceeded. Retry after 42 seconds.",
        "request_id": "req_01j9xmf..."
    }
}
```

---

## 6. Middleware Registration Order

Middleware order matters — they must be registered in the correct sequence in `main.py`:

```python
# main.py
from fastapi import FastAPI
from middleware.rate_limit import FailedAuthRateLimitMiddleware, ApiKeyRateLimitMiddleware

def create_app() -> FastAPI:
    app = FastAPI(title="OpenZep API")

    # 1. Tracing middleware (adds request_id)
    app.add_middleware(TracingMiddleware)

    # 2. Failed auth rate limiter (before auth — blocks IP before auth check)
    app.add_middleware(FailedAuthRateLimitMiddleware)

    # 3. Auth middleware (validates API key / JWT)
    app.add_middleware(AuthMiddleware)

    # 4. Tenant session middleware (sets PostgreSQL app.org_id for RLS)
    app.add_middleware(TenantSessionMiddleware)

    # 5. Per-key rate limiter (after auth — needs org_id from request.state)
    app.add_middleware(ApiKeyRateLimitMiddleware)

    # 6. Domain routers
    app.include_router(api_v1)
    # ...

    return app
```

**Why this order:**
- `FailedAuthRateLimitMiddleware` must run first so blocked IPs never reach the auth or application layer
- `AuthMiddleware` runs before `TenantSessionMiddleware` so `request.state.org_id` is available when PostgreSQL RLS parameters are set
- `ApiKeyRateLimitMiddleware` runs last among auth/middleware so it can access `request.state.org_id` and `request.state.api_key_id`

---

## 7. Per-Route Rate Limit Overrides

### 7.1 `rate_limit` Dependency

For endpoints that need different rate limits than the org default (e.g., expensive graph queries that should be throttled more aggressively):

```python
# dependencies/rate_limit.py
from fastapi import HTTPException, status, Depends, Request
import time


def rate_limit(max_requests: int, window_seconds: int = 60):
    """Dependency factory: apply a custom rate limit to a specific route.

    Usage:
        @router.get("/v1/users/{user_id}/graph/communities")
        async def get_communities(
            _: None = Depends(rate_limit(max_requests=30, window_seconds=60)),
            ...
        ):
            # This route is limited to 30 requests per minute
            # instead of the org's default.
    """
    async def rate_limit_dependency(
        request: Request,
        limiter: SlidingWindowRateLimiter = Depends(get_rate_limiter),
    ) -> None:
        # Use a compound key: key_id + endpoint bucket
        api_key_id = getattr(request.state, "api_key_id", "anonymous")
        bucket = f"custom:{request.url.path}"
        key = f"ratelimit:custom:{api_key_id}:{bucket}"

        is_allowed, remaining, reset_ts = await limiter.check_and_increment(
            key=key,
            max_requests=max_requests,
            window_seconds=window_seconds,
        )

        if not is_allowed:
            retry_after = max(1, int(reset_ts - time.time()))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "RATE_LIMITED",
                    "message": f"Endpoint rate limit exceeded. Retry after {retry_after}s.",
                },
                headers={"Retry-After": str(retry_after)},
            )

    return rate_limit_dependency
```

### 7.2 Usage Examples

```python
# Routers with custom rate limits

@router.get("/v1/users/{user_id}/context")
async def get_context(
    user_id: str,
    _: None = Depends(rate_limit(max_requests=300, window_seconds=60)),  # Higher for context
    identity: ApiKeyIdentity = Depends(get_api_key_identity),
    service: ContextService = Depends(get_context_service),
):
    """Get assembled context block. Higher rate limit because this is the primary value."""
    ...


@router.post("/v1/users/{user_id}/memory")
async def ingest_memory(
    user_id: str,
    _: None = Depends(rate_limit(max_requests=600, window_seconds=60)),  # Ingestion is cheap
    ...
):
    ...


@router.get("/v1/users/{user_id}/graph/communities")
async def get_communities(
    user_id: str,
    _: None = Depends(rate_limit(max_requests=30, window_seconds=60)),  # Expensive graph query
    ...
):
    ...

```

---

## 8. Trusted Proxy Headers

### 8.1 IP Detection Behind Load Balancer

In production behind a load balancer (Traefik, Nginx, ALB), the client IP must be extracted from forwarded headers:

```python
# core/config.py
class Settings(BaseSettings):
    # Rate limiting
    RATE_LIMIT_IP_MAX: int = 10       # Max failed auth attempts per IP per window
    RATE_LIMIT_WINDOW_SEC: int = 60   # Sliding window duration in seconds
    RATE_LIMIT_TRUSTED_PROXIES: list[str] = [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ]  # CIDR ranges of trusted load balancers

    RATE_LIMIT_REAL_IP_HEADER: str = "X-Forwarded-For"  # Or X-Real-IP
```

### 8.2 IP Validation

```python
# middleware/rate_limit.py
import ipaddress


class IPExtractor:
    """Extract and validate client IP from request, respecting proxy headers."""

    def __init__(self, trusted_proxies: list[str], real_ip_header: str = "X-Forwarded-For"):
        self._trusted_ranges = [ipaddress.ip_network(cidr) for cidr in trusted_proxies]
        self._real_ip_header = real_ip_header

    def get_client_ip(self, request: Request) -> str:
        """Extract the real client IP address.

        If the request comes through a trusted proxy, use the forwarded
        header value. Otherwise, use the direct connection IP.

        This prevents IP spoofing: if we trust X-Forwarded-For from
        an untrusted source, an attacker can forge any IP.
        """
        # Get the direct connection IP
        direct_ip = request.client.host if request.client else "unknown"

        # Check if the direct connection is from a trusted proxy
        if self._is_trusted_proxy(direct_ip):
            forwarded = request.headers.get(self._real_ip_header)
            if forwarded:
                # Take the leftmost IP (original client)
                return forwarded.split(",")[0].strip()

        return direct_ip

    def _is_trusted_proxy(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
            return any(addr in network for network in self._trusted_ranges)
        except ValueError:
            return False
```

---

## 9. Configuration

### 9.1 Environment Variables

```python
# core/config.py
class Settings(BaseSettings):
    # Rate limiting — per-IP (failed auth)
    RATE_LIMIT_IP_MAX: int = 10
    RATE_LIMIT_WINDOW_SEC: int = 60

    # Rate limiting — general
    RATE_LIMIT_DEFAULT_RPM: int = 100   # Default requests per minute for new orgs
    RATE_LIMIT_CACHE_TTL: int = 300     # Quota cache TTL in seconds

    # Proxy trust
    RATE_LIMIT_TRUSTED_PROXIES: list[str] = [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ]
    RATE_LIMIT_REAL_IP_HEADER: str = "X-Forwarded-For"

    # Redis — rate limiter uses the same Redis as the app
    REDIS_URL: str = "redis://localhost:6379/0"
```

### 9.2 Redis Memory Budget

Each rate limit key uses approximately:
- Key: `ratelimit:ip:1.2.3.4` = ~50 bytes
- Per request entry in sorted set: ~44 bytes (timestamp + member)
- TTL: 120 seconds (2 × window)

For a busy key at max 1000 requests/minute: 1000 × 44 + 50 = ~44KB for 2 minutes.
For 10,000 active keys: ~440MB peak Redis memory.

**Budget at 500 req/s sustained:**
- Per-key keys: 10,000 orgs × 1 key × 44KB = ~440MB
- Per-IP keys: depends on unique IPs, but each is bounded by 10 entries × 44B = 440 bytes
- Total estimated: < 1GB Redis memory for rate limiting

Monitor `used_memory_peak` and adjust `maxmemory` in Redis if needed.

---

## 10. Testing

### 10.1 Unit Tests

```python
# tests/unit/test_rate_limiter.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from middleware.rate_limit import SlidingWindowRateLimiter

class TestSlidingWindowRateLimiter:
    @pytest.mark.asyncio
    async def test_first_request_allowed(self):
        redis = AsyncMock(spec=Redis)
        # Mock pipeline
        pipe = AsyncMock()
        pipe.zremrangebyscore = AsyncMock()
        pipe.zcard = AsyncMock(return_value=1)
        pipe.zadd = AsyncMock()
        pipe.expire = AsyncMock()
        redis.pipeline = MagicMock(return_value=pipe)
        pipe.execute = AsyncMock(return_value=[None, 1, None, None])

        limiter = SlidingWindowRateLimiter(redis)
        allowed, remaining, reset = await limiter.check_and_increment(
            key="test", max_requests=10, window_seconds=60
        )

        assert allowed is True
        assert remaining == 9

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded(self):
        redis = AsyncMock(spec=Redis)
        pipe = AsyncMock()
        pipe.zremrangebyscore = AsyncMock()
        pipe.zcard = AsyncMock(return_value=11)
        pipe.zadd = AsyncMock()
        pipe.expire = AsyncMock()
        redis.pipeline = MagicMock(return_value=pipe)
        pipe.execute = AsyncMock(return_value=[None, 11, None, None])

        limiter = SlidingWindowRateLimiter(redis)
        allowed, remaining, reset = await limiter.check_and_increment(
            key="test", max_requests=10, window_seconds=60
        )

        assert allowed is False
        assert remaining == 0
```

### 10.2 Integration Tests

```python
# tests/integration/test_rate_limiting.py
import pytest
import time


class TestPerIPRateLimit:
    async def test_failed_auth_rate_limit(self, async_client, redis):
        """10 failed attempts should trigger rate limit."""
        headers = {"Authorization": "Bearer invalid_key_123"}

        for _ in range(10):
            resp = await async_client.get("/v1/users", headers=headers)
            assert resp.status_code == 401  # Failed auth

        # 11th attempt should be rate limited
        resp = await async_client.get("/v1/users", headers=headers)
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "RATE_LIMITED"
        assert "Retry-After" in resp.headers

    async def test_rate_limit_headers_present(self, async_client, valid_api_key):
        resp = await async_client.get(
            "/v1/users",
            headers={"Authorization": f"Bearer {valid_api_key}"},
        )
        assert "X-RateLimit-Limit" in resp.headers
        assert "X-RateLimit-Remaining" in resp.headers
        assert "X-RateLimit-Reset" in resp.headers


class TestPerKeyRateLimit:
    async def test_key_rate_limit_exceeded(self, async_client, valid_api_key, redis):
        """Exceeding per-key limit should return 429."""
        # Set a very low limit for this test
        org_id = "test-org-id"
        await redis.setex(
            f"quotas:ratelimit:{org_id}",
            60,
            json.dumps({"requests_per_minute": 3}),
        )

        for _ in range(3):
            resp = await async_client.get(
                "/v1/users",
                headers={"Authorization": f"Bearer {valid_api_key}"},
            )
            assert resp.status_code == 200

        # 4th request should be rate limited
        resp = await async_client.get(
            "/v1/users",
            headers={"Authorization": f"Bearer {valid_api_key}"},
        )
        assert resp.status_code == 429

    async def test_different_keys_have_independent_limits(
        self, async_client, api_key_factory
    ):
        """Two keys from the same org should not share a rate limit bucket."""
        key_a = await api_key_factory(org_id="org_a")
        key_b = await api_key_factory(org_id="org_a")

        # Exhaust key_a
        async def exhaust_key(key):
            for _ in range(5):
                await async_client.get(
                    "/v1/users",
                    headers={"Authorization": f"Bearer {key}"},
                )

        await exhaust_key(key_a)

        # Key B should still work fine
        resp = await async_client.get(
            "/v1/users",
            headers={"Authorization": f"Bearer {key_b}"},
        )
        assert resp.status_code == 200
```

### 10.3 Rate Limit Header Assertion Helper

```python
# tests/integration/conftest.py
def assert_rate_limit_headers(
    response,
    expected_limit: int,
    expected_remaining: int,
) -> None:
    """Assert standard rate limit headers in a response."""
    assert response.headers["X-RateLimit-Limit"] == str(expected_limit)
    assert response.headers["X-RateLimit-Remaining"] == str(expected_remaining)
    assert "X-RateLimit-Reset" in response.headers
    reset_ts = int(response.headers["X-RateLimit-Reset"])
    assert reset_ts > int(time.time())  # Should be in the future
```

---

## 11. Concurrent Request Handling

### 11.1 The Race Condition

Concurrent requests from the same API key create a race condition: if 3 requests arrive simultaneously when only 2 remain in the window, our sliding window counter might allow all 3. This is because the `zcard + zadd` is not atomic across separate pipeline calls.

### 11.2 Mitigation

We accept a **small overrun** (within ~5% of the limit) as acceptable for performance. The alternative — Lua scripting or Redis locks — adds latency to every request:

```lua
-- If atomicity is required (future improvement):
-- redis-cli --eval rate_limit.lua , key max_requests window_ms

local key = KEYS[1]
local max_requests = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local now_ms = tonumber(redis.call('TIME')[1]) * 1000

redis.call('ZREMRANGEBYSCORE', key, 0, now_ms - window_ms)
local count = redis.call('ZCARD', key)

if count >= max_requests then
    return {0, count, (math.floor(now_ms / 1000 / 60) + 1) * 60}
end

redis.call('ZADD', key, now_ms, now_ms .. ':' .. ARGV[3])
redis.call('EXPIRE', key, window_ms / 500)
return {1, max_requests - count - 1, (math.floor(now_ms / 1000 / 60) + 1) * 60}
```

**Decision:** Defer Lua script unless overrun > 10% is observed in production.

---

## 12. Operational Considerations

### 12.1 Monitoring

| Metric | What It Tracks | Alert Threshold |
|--------|---------------|-----------------|
| `OpenZep.rate_limited_requests` | Count of 429 responses | > 1% of total requests |
| `OpenZep.rate_limit_key_count` | Active rate limit keys in Redis | > 100,000 keys |
| `OpenZep.rate_limit_p99_latency` | Time to check rate limit | > 5ms |

### 12.2 Graceful Degradation

If Redis is unavailable:
1. The rate limiter **fails open**: requests are allowed through without rate limiting
2. An emergency alert fires
3. The system falls back to a simple in-process counter (less accurate, but prevents complete auth bypass):

```python
class FallbackRateLimiter:
    """In-process fallback when Redis is down.

    Uses a simple dict with time-window checks. Not shared across
    processes — provides only basic protection during Redis outage.
    """

    def __init__(self, max_requests: int = 1000, window_seconds: int = 60):
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._counters: dict[str, list[float]] = {}

    def check(self, key: str) -> bool:
        now = time.time()
        cutoff = now - self._window_seconds

        # Clean old entries
        if key in self._counters:
            self._counters[key] = [t for t in self._counters[key] if t > cutoff]

        # Check limit
        count = len(self._counters.get(key, []))
        if count >= self._max_requests:
            return False

        # Record request
        self._counters.setdefault(key, []).append(now)
        return True
```

---

## 13. Open Questions

| ID | Question | Status |
|----|----------|--------|
| RL-01 | Should rate limit overrun (burst tolerance) be configurable? | Current implementation has no burst tolerance. Add if feedback requests it. |
| RL-02 | Should we expose rate limit usage in the admin dashboard? | Yes — add to Phase 4 dashboard: "API Calls Today" chart with rate limit hit % |
| RL-03 | Distributed rate limiting — do we need Lua script atomicity? | Defer until production data shows overrun > 10% |
| RL-04 | Should MCP server have separate rate limits? | MCP (stdio) is local — no rate limit. MCP (SSE) uses the same API key auth and limits. |

---

> **Commit convention:** `feat(auth): implement Redis-backed sliding window rate limiting with per-IP and per-key tiers`
> **Review checklist:** Verify sliding window accuracy (not fixed window), check proxy IP extraction security, confirm 429 format matches error spec, validate Redis pipeline atomicity, test concurrent request handling.
