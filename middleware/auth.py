"""Authentication middleware supporting both API keys and JWT tokens.

Dual-mode authentication flow:

**API key mode** (for SDK clients):
1. Bearer token starts with ``oz_live_`` or ``oz_test_`` prefix.
2. Compute unsalted lookup hash via ``compute_lookup_hash``.
3. Check Redis cache at ``auth:key:{lookup_hash}`` (TTL: 300 s).
4. On miss, query ``api_keys`` table, verify salted hash.
5. Set ``request.state.org_id``, ``request.state.api_key_scopes``.
6. Set PostgreSQL RLS context.

**JWT mode** (for dashboard users):
1. Bearer token is a three-segment JWT (starts with ``eyJ``).
2. Verify signature with ``OZ_SECRET_KEY`` (HS256).
3. Extract ``sub`` (user_id), ``org_id``, ``role`` claims.
4. Set ``request.state.org_id``, ``request.state.user_id``,
   ``request.state.role``, ``request.state.auth_type = "jwt"``.

Public endpoints (``/health``, ``/docs``, ``/v1/auth/*``, etc.) pass
through without authentication.

RFC 7807 error bodies are returned for all 401/403 responses.
"""

from __future__ import annotations

import orjson
import logging
from typing import Any, cast

import redis.asyncio as aioredis
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from repositories.api_key_repository import ApiKeyRepository
from utils.crypto import compute_lookup_hash, verify_api_key

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

AUTH_CACHE_PREFIX: str = "auth:key:"
"""Redis key prefix for cached API key authentication data."""

AUTH_CACHE_TTL: int = 300
"""TTL in seconds for cached auth lookups (5 minutes)."""

AUTH_NEG_CACHE_PREFIX: str = "auth:neg:"
"""Redis key prefix for negative cache entries (key not found in DB)."""

AUTH_NEG_CACHE_TTL: int = 60
"""TTL in seconds for negative cache entries (1 minute)."""

AUTH_MISS_RATE_LIMIT_PREFIX: str = "auth:miss_ip:"
"""Redis key prefix for per-IP auth miss-rate counters."""

AUTH_MISS_RATE_LIMIT: int = 10
"""Maximum DB auth misses per IP per window before throttling."""

AUTH_MISS_RATE_WINDOW: int = 60
"""Sliding window in seconds for per-IP miss-rate limiting."""

SCHEMA_AUTH_HEADER: str = "Bearer"
"""Expected Authorization header scheme."""


# ═══════════════════════════════════════════════════════════════════════════════
# Public endpoint marker
# ═══════════════════════════════════════════════════════════════════════════════

PUBLIC_ENDPOINTS: set[str] = {
    "/health",
    "/ready",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/admin/organizations",
    "/admin/org/config/defaults",
    "/v1/auth/signup",
    "/v1/auth/login",
    "/v1/auth/refresh",
    "/v1/auth/verify-email",
    "/v1/auth/resend-otp",
    "/v1/auth/forgot-password",
    "/v1/auth/reset-password",
}
"""Paths that are allowed without authentication.

The ``/metrics`` path is handled by an exact-path exemption in
:meth:`AuthMiddleware.dispatch` — it must be unauthenticated so
Prometheus scrapers can reach it, but sub-paths (``/metrics/summary``
etc.) go through normal auth.

These endpoints do not require an ``Authorization`` header.  The set may be
extended at the application level.  Paths are matched suffix-wise so that
versioned routes (e.g. ``/v1/health``) are also recognised.
"""

# ═══════════════════════════════════════════════════════════════════════════════
# JWT constants
# ═══════════════════════════════════════════════════════════════════════════════

API_KEY_PREFIXES: tuple[str, ...] = ("oz_live_", "oz_test_")
"""Recognised API key prefixes.  Tokens not starting with one of these
are attempted as JWT first."""


# ═══════════════════════════════════════════════════════════════════════════════
# Helper functions
# ═══════════════════════════════════════════════════════════════════════════════


