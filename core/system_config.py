"""Platform system configuration — cache-first, DB-authoritative.

Resolution order (mirrors ``core.org_config``):
1. Redis cache (key ``system_config``, TTL 5 min) — performance
   optimisation only.  Cache failures are logged at ERROR but the request
   continues to the DB.
2. The platform root user's ``users.config_override`` (the authoritative
   store, looked up via ``UserRepository.get_platform_root``).  DB failures
   propagate as hard errors.

Only the non-secret whitelist in :mod:`schemas.system_config` is ever
read or written — the ``OZ_*`` secret env vars are never exposed.

⚠️ BREAKING: org-creation policy keys are gone (single platform org).
Registration is governed by ``instance_settings.registration_mode``.
Defaults: all fields ``None`` when the root row has no record.
"""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from uuid import UUID

import redis.asyncio  # noqa: TC002 — runtime import mirrors core/org_config.py
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.system_config import (
    SYSTEM_CONFIG_WHITELIST,
    SystemConfigResponse,
    SystemConfigUpdate,
)

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

SYSTEM_CONFIG_CACHE_TTL: int = 300
"""TTL in seconds for the cached system config (default 5 minutes)."""

CACHE_KEY: str = "system_config"
"""Redis key for the cached system config."""

_HUB_API_TIMEOUT_S: float = 10.0
"""Anonymous Hub existence-check budget — fail loud, never hang the request."""


def _is_local_gate_model(model: str) -> bool:
    """Return True when *model* is a local path, not a Hub id.

    ``from_pretrained`` accepts both, so local-dir paths skip the Hub
    existence check entirely — they never touch the network.
    """
    return model.startswith(("/", "./", "../", "~")) or os.path.exists(model)


def _hub_model_status(model_id: str) -> int:
    """Synchronously GET the anonymous Hub metadata endpoint (executor-bound).

    Args:
        model_id: Public Hub repo id (already ruled out as a local path).

    Returns:
        The HTTP status (200 when the repo exists and is publicly visible).

    Raises:
        Exception: Whatever ``urllib`` raises on unreachable Hub (URLError,
            TimeoutError, ...) — the caller maps it to ``ValidationError``.
    """
    url = f"https://huggingface.co/api/models/{urllib.parse.quote(model_id, safe='/')}"
    req = urllib.request.Request(
        url, method="GET", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=_HUB_API_TIMEOUT_S) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


async def _assert_gate_model_reachable(model_id: str) -> None:
    """Fail loud when a Hub gate model is unknown or the Hub is unreachable.

    Anonymous, no token involved (the repo is public) — nothing secret is
    ever sent or logged.

    Args:
        model_id: Hub repo id to verify.

    Raises:
        ValidationError: On any non-200 status or Hub/network failure (→ 422,
            the enable is not persisted).
    """
    from core.exceptions import ValidationError  # noqa: PLC0415 — lazy, mirrors below

    loop = asyncio.get_running_loop()
    try:
        status = await loop.run_in_executor(None, lambda: _hub_model_status(model_id))
    except Exception as exc:
        raise ValidationError(
            f"Enrichment gate model {model_id!r} could not be verified — "
            f"Hugging Face Hub is unreachable ({exc})."
        ) from exc
    if status != 200:
        raise ValidationError(
            f"Enrichment gate model {model_id!r} does not exist or is not "
            f"publicly visible on Hugging Face Hub (status {status})."
        )


def _request_id() -> str:
    """Return the current request correlation ID (``""`` outside requests)."""
    return structlog.contextvars.get_contextvars().get("request_id", "")


async def _read_root_override(db: AsyncSession) -> tuple[UUID | None, dict[str, Any]]:
    """Read the platform root user's id and ``config_override``.

    Args:
        db: Async DB session.

    Returns:
        A ``(root_id_or_None, override_dict)`` tuple.
    """
    from repositories.user_repository import UserRepository  # noqa: PLC0415 — lazy, avoids import cycle

    root = await UserRepository(db).get_platform_root()
    if root is None:
        return None, {}
    return root.id, dict(root.config_override or {})


# ── Public API ────────────────────────────────────────────────────────────────


