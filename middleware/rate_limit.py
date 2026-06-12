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

import logging
import time
from typing import Any

import redis.asyncio as aioredis
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

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


def _get_client_ip(request: Request) -> str:
    """Extract the originating client IP from request headers.

    Respects ``X-Forwarded-For`` when behind a reverse proxy, falling back
    to the direct client address.

    Args:
        request: The incoming HTTP request.

    Returns:
        The client IP address as a string.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # X-Forwarded-For can be a comma-separated list; the leftmost is the
        # original client.
        return forwarded.split(",")[0].strip()
    # Fallback to the direct connection address.
    client = request.client
    if client is not None:
        return client.host
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
    # note: Org rate limits could be cached from the organizations
    # table.  For now we use a Redis hash per org that is set during org
    # provisioning.  This avoids a DB query on every request.
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
    # Convert to seconds for the Retry-After header.
    now_s: float = time.time()

    pipe = redis.pipeline(transaction=True)

    # Step 1: Remove entries outside the sliding window.
    pipe.zremrangebyscore(key, 0, cutoff_ms)

    # Step 2: Count entries remaining in the window.
    pipe.zcard(key)

    # Step 3: Add the current request's timestamp.
    pipe.zadd(key, {str(now_ms): now_ms})  # type: ignore[arg-type]

    # Step 4: Set TTL on the key to avoid stale data accumulation.
    pipe.expire(key, window_seconds + 5)  # slight buffer

    # Execute pipeline.
    results = await pipe.execute()

    # Results order matches the pipeline commands.
    _removed_count: Any = results[0]  # noqa: F841 — ZREMRANGEBYSCORE result
    current_count: int = results[1]  # ZCARD result
    _added_count: Any = results[2]  # noqa: F841 — ZADD result
    _expire_set: Any = results[3]  # noqa: F841 — EXPIRE result

    is_allowed: bool = current_count <= max_requests
    remaining: int = max(0, max_requests - current_count)
    # Reset time is the end of the current window.
    reset_time: int = int(now_s) + window_seconds

    return is_allowed, remaining, reset_time


# ═══════════════════════════════════════════════════════════════════════════════
# RFC 7807 429 response
# ═══════════════════════════════════════════════════════════════════════════════


def _rate_limit_response(
    retry_after: int,
    path: str,
    limit: int,
    remaining: int,
    reset: int,
) -> JSONResponse:
    """Build an RFC 7807 429 Too Many Requests response.

    Args:
        retry_after: Seconds the client should wait before retrying.
        path: The request URL path.
        limit: The rate limit ceiling for this endpoint.
        remaining: Requests remaining in the current window (0 when exceeded).
        reset: Unix timestamp when the window resets.

    Returns:
        A :class:`JSONResponse` with 429 status and rate-limit headers.
    """
    return JSONResponse(
        status_code=429,
        headers={
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset),
            "Retry-After": str(retry_after),
        },
        content={
            "type": RFC_7807_TYPE,
            "title": "Too Many Requests",
            "status": 429,
            "detail": (
                f"You have exceeded the rate limit of {limit} requests "
                f"per window.  Retry after {retry_after} seconds."
            ),
            "instance": path,
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Rate-limit middleware
# ═══════════════════════════════════════════════════════════════════════════════


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limiting via Redis sorted sets.

    This middleware depends on ``request.app.state.redis`` — an async Redis
    client initialised during the application lifespan.

    Registration order (in ``main.py``):

        app.add_middleware(RequestIDMiddleware)    # 1st
        app.add_middleware(LoggingMiddleware)      # 2nd
        app.add_middleware(AuthMiddleware)         # 3rd
        app.add_middleware(RateLimitMiddleware)    # 4th — uses request.state.org_id
        app.add_middleware(TracingMiddleware)      # 5th
    """

    # ╠ Public paths that bypass rate limiting completely.
    BYPASS_PATHS: set[str] = {"/health", "/ready"}

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Enforce rate limits before passing the request downstream.

        Args:
            request: Incoming HTTP request.
            call_next: The next middleware or route handler.

        Returns:
            HTTP response — possibly a 429 if the rate limit is exceeded.
        """
        # ── Bypass for critical infrastructure paths ────────────────────
        if request.url.path in self.BYPASS_PATHS:
            return await call_next(request)

        # ── Get Redis client (graceful degradation if unavailable) ──────
        redis: aioredis.Redis | None = getattr(request.app.state, "redis", None)
        if redis is None:
            logger.warning(
                "Redis not available — rate limiting disabled for this request"
            )
            return await call_next(request)

        try:
            await redis.ping()
        except Exception:
            logger.warning(
                "Redis unreachable — rate limiting disabled for this request",
                exc_info=True,
            )
            return await call_next(request)

        # ── Determine rate-limit key and parameters ─────────────────────
        org_id: str | None = getattr(request.state, "org_id", None)

        if org_id:
            # Authenticated request — use org-based rate limit.
            max_req, window = await _get_org_rate_limit(redis, org_id)
            rate_key = f"rate:api:{org_id}"
        else:
            # Unauthenticated request — use IP-based rate limit.
            client_ip = _get_client_ip(request)
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
            return await call_next(request)

        # ── Attach rate-limit headers (even on allowed requests) ────────
        retry_after = max(1, reset_time - int(time.time()))

        if not is_allowed:
            return _rate_limit_response(
                retry_after=retry_after,
                path=request.url.path,
                limit=max_req,
                remaining=0,
                reset=reset_time,
            )

        # Allowed — pass through and add rate-limit headers to response.
        # ⚠️ HEADERS INJECTION: We set headers on the response object
        # returned by call_next.  This works because Response headers are
        # mutable (a MutableHeaders object).
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(max_req)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_time)
        return response