def _is_public_path(path: str) -> bool:
    """Check if a path is in the public endpoints allowlist.

    Performs both exact and prefix matching (e.g. ``/docs`` matches any
    sub-path under ``/docs/``).

    Args:
        path: The URL path from the request.

    Returns:
        ``True`` if the path is publicly accessible.
    """
    return any(
        path == endpoint or path.endswith(endpoint) or path.startswith(f"{endpoint}/")
        for endpoint in PUBLIC_ENDPOINTS
    )


def _rfc7807_response(
    status: int,
    title: str,
    detail: str,
    path: str,
    **extra: Any,
) -> JSONResponse:
    """Build an RFC 7807 Problem Details response.

    Args:
        status: HTTP status code.
        title: Human-readable title for the error type.
        detail: Detailed explanation of the error.
        path: The request URL path (used as ``instance``).
        **extra: Additional fields to include in the response body.

    Returns:
        A :class:`JSONResponse` with RFC 7807 structure.
    """
    return JSONResponse(
        status_code=status,
        content={
            "type": f"https://errors.openzync.tech/{title.lower().replace(' ', '_')}",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": path,
            **extra,
        },
    )


# ── ASGI response sender ──────────────────────────────────────────────────
# These replace the ``JSONResponse``-based helpers when used from raw ASGI
# middleware (AuthMiddleware).  They send directly via the ASGI ``send``
# channel instead of returning a response object.


async def _send_rfc7807(
    send: Send,
    status: int,
    title: str,
    detail: str,
    path: str,
    **extra: Any,
) -> None:
    """Send an RFC 7807 Problem Details response via ASGI.

    Args:
        send: The ASGI ``send`` callable.
        status: HTTP status code.
        title: Human-readable title for the error type.
        detail: Detailed explanation of the error.
        path: The request URL path (used as ``instance``).
        **extra: Additional fields to include in the response body.
    """
    body = orjson.dumps(
        {
            "type": f"https://errors.openzync.tech/{title.lower().replace(' ', '_')}",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": path,
            **extra,
        },
    )
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(body)).encode()),
            ],
        },
    )
    await send(
        {
            "type": "http.response.body",
            "body": body,
        },
    )


async def _lookup_key_in_redis(
    redis: aioredis.Redis,
    lookup_hash: str,
) -> dict[str, Any] | None:
    """Look up cached auth data by lookup hash.

    Args:
        redis: Async Redis client.
        lookup_hash: Unsalted SHA-256 hex digest of the API key.

    Returns:
        Deserialized cached data dict, or ``None`` if not found.
    """
    cache_key = f"{AUTH_CACHE_PREFIX}{lookup_hash}"
    cached = await redis.get(cache_key)
    if cached is not None:
        try:
            # ⚠️: Data is stored as JSON string.  type: ignore is safe
            # because we wrote it ourselves.
            return cast(dict[str, Any], orjson.loads(cached.encode()))
        except (orjson.JSONDecodeError, TypeError):
            logger.warning("Corrupted auth cache entry, ignoring", key=cache_key)
            await redis.delete(cache_key)
    return None


async def _cache_key_in_redis(
    redis: aioredis.Redis,
    lookup_hash: str,
    data: dict[str, Any],
    ttl: int = AUTH_CACHE_TTL,
) -> None:
    """Cache auth data in Redis.

    Args:
        redis: Async Redis client.
        lookup_hash: Unsalted SHA-256 hex digest of the API key.
        data: Auth data dict (``org_id``, ``scopes``) to cache.
        ttl: TTL in seconds (default: 300).
    """
    cache_key = f"{AUTH_CACHE_PREFIX}{lookup_hash}"
    await redis.setex(cache_key, ttl, orjson.dumps(data))


# ── Negative cache + auth miss-rate limiting ─────────────────────────────


async def _check_negative_cache(
    redis: aioredis.Redis,
    lookup_hash: str,
) -> bool:
    """Check whether a lookup hash was recently found absent from the DB.

    Args:
        redis: Async Redis client.
        lookup_hash: Unsalted SHA-256 hex digest of the API key.

    Returns:
        ``True`` if the key is known to not exist (cache hit on absence).
    """
    return bool(await redis.exists(f"{AUTH_NEG_CACHE_PREFIX}{lookup_hash}"))


