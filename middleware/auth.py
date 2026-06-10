"""Authentication middleware supporting both API keys and JWT tokens.

Dual-mode authentication flow:

**API key mode** (for SDK clients):
1. Bearer token starts with ``mg_live_`` or ``mg_test_`` prefix.
2. Compute unsalted lookup hash via ``compute_lookup_hash``.
3. Check Redis cache at ``auth:key:{lookup_hash}`` (TTL: 300 s).
4. On miss, query ``api_keys`` table, verify salted hash.
5. Set ``request.state.org_id``, ``request.state.api_key_scopes``.
6. Set PostgreSQL RLS context.

**JWT mode** (for dashboard users):
1. Bearer token is a three-segment JWT (starts with ``eyJ``).
2. Verify signature with ``MG_SECRET_KEY`` (HS256).
3. Extract ``sub`` (user_id), ``org_id``, ``role`` claims.
4. Set ``request.state.org_id``, ``request.state.user_id``,
   ``request.state.role``, ``request.state.auth_type = "jwt"``.

Public endpoints (``/health``, ``/docs``, ``/v1/auth/*``, etc.) pass
through without authentication.

RFC 7807 error bodies are returned for all 401/403 responses.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import redis.asyncio as aioredis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from utils.crypto import compute_lookup_hash, verify_api_key

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

AUTH_CACHE_PREFIX: str = "auth:key:"
"""Redis key prefix for cached API key authentication data."""

AUTH_CACHE_TTL: int = 300
"""TTL in seconds for cached auth lookups (5 minutes)."""

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
    "/v1/auth/signup",
    "/v1/auth/login",
    "/v1/auth/refresh",
}
"""Paths that are allowed without authentication.

