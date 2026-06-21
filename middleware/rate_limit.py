"""Sliding-window rate limiting middleware using Redis sorted sets.

Implements a sliding-window counter with Redis ``ZREMRANGEBYSCORE`` /
``ZADD`` / ``ZCOUNT`` for precise per-second granularity without race
conditions inherent to fixed-window counters.

Two tiers of rate limiting are enforced:

1. **IP-based** — for unauthenticated endpoints (health, docs, etc.).
   Key: ``rate:auth:{ip}`` — 10 requests per 60-second window.
2. **Org-based** — for authenticated API endpoints.
   Key: ``rate:api:{org_id}`` — rate determined by org quota or fallback.

All responses include RFC 7807 body on 429.

Graceful degradation: if Redis is unreachable the request is allowed through
with a warning logged.
"""

from __future__ import annotations

import orjson
import logging
import time
from typing import Any

import redis.asyncio as aioredis
from starlette.types import ASGIApp, Receive, Scope, Send

from core.config import settings

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

AUTH_RATE_LIMIT: int = 10
"""Default max requests per window for unauthenticated (IP-based) requests."""

AUTH_RATE_WINDOW: int = 60
"""Window in seconds for unauthenticated requests."""

FALLBACK_ORG_RATE_LIMIT: int = 1000
"""Fallback max requests per window for org-based requests when no quota is set."""

FALLBACK_ORG_WINDOW: int = 60
"""Window in seconds for org-based requests."""

RFC_7807_TYPE = "https://errors.openzep.dev/rate_limit_exceeded"


# ═══════════════════════════════════════════════════════════════════════════════
# Rate limit configuration helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _get_client_ip(scope: Scope) -> str:
    """Extract the originating client IP from the ASGI scope.

    Respects ``X-Forwarded-For`` when behind a reverse proxy, falling back
    to the direct client address.

    Args:
        scope: The ASGI connection scope.

    Returns:
        The client IP address as a string.
    """
    headers = dict(scope.get("headers") or [])
    forwarded = headers.get(b"x-forwarded-for", b"").decode()
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = scope.get("client")
    if client is not None:
        return client[0]
    return "unknown"


async def _get_org_rate_limit(
    redis: aioredis.Redis,
    org_id: str,
) -> tuple[int, int]:
    """Retrieve rate-limit configuration for a given organization.

    Checks the org's quota config in Redis (set during org creation/update).
    Falls back to :data:`FALLBACK_ORG_RATE_LIMIT` if not configured.

    Args:
        redis: Async Redis client.
        org_id: The organization UUID string.

    Returns:
        Tuple of ``(max_requests, window_seconds)``.
    """
    try:
        quota_key = f"org:quota:{org_id}"
        quota_data = await redis.hgetall(quota_key)
        if quota_data:
            max_req = int(quota_data.get("rate_limit_max", FALLBACK_ORG_RATE_LIMIT))
            window = int(quota_data.get("rate_limit_window", FALLBACK_ORG_WINDOW))
            return max_req, window
    except Exception:
        logger.warning("Failed to read org rate-limit config", exc_info=True)

    return FALLBACK_ORG_RATE_LIMIT, FALLBACK_ORG_WINDOW


async def _check_rate_limit(
    redis: aioredis.Redis,
    key: str,
    max_requests: int,
    window_seconds: int,
) -> tuple[bool, int, int]:
    """Check and increment the sliding-window rate limit.

    Uses a Redis sorted set where each member is a unique timestamp (in
    milliseconds) and the score is that same timestamp.  Old entries outside
    the window are pruned with ``ZREMRANGEBYSCORE``.

    Args:
        redis: Async Redis client.
        key: Redis key for this rate-limit counter.
        max_requests: Maximum number of requests allowed in the window.
        window_seconds: Size of the sliding window in seconds.

    Returns:
        Tuple of ``(is_allowed, remaining, reset_time)``.
        - ``is_allowed``: ``True`` if the request should proceed.
        - ``remaining``: Number of requests remaining in this window.
        - ``reset_time``: Unix timestamp (seconds) when the window resets.
    """
    window_ms: int = window_seconds * 1000
    now_ms: int = int(time.time() * 1000)
    cutoff_ms: int = now_ms - window_ms
    now_s: float = time.time()

    pipe = redis.pipeline(transaction=True)

    pipe.zremrangebyscore(key, 0, cutoff_ms)
    pipe.zcard(key)
    pipe.zadd(key, {str(now_ms): now_ms})  # type: ignore[arg-type]
    pipe.expire(key, window_seconds + 5)

    results = await pipe.execute()

    current_count: int = results[1]

    is_allowed: bool = current_count <= max_requests
    remaining: int = max(0, max_requests - current_count)
    reset_time: int = int(now_s) + window_seconds

    return is_allowed, remaining, reset_time


