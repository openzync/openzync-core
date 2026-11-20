"""Admin REST API for per-user configuration.

Endpoints allow dashboard users and API keys with ``admin:write`` scope to
read and update UI-exposed settings (LLM, embeddings, graph, behaviour)
that were previously env-var-only.

All endpoints are scoped to the authenticated user — an admin can
only manage their own config.

NOTE: the ``/admin/org/config`` prefix and ``OrgConfig*`` class names are
legacy (pre-0070 org scoping) kept for API compat — scope is per-user.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002 — runtime import

from core.audit import audit_action
from core.config import get_settings
from core.exceptions import RateLimitError
from core.openbao_exceptions import OpenBaoError
from dependencies.auth import require_permission
from dependencies.db import get_db
from schemas.organization_config import (
    SYSTEM_MANAGED_FALKORDB_FIELDS,
    SYSTEM_MANAGED_SURREALDB_FIELDS,
    OrgConfigBase,
    OrgConfigResponse,
    TestOrgConfigRequest,
    TestOrgConfigResponse,
    UpdateOrgConfigRequest,
)
from services.org_config_connection_service import OrgConfigConnectionService
from services.org_config_service import OrgConfigService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/org/config",
    tags=["Admin - Organization Config"],
)

#: Path to the onboarding defaults YAML file (relative to project root).
DEFAULTS_PATH = Path(__file__).parent.parent / "config" / "defaults" / "org_config.yaml"


def _get_system_managed_fields() -> list[str]:
    """Return field names that are locked at the system level.

    Checks the global Settings instance.  When a backend's URL is set at
    the system level, all corresponding per-user config fields are blocked.
    """
    settings = get_settings()
    fields: list[str] = []
    if settings.SURREALDB_URL:
        fields.extend(SYSTEM_MANAGED_SURREALDB_FIELDS)
    if settings.FALKORDB_URL:
        fields.extend(SYSTEM_MANAGED_FALKORDB_FIELDS)
    return fields


# ── Dependency factory ────────────────────────────────────────────────────────


def _get_config_service(
    request: Request,
) -> OrgConfigService:
    """Build a request-scoped OrgConfigService.

    Reads the OpenBao client and Redis client from ``request.app.state``
    (initialised during the application lifespan).
    """
    bao_client = getattr(request.app.state, "openbao_client", None)
    if bao_client is None:
        raise HTTPException(
            status_code=503,
            detail="OpenBao client not available — secrets backend not initialised",
        )
    redis = getattr(request.app.state, "redis", None)
    return OrgConfigService(bao_client=bao_client, redis=redis)


def _get_connection_service(
    request: Request,
) -> OrgConfigConnectionService:
    """Build a request-scoped OrgConfigConnectionService (503 if OpenBao down)."""
    bao_client = getattr(request.app.state, "openbao_client", None)
    if bao_client is None:
        raise HTTPException(
            status_code=503,
            detail="OpenBao client not available — secrets backend not initialised",
        )
    redis = getattr(request.app.state, "redis", None)
    return OrgConfigConnectionService(bao_client=bao_client, redis=redis)


#: Max config-test probes per user per window (fail-open on Redis errors).
_TEST_THROTTLE_LIMIT = 10
_TEST_THROTTLE_WINDOW_S = 60


async def _check_config_test_throttle(redis: Any | None, user_id: str) -> None:
    """Enforce a per-user fixed-window throttle (~10/min) for config tests.

    Fail-open on Redis errors — throttling is protective, not load-bearing.
    Exceeding the allowance raises :class:`RateLimitError` (HTTP 429).
    """
    if redis is None:
        return
    key = f"user_config:test:{user_id}"
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, _TEST_THROTTLE_WINDOW_S)
    except RedisError:
        # Fail-open: throttling is protective, not load-bearing — a Redis
        # outage must never block config testing.  Non-Redis errors still
        # propagate.
        logger.error(
            "org_config.test_throttle_failed",
            extra={"user_id": user_id},
            exc_info=True,
        )
        return
    if count > _TEST_THROTTLE_LIMIT:
        raise RateLimitError(
            "Too many config test requests — try again in a minute.",
            detail={
                "limit": _TEST_THROTTLE_LIMIT,
                "window_s": _TEST_THROTTLE_WINDOW_S,
            },
        )


# ═══════════════════════════════════════════════════════════════════════════════
# GET  /defaults  — Seeded onboarding defaults
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/defaults",
    response_model=UpdateOrgConfigRequest,
)
async def get_org_config_defaults() -> UpdateOrgConfigRequest:
    """Return seeded onboarding defaults for a new user.

    These are **not** the stored config — they are starter values for the
    onboarding form.  The user reviews and adjusts them before saving via
    ``PATCH /admin/org/config``.
    Empty-string secrets are intentional (frontend treats "" as unset —
    the user MUST fill them in; never persist the placeholder).

    No auth required — this endpoint returns only non-sensitive defaults.
    Secrets such as ``openai_api_key`` are returned as empty strings so
    the user must fill them in.
    """
    if not DEFAULTS_PATH.is_file():
        raise HTTPException(status_code=500, detail="Defaults configuration file not found")
    with DEFAULTS_PATH.open() as f:
        data: dict = yaml.safe_load(f)
    return UpdateOrgConfigRequest(**data)


# ═══════════════════════════════════════════════════════════════════════════════
# GET  — Retrieve stored config
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "",
    response_model=OrgConfigResponse,
)
async def get_org_config(
    _user_id: str = Depends(require_permission("configuration:read")),
    service: OrgConfigService = Depends(_get_config_service),
) -> OrgConfigResponse:
    """Get the stored configuration for the current user.

    Returns only the fields explicitly set in the DB.  Unset fields are
    ``null`` — there is no env-var fallback.

    Permission-gated (``require_permission("configuration:read")``): the
    response contains unmasked secrets, so members and API keys are denied.
    """
    response = await service.get_config_response(UUID(_user_id))
    response.system_managed_fields = _get_system_managed_fields()
    return response


# ═══════════════════════════════════════════════════════════════════════════════
# PATCH — Partial update
# ═══════════════════════════════════════════════════════════════════════════════


@router.patch(
    "",
    response_model=OrgConfigBase,
)
@audit_action("config.update", "config", "Configuration updated")
async def update_org_config(
    body: UpdateOrgConfigRequest,
    _user_id: str = Depends(require_permission("configuration:write")),
    service: OrgConfigService = Depends(_get_config_service),
) -> OrgConfigBase:
    """Partially update the user's configuration.

    Only fields explicitly provided in the request body are updated.
    Set a field to ``null`` to remove it from the stored config (it
    will be returned as ``null`` on subsequent reads).

    Requires an API key with ``admin:write`` scope or a JWT dashboard
    session.
    """
    # ── Reject system-managed fields ─────────────────────────────────
    system_managed = _get_system_managed_fields()
    if system_managed:
        overridden = {
            f for f in system_managed
            if f in body.model_dump(exclude_unset=True)
        }
        if overridden:
            raise HTTPException(
                status_code=422,
                detail=(
                    "These fields are configured at the system level "
                    "and cannot be modified: "
                    f"{', '.join(sorted(overridden))}."
                ),
            )
    return await service.update_config(UUID(_user_id), body)


# ═══════════════════════════════════════════════════════════════════════════════
# PUT — Full replace
# ═══════════════════════════════════════════════════════════════════════════════


@router.put(
    "",
    response_model=OrgConfigBase,
)
@audit_action("config.update", "config", "Configuration updated")
async def replace_org_config(
    body: UpdateOrgConfigRequest,
    _user_id: str = Depends(require_permission("configuration:write")),
    service: OrgConfigService = Depends(_get_config_service),
) -> OrgConfigBase:
    """Merge-alias for ``PATCH /admin/org/config`` (kept for API compat).

    Despite the ``PUT`` verb, this does NOT replace the stored config:
    only fields explicitly provided in the request body are updated and
    missing keys are preserved (merge, not replace).  Set a field to
    ``null`` to remove it.  Documented as an alias — not a true replace —
    so callers are never surprised by preserved keys.

    Requires an API key with ``admin:write`` scope or a JWT dashboard
    session.
    """
    # ── Reject system-managed fields ─────────────────────────────────
    system_managed = _get_system_managed_fields()
    if system_managed:
        overridden = {
            f for f in system_managed
            if f in body.model_dump(exclude_unset=True)
        }
        if overridden:
            raise HTTPException(
                status_code=422,
                detail=(
                    "These fields are configured at the system level "
                    "and cannot be modified: "
                    f"{', '.join(sorted(overridden))}."
                ),
            )
    return await service.update_config(UUID(_user_id), body)


# ═══════════════════════════════════════════════════════════════════════════════
# POST /test — Probe candidate connections without persisting
# ═══════════════════════════════════════════════════════════════════════════════


@router.post(
    "/test",
    response_model=TestOrgConfigResponse,
)
@audit_action("config.test", "org_config")
async def test_org_config(
    body: TestOrgConfigRequest,
    request: Request,
    _user_id: str = Depends(require_permission("configuration:write")),  # noqa: B008
    service: OrgConfigConnectionService = Depends(  # noqa: B008
        _get_connection_service
    ),
    as_user: UUID | None = None,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> TestOrgConfigResponse:
    """Probe live connectivity for a candidate config without saving it.

    Overlays ``body.config`` on the stored config in-memory and probes
    the requested domain.  With ``as_user``, that user's config override
    is layered under the candidate first (``candidate ⊕ user ⊕ stored``).
    Probe failures return 200 with ``ok: false`` entries — only malformed
    payloads (422) or a down secrets backend (503) change the status code.

    Requires ``configuration:write``.  Throttled to ~10 requests/min
    per user.  The candidate and probe details carry no secrets beyond
    action metadata in the audit log.
    """
    from repositories.user_repository import UserRepository
    from services.user_config_service import UserConfigService

    redis = getattr(request.app.state, "redis", None)
    await _check_config_test_throttle(redis, _user_id)
    candidate = body.config
    if as_user is not None:
        # Single-call probe path — read through the user-config service
        # (no direct repo round-trip from the router); the candidate
        # merge below is in-memory only.
        bao_client = getattr(request.app.state, "openbao_client", None)
        user_config_service = UserConfigService(
            user_repo=UserRepository(db),
            bao_client=bao_client,
            redis=redis,
        )
        override = await user_config_service.get_override(as_user)
        if override:
            from schemas.organization_config import UpdateOrgConfigRequest

            merged = {**override, **candidate.model_dump(exclude_unset=True)}
            candidate = UpdateOrgConfigRequest(**merged)
    try:
        return await service.test_connections(UUID(_user_id), body.domain, candidate)
    except OpenBaoError as exc:
        raise HTTPException(
            status_code=503,
            detail="Secrets backend unavailable — cannot load stored config",
        ) from exc