async def _mark_negative_cache(
    redis: aioredis.Redis,
    lookup_hash: str,
) -> None:
    """Record that a lookup hash does not correspond to a valid API key.

    Subsequent lookups for the same hash will be rejected without a DB
    query for ``AUTH_NEG_CACHE_TTL`` seconds.

    Args:
        redis: Async Redis client.
        lookup_hash: Unsalted SHA-256 hex digest of the API key.
    """
    await redis.setex(
        f"{AUTH_NEG_CACHE_PREFIX}{lookup_hash}",
        AUTH_NEG_CACHE_TTL,
        "1",
    )


async def _check_auth_miss_rate_limit(
    redis: aioredis.Redis,
    client_ip: str,
) -> None:
    """Check whether an IP address has exceeded the auth miss-rate limit.

    Each DB cache-miss increments a per-IP counter.  If the counter
    exceeds ``AUTH_MISS_RATE_LIMIT`` within the sliding window, a
    :class:`RateLimitError` is raised so the caller can return 429.

    Args:
        redis: Async Redis client.
        client_ip: The client's IP address.

    Raises:
        RateLimitError: If the IP has exceeded the allowed miss rate.
    """
    key = f"{AUTH_MISS_RATE_LIMIT_PREFIX}{client_ip}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, AUTH_MISS_RATE_WINDOW)
    if count > AUTH_MISS_RATE_LIMIT:
        from core.exceptions import RateLimitError

        raise RateLimitError(
            f"Too many authentication attempts from {client_ip}. "
            f"Try again later."
        )