# ═══════════════════════════════════════════════════════════════════════════════
# Rate-limit middleware
# ═══════════════════════════════════════════════════════════════════════════════


class RateLimitMiddleware:
    """Sliding-window rate limiting via Redis sorted sets — raw ASGI.

    This middleware depends on ``app.state.redis`` — an async Redis client
    initialised during the application lifespan.

    If the rate limit is exceeded the middleware responds with a 429 RFC 7807
    JSON body directly — it never calls the downstream app.
    """

    BYPASS_PATHS: set[str] = {"/health", "/ready"}

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._bypass_paths: set[str] = self.BYPASS_PATHS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # ── Bypass for critical infrastructure paths ────────────────────
        if path in self._bypass_paths:
            await self.app(scope, receive, send)
            return

        # ── Get Redis client (graceful degradation if unavailable) ──────
        app_state = scope.get("app", {}).state if hasattr(scope.get("app"), "state") else {}
        redis: aioredis.Redis | None = getattr(app_state, "redis", None) if not isinstance(app_state, dict) else None

        if redis is None:
            # Fallback: try scope["app"].state.redis (Starlette app state)
            _app = scope.get("app", None)
            redis = getattr(_app.state, "redis", None) if _app is not None else None

        if redis is None:
            logger.warning("Redis not available — rate limiting disabled")
            await self.app(scope, receive, send)
            return

        try:
            await redis.ping()
        except Exception:
            logger.warning("Redis unreachable — rate limiting disabled", exc_info=True)
            await self.app(scope, receive, send)
            return

        # ── Determine rate-limit key and parameters ─────────────────────
        state = scope.get("state") or {}
        org_id: str | None = state.get("org_id", None)

        if org_id:
            max_req, window = await _get_org_rate_limit(redis, org_id)
            rate_key = f"rate:api:{org_id}"
        else:
            client_ip = _get_client_ip(scope)
            max_req = settings.RATE_LIMIT_IP_MAX
            window = settings.RATE_LIMIT_WINDOW_SEC
            rate_key = f"rate:auth:{client_ip}"

        # ── Check sliding window ────────────────────────────────────────
        try:
            is_allowed, remaining, reset_time = await _check_rate_limit(
                redis=redis,
                key=rate_key,
                max_requests=max_req,
                window_seconds=window,
            )
        except Exception:
            logger.exception("Rate-limit check failed — allowing request")
            await self.app(scope, receive, send)
            return

        retry_after = max(1, reset_time - int(time.time()))

        if not is_allowed:
            # ── Respond with 429 directly ───────────────────────────────
            body = orjson.dumps({
                "type": RFC_7807_TYPE,
                "title": "Too Many Requests",
                "status": 429,
                "detail": (
                    f"You have exceeded the rate limit of {max_req} requests "
                    f"per window.  Retry after {retry_after} seconds."
                ),
                "instance": path,
            })
            headers = [
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(body)).encode()),
                (b"X-RateLimit-Limit", str(max_req).encode()),
                (b"X-RateLimit-Remaining", b"0"),
                (b"X-RateLimit-Reset", str(reset_time).encode()),
                (b"Retry-After", str(retry_after).encode()),
            ]
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": headers,
            })
            await send({
                "type": "http.response.body",
                "body": body,
            })
            return

        # ── Allowed — wrap send to add rate-limit headers ───────────────
        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers_list = list(message.get("headers", []))
                headers_list.extend([
                    (b"X-RateLimit-Limit", str(max_req).encode()),
                    (b"X-RateLimit-Remaining", str(remaining).encode()),
                    (b"X-RateLimit-Reset", str(reset_time).encode()),
                ])
                message["headers"] = headers_list
            await send(message)

        await self.app(scope, receive, send_wrapper)