async def get_system_config(
    redis: redis.asyncio.Redis | None = None,
    db: AsyncSession | None = None,
    *,
    skip_cache: bool = False,
) -> SystemConfigResponse:
    """Fetch the platform system config: cache → DB.

    Only whitelisted (non-secret) keys are returned.  When the root row has
    no record — or a whitelisted key is absent — the field defaults to
    ``None`` (registration mode lives in ``instance_settings`` now).

    Args:
        redis: An optional async Redis client.  When ``None``, caching is
            skipped.
        db: Async DB session for the root-row read.  Required on a cache miss.
        skip_cache: If ``True``, bypass cache and always fetch from the DB.

    Returns:
        A :class:`SystemConfigResponse` with the whitelisted fields.

    Raises:
        RuntimeError: If *db* is ``None`` on a cache miss.
    """
    # 1. Try cache (unless skip_cache is set)
    # Stale-cache safety: SystemConfigResponse uses pydantic's default
    # extra="ignore", so cached JSON carrying removed keys (e.g. llm_* /
    # reranker_* written before their removal) still validates — unknown
    # keys are dropped, never raised.  TTL (5 min) self-heals; updates
    # delete the key outright (see update_system_config step 4).
    if not skip_cache and redis is not None:
        try:
            cached = await redis.get(CACHE_KEY)
            if cached:
                return SystemConfigResponse.model_validate_json(cached)
        except Exception:
            logger.error(
                "system_config.cache_read_failed",
                extra={"request_id": _request_id()},
                exc_info=True,
            )

    # 2. Fetch from DB — the raw override may contain other keys; only
    #    the whitelist is read out of it, never the whole payload.
    if db is None:
        raise RuntimeError("get_system_config requires a DB session on cache miss")
    _, raw = await _read_root_override(db)
    config = SystemConfigResponse(
        **{key: raw[key] for key in SYSTEM_CONFIG_WHITELIST if key in raw}
    )

    # 3. Write to cache (best-effort)
    if not skip_cache and redis is not None:
        try:
            await redis.setex(
                CACHE_KEY,
                SYSTEM_CONFIG_CACHE_TTL,
                config.model_dump_json(),
            )
        except Exception:
            logger.error(
                "system_config.cache_write_failed",
                extra={"request_id": _request_id()},
                exc_info=True,
            )

    return config


async def update_system_config(
    update_data: SystemConfigUpdate | dict[str, Any],
    redis: redis.asyncio.Redis | None = None,
    db: AsyncSession | None = None,
) -> SystemConfigResponse:
    """Update the platform system config in the DB, invalidate cache, re-read.

    Performs a shallow merge onto the root row's override.  Keys set to
    ``None`` are removed from the stored config (returning ``None`` on
    next read).  **Any key outside the whitelist is rejected** — a
    defensive second line behind the schema's ``extra="forbid"``, so a
    plain-dict caller can never write a secret key.

    Args:
        update_data: Fields to update.  Can be a
            :class:`SystemConfigUpdate` or a plain dict.
        redis: An optional async Redis client (for cache invalidation).
        db: Async DB session for the root-row write.  Required.

    Returns:
        The freshly stored config after the update.

    Raises:
        ValidationError: If a key outside the whitelist is present, or the
            platform root user does not exist yet (run setup first).
    """
    from core.exceptions import ValidationError

    if isinstance(update_data, SystemConfigUpdate):
        update_dict = update_data.model_dump(exclude_unset=True)
    else:
        update_dict = update_data

    unknown = set(update_dict) - SYSTEM_CONFIG_WHITELIST
    if unknown:
        raise ValidationError(
            "These keys are not editable system config: "
            f"{', '.join(sorted(unknown))}."
        )

    # Fail-loud enable path: enabling the gate with an unknown/unreachable
    # Hub model must 422 BEFORE anything is persisted.  ``enabled=false``
    # (or a missing/null block) skips the check — disabling stays offline-safe.
    gate_block = update_dict.get("enrichment_gate")
    if gate_block is not None:
        if isinstance(gate_block, dict):
            gate_enabled = gate_block.get("enabled")
            gate_model = gate_block.get("model")
        else:
            gate_enabled = getattr(gate_block, "enabled", None)
            gate_model = getattr(gate_block, "model", None)
        if gate_enabled is True:
            from core.enrichment_gate import (  # noqa: PLC0415 — lazy, avoids import cycle
                DEFAULT_MODEL as _GATE_DEFAULT_MODEL,
            )

            if gate_model is None:
                model_id = _GATE_DEFAULT_MODEL
            elif not isinstance(gate_model, str) or not gate_model.strip():
                raise ValidationError(
                    "Enrichment gate 'model' must be a non-empty string."
                )
            else:
                model_id = gate_model
            if not _is_local_gate_model(model_id):
                await _assert_gate_model_reachable(model_id)

    if db is None:
        raise RuntimeError("update_system_config requires a DB session")
    from repositories.user_repository import UserRepository  # noqa: PLC0415 — lazy, avoids import cycle

    repo = UserRepository(db)
    root = await repo.get_platform_root()
    if root is None:
        raise ValidationError(
            "Platform root user does not exist yet — "
            "complete POST /v1/auth/setup first."
        )

    # 1. Read existing root override
    existing = dict(root.config_override or {})

    # 2. Shallow merge: provided keys override, None values remove
    for key, value in update_dict.items():
        if value is None:
            existing.pop(key, None)
        else:
            existing[key] = value

    # 3. Write to DB (authoritative store)
    await repo.set_config_override(root.id, existing)

    # 4. Invalidate Redis cache
    if redis is not None:
        try:
            await redis.delete(CACHE_KEY)
        except Exception:
            logger.error(
                "system_config.cache_invalidation_failed",
                extra={"request_id": _request_id()},
                exc_info=True,
            )

    # 5. Re-read from DB (cache is cold — forces fresh read)
    return await get_system_config(redis=redis, db=db, skip_cache=True)