These endpoints do not require an ``Authorization`` header.  The set may be
extended at the application level.  Paths are matched suffix-wise so that
versioned routes (e.g. ``/v1/health``) are also recognised.
"""

# ═══════════════════════════════════════════════════════════════════════════════
# JWT constants
# ═══════════════════════════════════════════════════════════════════════════════

API_KEY_PREFIXES: tuple[str, ...] = ("mg_live_", "mg_test_")
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
            "type": f"https://errors.memgraph.dev/{title.lower().replace(' ', '_')}",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": path,
            **extra,
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
            return cast(dict[str, Any], json.loads(cached))
        except (json.JSONDecodeError, TypeError):
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
    await redis.setex(cache_key, ttl, json.dumps(data))


async def _query_key_from_db(
    db_factory: async_sessionmaker[AsyncSession],
    lookup_hash: str,
) -> dict[str, Any] | None:
    """Query the database for an API key by its lookup hash.

    Args:
        db_factory: Async session factory from ``request.app.state``.
        lookup_hash: Unsalted SHA-256 hex digest of the API key.

    Returns:
        Dict with ``org_id``, ``scopes``, ``key_hash``, ``salt``, ``is_revoked``,
        ``expires_at`` if found, or ``None``.
    """
    # Late import to avoid circular dependency; the model is expected to exist.
    try:
        from models.api_key import ApiKey
    except ImportError as err:
        logger.critical(
            "ApiKey model not available — cannot authenticate. "
            "Ensure models/api_key.py exists.",
            exc_info=True,
        )
        raise RuntimeError(
            "ApiKey model is required for authentication middleware."
        ) from err

    async with db_factory() as session:
        async with session.begin():
            result = await session.execute(
                select(ApiKey).where(ApiKey.lookup_hash == lookup_hash)  # type: ignore[attr-defined]
            )
            api_key = result.scalar_one_or_none()

    if api_key is None:
        return None

    return {
        "org_id": str(api_key.organization_id),  # type: ignore[attr-defined]
        "scopes": list(api_key.scopes),  # type: ignore[attr-defined]
        "key_hash": api_key.key_hash,  # type: ignore[attr-defined]
        "salt": api_key.salt,  # type: ignore[attr-defined]
        "is_revoked": api_key.is_revoked,  # type: ignore[attr-defined]
        "expires_at": api_key.expires_at,  # type: ignore[attr-defined]
    }


async def _set_rls_context(
    db_factory: async_sessionmaker[AsyncSession],
    org_id: str | None,
    bypass_rls: bool = False,
) -> None:
    """Set PostgreSQL session-level configuration for RLS policies.

    This must be called within a DB session so the ``SET_CONFIG`` takes effect
    for the lifetime of that backend connection.

    Args:
        db_factory: Async session factory.
        org_id: The authenticated organization ID, or ``"none"``.
        bypass_rls: Whether to bypass RLS (e.g., for service accounts).
    """
    async with db_factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.org_id', :org_id, true)"),
                {"org_id": org_id or "none"},
            )
            await session.execute(
                text("SELECT set_config('app.bypass_rls', :bypass, true)"),
                {"bypass": "true" if bypass_rls else "false"},
            )


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
    request: Request,
    token: str,
) -> JSONResponse | None:
    """Verify a JWT token and set ``request.state``.

    Extracts ``sub`` (user_id), ``org_id``, and ``role`` from the JWT
    claims and populates ``request.state`` accordingly.

    Args:
        request: The incoming HTTP request (state is mutated in-place).
        token: The raw JWT string.

    Returns:
        ``None`` on success, or an RFC 7807 ``JSONResponse`` on failure.
    """
    from core.config import settings
    from utils.crypto import verify_jwt_token

    try:
        payload = verify_jwt_token(token, settings.SECRET_KEY)
    except Exception as exc:
        logger.warning("JWT verification failed", exc_info=exc)
        return _rfc7807_response(
            status=401,
            title="Invalid Token",
            detail="The JWT token is invalid or expired.",
            path=request.url.path,
        )

    # Validate required claims
    user_id: str | None = payload.get("sub")
    org_id: str | None = payload.get("org_id")
    role: str | None = payload.get("role", "member")
    token_type: str | None = payload.get("type")

    if token_type != "access":
        return _rfc7807_response(
            status=401,
            title="Invalid Token Type",
            detail="Only access tokens are accepted for API authentication.",
            path=request.url.path,
        )

    if not user_id or not org_id:
        return _rfc7807_response(
            status=401,
            title="Invalid Token Claims",
            detail="JWT must contain 'sub' (user_id) and 'org_id' claims.",
            path=request.url.path,
        )

    request.state.auth_type = "jwt"  # type: ignore[attr-defined]
    request.state.org_id = org_id  # type: ignore[attr-defined]
    request.state.user_id = user_id  # type: ignore[attr-defined]
    request.state.role = role  # type: ignore[attr-defined]
    request.state.api_key_scopes = ["read", "write", "admin"]  # type: ignore[attr-defined]
    # JWT users get full scopes — fine-grained RBAC can be added later.

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Auth middleware
# ═══════════════════════════════════════════════════════════════════════════════


class AuthMiddleware(BaseHTTPMiddleware):
    """Authenticate requests via ``Authorization: Bearer <api_key|jwt>``.

    Supports two authentication methods:

    - **API key** (SDK clients): Identified by ``mg_live_`` / ``mg_test_``
      prefix.  Validated against the ``api_keys`` table with Redis caching.
    - **JWT** (dashboard users):  Identified by a three-segment JWT string.
      Validated with ``MG_SECRET_KEY`` via HS256.

    This middleware depends on:
    - ``request.app.state.redis`` — an ``aioredis.Redis`` client initialised
      during the application lifespan.
    - ``request.app.state.db_session_factory`` — an ``async_sessionmaker``
      bound to the application engine.

    It sets the following attributes on ``request.state``:
    - ``org_id`` — the authenticated organization's UUID (or ``None``).
    - ``user_id`` — the authenticated user's UUID (JWT only; ``None`` for API key).
    - ``role`` — user role string (JWT only; empty for API key).
    - ``auth_type`` — ``"jwt"`` or ``"api_key"``.
    - ``api_key_scopes`` — list of permission strings (or ``[]``).
    """

    def __init__(self, app: Any, **kwargs: Any) -> None:
        """Initialise the middleware.

        Args:
            app: The ASGI application.
            **kwargs: Additional arguments for ``BaseHTTPMiddleware``.
        """
        super().__init__(app, **kwargs)
        self._public_endpoints: set[str] = PUBLIC_ENDPOINTS

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Authenticate the request and set ``request.state``.

        Args:
            request: Incoming HTTP request.
            call_next: The next middleware or route handler.

        Returns:
            HTTP response — possibly an RFC 7807 401 if auth fails.
        """
        # ── Initialise state defaults ────────────────────────────────────
        request.state.org_id = None  # type: ignore[attr-defined]
        request.state.user_id = None  # type: ignore[attr-defined]
        request.state.role = None  # type: ignore[attr-defined]
        request.state.auth_type = None  # type: ignore[attr-defined]
        request.state.api_key_scopes = []  # type: ignore[attr-defined]

        # ── CORS preflight pass-through ──────────────────────────────────
        # Browsers never send Authorization on OPTIONS preflight, so we
        # must let them through regardless of path. The CORSMiddleware
        # (registered outermost) should catch these first, but this is a
        # defense-in-depth guard in case middleware ordering changes.
        if request.method == "OPTIONS":
            return await call_next(request)

        # ── Public endpoints pass through ────────────────────────────────
        if _is_public_path(request.url.path):
            return await call_next(request)

        # ── Extract Authorization header ─────────────────────────────────
        auth_header: str | None = request.headers.get("Authorization")
        if auth_header is None:
            return _rfc7807_response(
                status=401,
                title="Authentication Required",
                detail="Missing Authorization header. Use: Bearer <api_key or jwt>",
                path=request.url.path,
            )

        if not auth_header.startswith(SCHEMA_AUTH_HEADER):
            return _rfc7807_response(
                status=401,
                title="Invalid Authorization Scheme",
                detail=(
                    f"Authorization header must start with '{SCHEMA_AUTH_HEADER}'. "
                    f"Got: {auth_header.split()[0] if ' ' in auth_header else 'empty'}"
                ),
                path=request.url.path,
            )

        raw_key: str = auth_header[len(SCHEMA_AUTH_HEADER) :].strip()
        if not raw_key:
            return _rfc7807_response(
                status=401,
                title="Empty Credentials",
                detail="Authorization header is empty after 'Bearer '.",
                path=request.url.path,
            )

        # ── Route to JWT or API key flow ─────────────────────────────────
        # JWT tokens have 2 dots.  API keys have a known prefix.
        # If the token doesn't have an API key prefix, try JWT first.
        if not raw_key.startswith(API_KEY_PREFIXES) and _is_jwt_token(raw_key):
            jwt_result = _verify_jwt_and_set_state(request, raw_key)
            if jwt_result is not None:
                return jwt_result
            return await call_next(request)

        # ── API key flow ─────────────────────────────────────────────────
        lookup_hash: str = compute_lookup_hash(raw_key)

        # ── Check Redis cache ────────────────────────────────────────────
        redis: aioredis.Redis | None = getattr(request.app.state, "redis", None)
        if redis is not None:
            try:
                cached = await _lookup_key_in_redis(redis, lookup_hash)
                if cached is not None:
                    request.state.auth_type = "api_key"  # type: ignore[attr-defined]
                    request.state.org_id = cached["org_id"]  # type: ignore[attr-defined]
                    request.state.api_key_scopes = cached.get("scopes", [])  # type: ignore[attr-defined]
                    return await call_next(request)
            except Exception:
                # Graceful degradation — if Redis is down, fall through to DB.
                logger.warning(
                    "Redis auth cache lookup failed, falling back to DB",
                    exc_info=True,
                )

        # ── Check DB ─────────────────────────────────────────────────────
        db_factory: async_sessionmaker[AsyncSession] | None = getattr(
            request.app.state, "db_session_factory", None
        )
        if db_factory is None:
            logger.error(
                "db_session_factory not available on app.state — "
                "AuthMiddleware cannot query API keys."
            )
            return _rfc7807_response(
                status=500,
                title="Internal Server Error",
                detail="Authentication service is misconfigured.",
                path=request.url.path,
            )

        try:
            key_data = await _query_key_from_db(db_factory, lookup_hash)
        except Exception:
            logger.exception("Failed to query API key from database")
            return _rfc7807_response(
                status=500,
                title="Internal Server Error",
                detail="Authentication service temporarily unavailable.",
                path=request.url.path,
            )

        if key_data is None:
            return _rfc7807_response(
                status=401,
                title="Invalid API Key",
                detail="The provided API key is not valid.",
                path=request.url.path,
            )

        # ── Verify against salted hash ───────────────────────────────────
        if not verify_api_key(raw_key, key_data["key_hash"], key_data["salt"]):
            return _rfc7807_response(
                status=401,
                title="Invalid API Key",
                detail="The provided API key is not valid.",
                path=request.url.path,
            )

        # ── Validate key state ───────────────────────────────────────────
        if key_data["is_revoked"]:
            return _rfc7807_response(
                status=401,
                title="API Key Revoked",
                detail="This API key has been revoked.",
                path=request.url.path,
            )

        if key_data.get("expires_at") is not None:
            from datetime import datetime, timezone

            expires = key_data["expires_at"]
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires < datetime.now(timezone.utc):
                return _rfc7807_response(
                    status=401,
                    title="API Key Expired",
                    detail="This API key has expired.",
                    path=request.url.path,
                )

        # ── Set request state ────────────────────────────────────────────
        org_id: str = key_data["org_id"]
        scopes: list[str] = key_data["scopes"]

        request.state.auth_type = "api_key"  # type: ignore[attr-defined]
        request.state.org_id = org_id  # type: ignore[attr-defined]
        request.state.api_key_scopes = scopes  # type: ignore[attr-defined]

        # ── Cache in Redis (fire-and-forget) ─────────────────────────────
        if redis is not None:
            try:
                await _cache_key_in_redis(
                    redis,
                    lookup_hash,
                    {"org_id": org_id, "scopes": scopes},
                )
            except Exception:
                logger.warning("Failed to cache auth data in Redis", exc_info=True)

        # ── Set PostgreSQL RLS session config ────────────────────────────
        try:
            await _set_rls_context(db_factory, org_id, bypass_rls=False)
        except Exception:
            logger.warning("Failed to set RLS session config", exc_info=True)

        return await call_next(request)