async def _query_key_from_db(
    db_factory: async_sessionmaker[AsyncSession],
    lookup_hash: str,
) -> dict[str, Any] | None:
    """Query the database for an API key by its lookup hash via ApiKeyRepository.

    Args:
        db_factory: Async session factory from ``request.app.state``.
        lookup_hash: Unsalted SHA-256 hex digest of the API key.

    Returns:
        Dict with ``id``, ``org_id``, ``scopes``, ``key_hash``, ``salt``,
        ``is_revoked``, ``expires_at`` if found, or ``None``.
    """
    async with db_factory() as session:
        async with session.begin():
            repo = ApiKeyRepository(session)
            api_key = await repo.get_by_lookup_hash(lookup_hash)

    if api_key is None:
        return None

    return {
        "id": str(api_key.id),
        "org_id": str(api_key.organization_id),
        "project_id": str(api_key.project_id) if api_key.project_id else None,
        "created_by": str(api_key.created_by) if api_key.created_by else None,
        "scopes": list(api_key.scopes),
        "key_hash": api_key.key_hash,
        "salt": api_key.salt,
        "is_revoked": api_key.is_revoked,
        "expires_at": api_key.expires_at,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# JWT helper
# ═══════════════════════════════════════════════════════════════════════════════


def _is_jwt_token(token: str) -> bool:
    """Check if a bearer token looks like a JWT (three base64url segments).

    JWT tokens have exactly two dots separating three URL-safe base64
    segments.  This is a cheap heuristic check before attempting
    cryptographic verification.

    Args:
        token: The raw bearer token string.

    Returns:
        ``True`` if the token has two dots (likely a JWT).
    """
    return token.count(".") == 2


def _verify_jwt_and_set_state(
    state: dict[str, Any],
    path: str,
    token: str,
) -> dict | None:
    """Verify a JWT token and populate the request state dict.

    Extracts ``sub`` (user_id), ``org_id``, and ``role`` from the JWT
    claims and writes them into ``state``.

    Args:
        state: Mutable request state dict (populated on success).
        path: The request URL path (used in error responses).
        token: The raw JWT string.

    Returns:
        ``None`` on success, or an error dict (status, title, detail, path)
        suitable for passing to ``_send_rfc7807`` on failure.
    """
    from core.config import settings
    from utils.crypto import verify_jwt_token

    try:
        payload = verify_jwt_token(token, settings.SECRET_KEY)
    except Exception as exc:
        logger.warning("JWT verification failed", exc_info=exc)
        return {
            "status": 401,
            "title": "Invalid Token",
            "detail": "The JWT token is invalid or expired.",
            "path": path,
        }

    # Validate required claims
    user_id: str | None = payload.get("sub")
    org_id: str | None = payload.get("org_id")
    role: str | None = payload.get("role", "member")
    token_type: str | None = payload.get("type")

    if token_type != "access":
        return {
            "status": 401,
            "title": "Invalid Token Type",
            "detail": "Only access tokens are accepted for API authentication.",
            "path": path,
        }

    if not user_id or not org_id:
        return {
            "status": 401,
            "title": "Invalid Token Claims",
            "detail": "JWT must contain 'sub' (user_id) and 'org_id' claims.",
            "path": path,
        }

    state["auth_type"] = "jwt"
    state["org_id"] = org_id
    state["user_id"] = user_id
    state["role"] = role
    state["api_key_scopes"] = ["read", "write", "admin"]
    # JWT users get full scopes — fine-grained RBAC can be added later.

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Auth middleware
# ═══════════════════════════════════════════════════════════════════════════════


class AuthMiddleware:
    """Raw ASGI auth middleware — no ``BaseHTTPMiddleware`` overhead.

    Authenticates requests via ``Authorization: Bearer <api_key|jwt>``.
    Sets ``scope["state"]`` for downstream middleware and route handlers.

    Supports two authentication methods:

    - **API key** (SDK clients): Identified by ``oz_live_`` / ``oz_test_``
      prefix.  Validated against the ``api_keys`` table with Redis caching.
    - **JWT** (dashboard users):  Identified by a three-segment JWT string.
      Validated with ``OZ_SECRET_KEY`` via HS256.

    This middleware reads:
    - ``scope["app"].state.redis`` — an ``aioredis.Redis`` client.
    - ``scope["app"].state.db_session_factory`` — an ``async_sessionmaker``.

    It sets the following keys on ``scope["state"]``:
    - ``org_id`` — the authenticated organization's UUID (or ``None``).
    - ``user_id`` — the authenticated user's UUID (JWT only; ``None`` for API key).
    - ``role`` — user role string (JWT only; ``None`` for API key).
    - ``auth_type`` — ``"jwt"`` or ``"api_key"``.
    - ``api_key_scopes`` — list of permission strings (or ``[]``).
    - ``api_key_project_id`` — optional project UUID string this API key is
      restricted to (``None`` means org-wide access).
    """

    def __init__(self, app: ASGIApp) -> None:
        """Initialise the middleware.

        Args:
            app: The ASGI application.
        """
        self.app = app
        self._public_endpoints: set[str] = PUBLIC_ENDPOINTS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate the request and set ``scope["state"]``.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive callable.
            send: The ASGI send callable.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # ── Initialise state defaults ────────────────────────────────────
        scope["state"] = {
            "org_id": None,
            "user_id": None,
            "role": None,
            "auth_type": None,
            "api_key_scopes": [],
            "api_key_project_id": None,
        }

        # ── Extract request metadata from scope ──────────────────────────
        headers: dict[str, str] = {
            k.decode("ascii").lower(): v.decode("ascii")
            for k, v in scope.get("headers", [])
        }
        path: str = scope.get("path", "/")
        method: str = scope.get("method", "GET")

        # ── CORS preflight pass-through ──────────────────────────────────
        # Browsers never send Authorization on OPTIONS preflight, so we
        # must let them through regardless of path. The CORSMiddleware
        # (registered outermost) should catch these first, but this is a
        # defense-in-depth guard in case middleware ordering changes.
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # ── Prometheus scrape endpoint — exact path only ─────────────────
        # /metrics must be unauthenticated for Prometheus scrapers, but
        # sub-paths like /metrics/summary go through normal auth.
        if path == "/metrics":
            await self.app(scope, receive, send)
            return

        # ── Public endpoints pass through ────────────────────────────────
        if _is_public_path(path):
            await self.app(scope, receive, send)
            return

        # ── Extract Authorization header ─────────────────────────────────
        auth_header: str | None = headers.get("authorization")
        if auth_header is None:
            await _send_rfc7807(
                send,
                status=401,
                title="Authentication Required",
                detail="Missing Authorization header. Use: Bearer <api_key or jwt>",
                path=path,
            )
            return

        if not auth_header.startswith(SCHEMA_AUTH_HEADER):
            await _send_rfc7807(
                send,
                status=401,
                title="Invalid Authorization Scheme",
                detail=(
                    f"Authorization header must start with '{SCHEMA_AUTH_HEADER}'. "
                    f"Got: {auth_header.split()[0] if ' ' in auth_header else 'empty'}"
                ),
                path=path,
            )
            return

        raw_key: str = auth_header[len(SCHEMA_AUTH_HEADER) :].strip()
        if not raw_key:
            await _send_rfc7807(
                send,
                status=401,
                title="Empty Credentials",
                detail="Authorization header is empty after 'Bearer '.",
                path=path,
            )
            return

        # ── Route to JWT or API key flow ─────────────────────────────────
        # JWT tokens have 2 dots.  API keys have a known prefix.
        # If the token doesn't have an API key prefix, try JWT first.
        if not raw_key.startswith(API_KEY_PREFIXES) and _is_jwt_token(raw_key):
            jwt_error = _verify_jwt_and_set_state(
                scope["state"], path, raw_key,
            )
            if jwt_error is not None:
                await _send_rfc7807(send, **jwt_error)
                return
            await self.app(scope, receive, send)
            return

        # ── API key flow ─────────────────────────────────────────────────
        lookup_hash: str = compute_lookup_hash(raw_key)

        # ── Access app state from scope ──────────────────────────────────
        app_state = scope.get("app")
        app_state_obj = getattr(app_state, "state", None) if app_state else None
        redis: aioredis.Redis | None = (
            getattr(app_state_obj, "redis", None) if app_state_obj else None
        )
        db_factory: async_sessionmaker[AsyncSession] | None = (
            getattr(app_state_obj, "db_session_factory", None)
            if app_state_obj
            else None
        )

        # ── Check Redis cache ────────────────────────────────────────────
        if redis is not None:
            try:
                cached = await _lookup_key_in_redis(redis, lookup_hash)
                if cached is not None:
                    scope["state"]["auth_type"] = "api_key"
                    scope["state"]["org_id"] = cached["org_id"]
                    scope["state"]["user_id"] = cached.get("created_by")
                    scope["state"]["api_key_scopes"] = cached["scopes"]
                    scope["state"]["api_key_project_id"] = cached.get("project_id")
                    await self.app(scope, receive, send)
                    return
            except Exception:
                logger.error("auth.cache_lookup_failed", exc_info=True)
                raise  # Let the exception propagate — auth layer depends on Redis

        # ── Negative cache check ──────────────────────────────────────────
        # If this lookup_hash was recently looked up and not found in DB,
        # reject immediately without hitting the database.
        if redis is not None:
            try:
                if await _check_negative_cache(redis, lookup_hash):
                    await _send_rfc7807(
                        send,
                        status=401,
                        title="Invalid API Key",
                        detail="The provided API key is not valid.",
                        path=path,
                    )
                    return
            except Exception:
                logger.error("auth.negative_cache_check_failed", exc_info=True)
                # Non-critical — proceed without negative cache

        # ── Per-IP auth miss-rate limit ───────────────────────────────────
        # Prevent a single IP from hammering the DB with many unique
        # (likely rotated) API keys per minute.
        if redis is not None:
            client_raw = scope.get("client")
            client_ip: str = client_raw[0] if client_raw is not None else "unknown"
            try:
                await _check_auth_miss_rate_limit(redis, client_ip)
            except Exception as exc:
                if "Too many authentication attempts" in str(exc):
                    # This is a legitimate rate-limit response, handle it
                    await _send_rfc7807(
                        send,
                        status=429,
                        title="Too Many Requests",
                        detail="Too many authentication attempts. Try again later.",
                        path=path,
                    )
                    return
                logger.error("auth.auth_miss_rate_check_failed", exc_info=True)
                # Non-critical — proceed without miss-rate limiting

        # ── Check DB ─────────────────────────────────────────────────────
        if db_factory is None:
            logger.error(
                "db_session_factory not available on app.state — "
                "AuthMiddleware cannot query API keys."
            )
            await _send_rfc7807(
                send,
                status=500,
                title="Internal Server Error",
                detail="Authentication service is misconfigured.",
                path=path,
            )
            return

        try:
            key_data = await _query_key_from_db(db_factory, lookup_hash)
        except Exception:
            logger.exception("Failed to query API key from database")
            await _send_rfc7807(
                send,
                status=500,
                title="Internal Server Error",
                detail="Authentication service temporarily unavailable.",
                path=path,
            )
            return

        if key_data is None:
            # Record this miss in the negative cache so subsequent requests
            # with the same key are rejected without a DB round-trip.
            if redis is not None:
                try:
                    await _mark_negative_cache(redis, lookup_hash)
                except Exception:
                    logger.error("auth.negative_cache_mark_failed", exc_info=True)
            await _send_rfc7807(
                send,
                status=401,
                title="Invalid API Key",
                detail="The provided API key is not valid.",
                path=path,
            )
            return

        # ── Verify against salted hash ───────────────────────────────────
        if not verify_api_key(raw_key, key_data["key_hash"], key_data["salt"]):
            await _send_rfc7807(
                send,
                status=401,
                title="Invalid API Key",
                detail="The provided API key is not valid.",
                path=path,
            )
            return

        # ── Validate key state ───────────────────────────────────────────
        if key_data["is_revoked"]:
            await _send_rfc7807(
                send,
                status=401,
                title="API Key Revoked",
                detail="This API key has been revoked.",
                path=path,
            )
            return

        if key_data.get("expires_at") is not None:
            from datetime import datetime, timezone

            expires = key_data["expires_at"]
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires < datetime.now(timezone.utc):
                await _send_rfc7807(
                    send,
                    status=401,
                    title="API Key Expired",
                    detail="This API key has expired.",
                    path=path,
                )
                return

        # ── Set request state ────────────────────────────────────────────
        org_id_val: str = key_data["org_id"]
        scopes: list[str] = key_data["scopes"]
        created_by: str | None = key_data.get("created_by")

        scope["state"]["auth_type"] = "api_key"
        scope["state"]["org_id"] = org_id_val
        scope["state"]["user_id"] = created_by  # None if key has no creator
        scope["state"]["api_key_scopes"] = scopes
        scope["state"]["api_key_project_id"] = key_data.get("project_id")

        # ═ Update last_used timestamp (fire-and-forget) ═══════════════════
        try:
            async with db_factory() as session:
                await ApiKeyRepository(session).update_last_used(UUID(key_data["id"]))
                await session.commit()
        except Exception:
            logger.error("auth.last_used_update_failed", exc_info=True)

        # ── Cache in Redis (fire-and-forget) ─────────────────────────────
        if redis is not None:
            try:
                await _cache_key_in_redis(
                    redis,
                    lookup_hash,
                    {
                        "org_id": org_id_val,
                        "scopes": scopes,
                        "project_id": key_data.get("project_id"),
                        "created_by": created_by,
                    },
                )
            except Exception:
                logger.error("auth.cache_write_failed", exc_info=True)

        # ── RLS context is set in dependencies/db.py on the actual request
        # session.  Do NOT set it here — _set_rls_context opens its own
        # short-lived session and PostgreSQL's set_config is session-local,
        # so the config has zero effect on the request handler's session.

        await self.app(scope, receive, send)
