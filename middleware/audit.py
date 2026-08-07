"""Audit middleware — records every HTTP request to the audit log.

Runs after :class:`AuthMiddleware` in the middleware stack (see
``main.py`` for registration order) **but** captures its payload
in the *post-response* phase when ``scope["state"]`` is fully
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

import orjson
import logging
import re
import uuid
from typing import Any
from uuid import UUID

from starlette.types import ASGIApp, Receive, Scope, Send

from core.audit import get_audit_metadata
from core.config import get_settings
from core.exceptions import DatabaseUnavailableError
from services.pii_service import PIIDetector, PIIRedactor
from services.worker.worker_settings import get_queue_name

logger = logging.getLogger(__name__)

# Module-level PII detector + redactor (regex-only, stateless, no config needed).
_pii_detector = PIIDetector()
_pii_redactor = PIIRedactor(mode="mask")

# UUID pattern for extracting resource IDs from URL paths.
_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


async def _resolve_audit_body_capture(
    org_id: str | None,
    scope: Scope,
) -> bool:
    """Resolve whether to capture the response body for this request.

    Checks the org's DB config (via Redis cache, then DB).  When the
    field is not set for the org, defaults to ``False``.

    Args:
        org_id: The authenticated organization ID (may be ``None`` for
            unauthenticated requests).
        scope: The ASGI connection scope (used to access ``app.state``).

    Returns:
        ``True`` if the response body should be captured for audit.
    """
    if org_id is None:
        return False

    app = scope.get("app")
    if app is None:
        return False

    redis = getattr(app.state, "redis", None)

    # Try Redis cache (fast path)
    if redis is not None:
        try:
            from core.org_config import CACHE_KEY_PREFIX

            cache_key = f"{CACHE_KEY_PREFIX}:{org_id}"
            cached = await redis.get(cache_key)
            if cached:
                raw = orjson.loads(cached.encode())
                org_val = raw.get("audit_log_response_body")
                if org_val is not None:
                    return bool(org_val)
        except Exception:
            logger.warning("audit.org_config_cache_read_failed", exc_info=True)
            # Fall through to OpenBao — cache miss is acceptable, OpenBao is authoritative

    # Cache miss — resolve from OpenBao (post-response so user doesn't wait).
    try:
        from core.org_config import get_org_config

        bao_client = getattr(app.state, "openbao_client", None)
        if bao_client is not None:
            config = await get_org_config(
                UUID(org_id), redis=redis, bao_client=bao_client, skip_cache=True
            )
        else:
            from core.config import BootstrapSettings
            from core.openbao import OpenBaoClient

            bootstrap = BootstrapSettings()
            async with OpenBaoClient(
                bootstrap.OPENBAO_ADDR,
                bootstrap.OPENBAO_ROLE_ID,
                bootstrap.OPENBAO_SECRET_ID,
                timeout=10.0,
            ) as _bao:
                config = await get_org_config(
                    UUID(org_id), redis=redis, bao_client=_bao, skip_cache=True
                )
        return bool(config.audit_log_response_body) if config.audit_log_response_body is not None else False
    except Exception as exc:
        logger.error("audit.org_config_db_resolve_failed", exc_info=True)
        raise DatabaseUnavailableError(
            "Cannot resolve org config for audit decision."
        ) from exc


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

# Routes whose responses carry the one-time webhook signing secret
# (``routers/admin_webhooks.py`` POST "/v1/admin/webhooks" → create, and any
# future rotate route added there).  SECURITY: the raw secret must never be
# persisted to audit_logs — it is shown exactly once to the caller, so these
# responses skip response-body capture while the audit event itself (action,
# actor, status) is still logged.  Add any new secret-bearing route here.
WEBHOOK_SECRET_RESPONSE_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/v1/admin/webhooks"),
})

def _resolve_action(
    method: str,
    path: str,
    scope: Scope,
) -> tuple[str, str, str | None]:
    """Resolve audit (action, resource_type, display_name) from route metadata.

    Tries to match the request against registered FastAPI routes.  If the
    matched route's endpoint has audit metadata (via ``@audit_action``
    decorator), uses that.  Falls back to ``http.{method}``.
    """
    app = scope.get("app")
    if app is not None and hasattr(app, "routes"):
        for route in app.routes:
            if not hasattr(route, "endpoint"):
                continue
            if hasattr(route, "methods") and method not in route.methods:
                continue
            route_path: str | None = getattr(route, "path", None)
            if route_path is not None:
                match = route.path_regex.match(path) if hasattr(route, "path_regex") else None
                if match is not None:
                    meta = get_audit_metadata(route.endpoint)
                    if meta is not None:
                        return (meta["action"], meta["resource"], meta["display"])

    # Fallback — no route matched or no metadata found
    parts = path.strip("/").split("/") if path.strip("/") else ["unknown"]
    return (f"http.{method.lower()}", parts[-1], None)


# ── Middleware ─────────────────────────────────────────────────────────────────


class AuditMiddleware:
    """Raw ASGI middleware that enqueues an audit job for every non-exempt request.

    Operates in the **post-response** phase so that ``scope["state"]``
    is fully populated by :class:`AuthMiddleware <openzync.middleware.auth.AuthMiddleware>`.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        path = scope.get("path", "")

        # ── Buffer response body for audit capture ──────────────────────
        status_code: int = 200
        body_chunks: list[bytes] = []

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 200)
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if chunk:
                    body_chunks.append(chunk)
            await send(message)

        await self.app(scope, receive, send_wrapper)

        # ── Post-response: enqueue audit (fire-and-forget) ──────────────
        if method in ("OPTIONS", "GET") or path in EXEMPT_PATHS:
            return

        try:
            await self._enqueue_audit(scope, method, path, status_code, body_chunks)
        except Exception:
            logger.error(
                "audit.enqueue_failed",
                extra={"method": method, "path": path},
                exc_info=True,
            )
            # Fire-and-forget — don't fail the request for an audit log

    async def _enqueue_audit(
        self,
        scope: Scope,
        method: str,
        path: str,
        status_code: int,
        body_chunks: list[bytes],
    ) -> None:
        """Extract request metadata and enqueue an ARQ audit job."""
        state = scope.get("state") or {}
        headers = dict(scope.get("headers") or [])

        org_id: str | None = state.get("org_id", None)
        user_id: str | None = state.get("user_id", None)
        auth_type: str | None = state.get("auth_type", None)
        request_id: str | None = state.get("request_id", None)

        # ── Resolve action / resource type / display_name ──────────────────
        action, resource_type, display_name = _resolve_action(method, path, scope)

        # ── Extract resource_id from path ──────────────────────────────────
        matches = _UUID_PATTERN.findall(path)
        resource_id: str | None = matches[-1] if matches else None

        # ── Build actor fields ─────────────────────────────────────────────
        if auth_type == "jwt":
            actor_id = user_id
            actor_type = "user"
        elif auth_type == "api_key":
            actor_id = org_id
            actor_type = "api_key"
        else:
            actor_id = "anonymous"
            actor_type = None

        # ── IP address ─────────────────────────────────────────────────────
        forwarded = headers.get(b"x-forwarded-for", b"").decode()
        ip_address: str = forwarded.split(",")[0].strip() if forwarded else (
            scope["client"][0] if scope.get("client") else "unknown"
        )

        # ── Details payload ────────────────────────────────────────────────
        query_bytes: bytes = scope.get("query_string", b"")
        details: dict[str, Any] = {
            "method": method,
            "path": path,
            "query": query_bytes.decode(),
            "status_code": status_code,
            "request_id": request_id,
            "user_agent": headers.get(b"user-agent", b"").decode(),
            "display_name": display_name,
        }

        # Resolve audit_log_response_body: per-org config → env default.
        _capture_body = await _resolve_audit_body_capture(org_id, scope)
        if (method, path) in WEBHOOK_SECRET_RESPONSE_ROUTES:
            # SECURITY: never persist the one-time webhook signing secret —
            # it is returned exactly once to the caller (see the constant's
            # comment for the routes this covers).
            _capture_body = False
        if _capture_body and body_chunks:
            try:
                raw_text = b"".join(body_chunks).decode("utf-8", errors="replace")[:10_000]
                detections = _pii_detector.detect(raw_text)
                redacted = _pii_redactor.apply(raw_text, detections) if detections else raw_text
                details["response_body"] = redacted
            except Exception:
                logger.warning("audit.body_read_failed", exc_info=True)

        # ── Enqueue ARQ job (fire-and-forget) ─────────────────────────────
        app = scope.get("app")
        arq_pool = getattr(app.state, "arq_pool", None) if app is not None else None
        if arq_pool is None:
            logger.warning("audit.no_arq_pool", extra={"path": path})
            return

        queue_full_name = get_queue_name(get_settings().ENVIRONMENT, "low")
        await arq_pool.enqueue(
            "write_audit_log",
            queue_name=queue_full_name,
            organization_id=str(org_id) if org_id else None,
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=orjson.dumps(details).decode("utf-8"),
            ip_address=ip_address,
            trace_id=request_id or str(uuid.uuid4()),
        )
