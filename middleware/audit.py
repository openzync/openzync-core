"""Audit middleware — records every HTTP request to the audit log.

Runs after :class:`AuthMiddleware` in the middleware stack (see
``main.py`` for registration order) **but** captures its payload
in the *post-response* phase when ``request.state`` is fully
populated by the auth layer.

``AuditMiddleware`` enqueues an ARQ job for every non-exempt request.
The ARQ worker (low-priority queue) writes the entry to ``audit_logs``.
This design means:

- The request-response cycle is never blocked by audit I/O.
- Audit jobs survive an API process restart (they are in Redis).
- A backlog of audit jobs does not starve data-processing tasks
  (audit uses the ``"low"`` queue).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any
from uuid import UUID

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from core.config import settings
from services.pii_service import PIIDetector, PIIRedactor
from services.worker.worker_settings import get_queue_name

logger = logging.getLogger(__name__)

# Module-level PII detector + redactor (regex-only, stateless, no config needed).
_pii_detector = PIIDetector()
_pii_redactor = PIIRedactor(mode="mask")


async def _resolve_audit_body_capture(
    org_id: str | None,
    request: Request,
) -> bool:
    """Resolve whether to capture the response body for this request.

    Checks the org's DB config (via Redis cache, then DB).  When the
    field is not set for the org, defaults to ``False``.

    Args:
        org_id: The authenticated organization ID (may be ``None`` for
            unauthenticated requests).
        request: The incoming HTTP request (used to access ``app.state``).

    Returns:
        ``True`` if the response body should be captured for audit.
    """
    # Fast path: no org context, don't capture
    if org_id is None:
        return False

    redis = getattr(request.app.state, "redis", None)

    # Try Redis cache (fast path)
    if redis is not None:
        try:
            from core.org_config import CACHE_KEY_PREFIX

            cache_key = f"{CACHE_KEY_PREFIX}:{org_id}"
            cached = await redis.get(cache_key)
            if cached:
                raw = json.loads(cached)
                org_val = raw.get("audit_log_response_body")
                if org_val is not None:
                    return bool(org_val)
        except Exception:
            logger.debug("audit.org_config_cache_read_failed", exc_info=True)

    # Cache miss — resolve from DB (post-response so user doesn't wait).
    # On failure, default to False (don't capture).
    try:
        from core.db import AsyncSessionLocal
        from core.org_config import get_org_config

        async with AsyncSessionLocal() as db:
            config = await get_org_config(UUID(org_id), db, redis=redis, skip_cache=True)
            return bool(config.audit_log_response_body) if config.audit_log_response_body is not None else False
    except Exception:
        logger.warning("audit.org_config_db_resolve_failed", exc_info=True)
        return False

# ── Exempt paths (no audit for internal noise) ────────────────────────────────

EXEMPT_PATHS: frozenset = frozenset({
    "/health",
    "/ready",
    "/metrics",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/favicon.ico",
})

# ── Action mapping: (method, known_path_prefix) → (action, resource_type) ────

ROUTE_ACTIONS: dict[tuple[str, str], tuple[str, str]] = {
    # Auth
    ("POST", "/v1/auth/signup"): ("auth.signup", "user"),
    ("POST", "/v1/auth/login"): ("auth.login", "session"),
    ("POST", "/v1/auth/refresh"): ("auth.refresh", "token"),
    ("PATCH", "/v1/auth/me"): ("auth.profile.update", "user"),
    # Admin
    ("POST", "/admin/organizations"): ("organization.create", "organization"),
    ("POST", "/v1/admin/schemas"): ("schema.create", "schema"),
    ("PUT", "/v1/admin/schemas/"): ("schema.update", "schema"),
    ("DELETE", "/v1/admin/schemas/"): ("schema.delete", "schema"),
    ("POST", "/v1/admin/api-keys"): ("api_key.create", "api_key"),
    ("DELETE", "/v1/admin/api-keys/"): ("api_key.revoke", "api_key"),
    # Users
    ("POST", "/v1/users"): ("user.create", "user"),
    ("PATCH", "/v1/users/"): ("user.update", "user"),
    ("DELETE", "/v1/users/"): ("user.delete", "user"),
    # Sessions
    ("POST", "/v1/users/"): ("session.create", "session"),
    # session.create and user.* share the same prefix — must check longer path first
    ("DELETE", "/v1/users/"): ("session.delete", "session"),
    # Memory
    ("POST", "/v1/users/"): ("memory.ingest", "episode"),
    ("DELETE", "/v1/users/"): ("memory.wipe", "memory"),
    # Facts
    ("POST", "/v1/users/"): ("fact.create", "fact"),
    # Graph
    ("DELETE", "/v1/users/"): ("graph.node.delete", "graph_entity"),
}

# Longer paths must be checked first to avoid false matches
_PREFIX_ORDERED: list[tuple[str, tuple[str, str]]] = []


def _resolve_action(method: str, path: str) -> tuple[str, str]:
    """Resolve a route to ``(action, resource_type)``.

    Tries exact matches first, then prefix matches ordered by prefix
    length (longest first) to avoid ``/v1/users/`` matching a
    ``/v1/users/{id}/memory`` path incorrectly.

    Falls back to ``http.{method}`` / ``{resource}`` for unknown paths.
    """
    # Exact match
    key = (method, path)
    if key in ROUTE_ACTIONS:
        return ROUTE_ACTIONS[key]

    # Prefix match (sorted longest-first)
    for prefix, result in _PREFIX_ORDERED:
        if path.startswith(prefix):
            return result

    # Fallback — extract resource from first path segment
    parts = path.strip("/").split("/")
    resource = parts[-1] if parts else "unknown"
    action = f"http.{method.lower()}"
    return action, resource


# Build prefix-ordered list once at module load
def _build_prefix_list() -> None:
    seen: set[str] = set()
    items: list[tuple[str, tuple[str, str]]] = []
    for (meth, prefix), result in ROUTE_ACTIONS.items():
        if prefix not in seen:
            items.append((prefix, result))
            seen.add(prefix)
    items.sort(key=lambda x: len(x[0]), reverse=True)
    _PREFIX_ORDERED.extend(items)


_build_prefix_list()


# ── Middleware ─────────────────────────────────────────────────────────────────


class AuditMiddleware(BaseHTTPMiddleware):
    """Middleware that enqueues an audit job for every non-exempt request.

    Operates in the **post-response** phase so that ``request.state``
    is fully populated by :class:`AuthMiddleware <openzep.middleware.auth.AuthMiddleware>`.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Process the request and enqueue an audit job after the response.

        Args:
            request: The incoming HTTP request.
            call_next: The next middleware or route handler.

        Returns:
            The HTTP response (unchanged).
        """
        response = await call_next(request)

        # ── Skip exempt paths and OPTIONS preflights ───────────────────────
        if request.method in ("OPTIONS", "GET") or request.url.path in EXEMPT_PATHS:
            return response

        try:
            await self._enqueue_audit(request, response)
        except Exception:
            # Audit must never break the response path.
            logger.exception(
                "audit.enqueue_failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                },
            )

        return response

    async def _enqueue_audit(self, request: Request, response: Response) -> None:
        """Extract request metadata and enqueue an ARQ audit job."""
        # ── Actor info from request.state (set by AuthMiddleware) ──────────
        org_id: str | None = getattr(request.state, "org_id", None)
        user_id: str | None = getattr(request.state, "user_id", None)
        auth_type: str | None = getattr(request.state, "auth_type", None)

        # ── Resolve action / resource type ─────────────────────────────────
        method = request.method
        path = request.url.path
        action, resource_type = _resolve_action(method, path)

        # ── Extract resource_id from path params (if available) ────────────
        # Starlette stores path params on request.path_params after routing.
        resource_id: str | None = request.path_params.get("user_id") or \
            request.path_params.get("session_id") or \
            request.path_params.get("node_id") or \
            request.path_params.get("schema_id") or \
            request.path_params.get("key_id") or \
            request.path_params.get("episode_id") or \
            None

        # ── Build actor fields ─────────────────────────────────────────────
        if auth_type == "jwt":
            actor_id = user_id
            actor_type = "user"
        elif auth_type == "api_key":
            actor_id = org_id  # API keys don't have a user_id
            actor_type = "api_key"
        else:
            actor_id = "anonymous"
            actor_type = None

        # ── IP address ─────────────────────────────────────────────────────
        forwarded = request.headers.get("X-Forwarded-For", "")
        ip_address: str = forwarded.split(",")[0].strip() or \
            (request.client.host if request.client else "unknown")

        # ── Details payload ────────────────────────────────────────────────
        details: dict[str, Any] = {
            "method": method,
            "path": path,
            "query": str(request.url.query),
            "status_code": response.status_code,
            "request_id": getattr(request.state, "request_id", None),
            "user_agent": request.headers.get("User-Agent", ""),
        }

        # Resolve audit_log_response_body: per-org config → env default.
        _capture_body = await _resolve_audit_body_capture(org_id, request)
        if _capture_body:
            try:
                body = await self._read_response_body(response)
                if body:
                    raw_text = body[:10_000]
                    # Run PII detection + redaction using regex-only detector.
                    detections = _pii_detector.detect(raw_text)
                    redacted = _pii_redactor.apply(raw_text, detections) if detections else raw_text
                    details["response_body"] = redacted
            except Exception:
                logger.warning("audit.body_read_failed", exc_info=True)

        # ── Enqueue ARQ job (fire-and-forget) ─────────────────────────────
        arq_pool = getattr(request.app.state, "arq_pool", None)
        if arq_pool is None:
            logger.warning("audit.no_arq_pool", extra={"path": path})
            return

        queue_full_name = get_queue_name(settings.ENVIRONMENT, "low")
        await arq_pool.enqueue(
            "write_audit_log",
            queue_name=queue_full_name,
            organization_id=str(org_id) if org_id else None,
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=json.dumps(details),
            ip_address=ip_address,
            trace_id=getattr(request.state, "request_id", None) or str(uuid.uuid4()),
        )

    @staticmethod
    async def _read_response_body(response: Response) -> str | None:
        """Read the response body without breaking the response.

        Works by consuming the async body iterator and re-constructing
        the response.  Only reads for non-streaming responses.
        """
        if not hasattr(response, "body_iterator"):
            return None

        body_chunks: list[bytes] = []
        try:
            async for chunk in response.body_iterator:
                body_chunks.append(chunk)
        except (StopAsyncIteration, RuntimeError):
            return None

        body = b"".join(body_chunks)

        # Re-construct the response so downstream middleware can read it
        # (Starlette's StreamingResponse consumes the iterator once).
        response.body_iterator = iter([body])  # type: ignore[assignment]

        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return None
