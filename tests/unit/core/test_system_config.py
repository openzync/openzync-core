"""Unit tests for core.system_config — the platform config whitelist.

Covers:
- whitelist: a secret key in the update body is rejected (422 at schema
  level, ValidationError at the core level for plain dicts).
- defaults: no platform root row → ``{}`` (the whitelist is empty;
  registration mode lives in ``instance_settings`` now).
- cache: read populates the cache; update invalidates it and re-reads
  fresh from the DB (``users.config_override`` on the platform root row).
- secrets are never part of the response.

The DB layer (``UserRepository``) is faked in-memory — these are unit
tests (no I/O).  Redis is an ``AsyncMock``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from core.exceptions import ValidationError as AppValidationError
from core.system_config import get_system_config, update_system_config
from schemas.system_config import (
    SYSTEM_CONFIG_WHITELIST,
    SystemConfigResponse,
    SystemConfigUpdate,
)

pytestmark = pytest.mark.unit

ROOT_ID = UUID("00000000-0000-0000-0000-00000000aa00")

_STORE: dict[str, Any] = {}
"""Fake platform-root ``config_override``.  Empty = no root row."""


@pytest.fixture(autouse=True)
def _clean_store():
    """Reset the fake root store before each test."""
    _STORE.clear()
    yield
    _STORE.clear()


def _patch_repo(monkeypatch: pytest.MonkeyPatch, has_root: bool = True) -> None:
    """Patch ``UserRepository`` (as imported in ``core.system_config``) with a fake."""

    class _FakeRoot:
        def __init__(self, root_id: UUID, override: dict[str, Any]) -> None:
            self.id = root_id
            self.config_override = dict(override)

    class _FakeRepo:
        def __init__(self, db: Any) -> None:
            self._db = db

        async def get_platform_root(self) -> Any | None:
            if not has_root:
                return None
            return _FakeRoot(ROOT_ID, _STORE)

        async def set_config_override(
            self, user_id: UUID, config: dict[str, Any]
        ) -> dict[str, Any]:
            _STORE.clear()
            _STORE.update(config)
            return dict(config)

    monkeypatch.setattr(
        "repositories.user_repository.UserRepository", _FakeRepo
    )


@pytest.fixture
def mock_db() -> AsyncMock:
    """Return a placeholder DB session (never touched — repo is faked)."""
    return AsyncMock()


class TestSystemConfigSchema:
    """Schema-level whitelist enforcement."""

    def test_secret_key_in_update_body_rejected_422(self) -> None:
        """A secret key (openai_api_key) fails schema validation (→ 422)."""
        with pytest.raises(ValidationError):
            SystemConfigUpdate(openai_api_key="sk-123")  # type: ignore[call-arg]

    def test_secret_key_variants_rejected(self) -> None:
        """Every secret-shaped key is rejected by construction."""
        for secret in (
            "openai_api_key",
            "anthropic_api_key",
            "smtp_password",
            "database_url",
            "any_secret",
        ):
            with pytest.raises(ValidationError):
                SystemConfigUpdate(**{secret: "x"})  # type: ignore[arg-type]

    def test_defaults(self) -> None:
        """SystemConfigResponse is fieldless — ``GET`` returns ``{}``."""
        cfg = SystemConfigResponse()
        assert cfg.model_dump() == {}


class TestGetSystemConfig:
    """get_system_config resolution + caching."""

    @pytest.mark.asyncio
    async def test_defaults_when_no_root_row(
        self, mock_db: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No platform root row → empty config."""
        _patch_repo(monkeypatch, has_root=False)

        cfg = await get_system_config(redis=None, db=mock_db)

        assert cfg.model_dump() == {}

    @pytest.mark.asyncio
    async def test_whitelisted_keys_returned_secrets_never(
        self, mock_db: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stored keys outside the empty whitelist never surface in GET."""
        _STORE.update(
            {
                "llm_backend": "openai",
                "llm_model": "gpt-4o-mini",
                "OZ_DATABASE_URL": "postgresql://secret",
                "openai_api_key": "sk-secret",
                "SMTP_PASSWORD": "smtp-secret",
            }
        )
        _patch_repo(monkeypatch)

        cfg = await get_system_config(redis=None, db=mock_db)

        assert cfg.model_dump() == {}
        # Secrets never surface in the response model — by construction.
        assert "openai_api_key" not in SystemConfigResponse.model_fields
        assert "OZ_DATABASE_URL" not in cfg.model_dump()
        assert "SMTP_PASSWORD" not in cfg.model_dump()

    @pytest.mark.asyncio
    async def test_no_db_on_cache_miss_raises(self) -> None:
        """A cache miss with ``db=None`` raises ``RuntimeError``."""
        redis = AsyncMock()
        redis.get.return_value = None
        with pytest.raises(RuntimeError, match="requires a DB session"):
            await get_system_config(redis, None)

    @pytest.mark.asyncio
    async def test_cache_hit_skips_db(self) -> None:
        """A warm cache short-circuits the DB read."""
        redis = AsyncMock()
        redis.get.return_value = SystemConfigResponse().model_dump_json()

        cfg = await get_system_config(redis, None)

        assert cfg.model_dump() == {}

    @pytest.mark.asyncio
    async def test_miss_populates_cache(
        self, mock_db: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cache miss populates the cache with the (empty) config."""
        redis = AsyncMock()
        redis.get.return_value = None
        _STORE.update(
            {"llm_model": "gpt-4o-mini", "openai_api_key": "sk-secret"}
        )
        _patch_repo(monkeypatch)

        await get_system_config(redis, mock_db)

        redis.setex.assert_awaited_once()
        cache_key, ttl, payload = redis.setex.call_args.args
        assert cache_key == "system_config"
        assert ttl == 300
        assert payload == "{}"
        # The raw override never lands in the cache verbatim.
        assert "gpt-4o-mini" not in payload
        assert "sk-secret" not in payload


class TestUpdateSystemConfig:
    """update_system_config merge + invalidation."""

    @pytest.mark.asyncio
    async def test_update_merges_invalidates_and_returns_fresh(
        self, mock_db: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty update is a no-op merge, invalidates cache, re-reads."""
        redis = AsyncMock()
        _STORE.update(
            {
                "llm_model": "old-model",
                "openai_api_key": "sk-keep",  # noqa: S105 — test fixture secret
            }
        )
        _patch_repo(monkeypatch)

        result = await update_system_config(
            SystemConfigUpdate(),
            redis=redis,
            db=mock_db,
        )

        assert result.model_dump() == {}
        # Merge preserved the unrelated stored keys (the whitelist is
        # empty, so the update touches nothing pre-existing).
        assert _STORE["openai_api_key"] == "sk-keep"  # noqa: S105 — test fixture secret
        assert _STORE["llm_model"] == "old-model"
        redis.delete.assert_awaited_once_with("system_config")

    @pytest.mark.asyncio
    async def test_update_rejects_secret_key_from_plain_dict(
        self, mock_db: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain-dict update with a non-whitelisted key → ValidationError."""
        _patch_repo(monkeypatch)

        with pytest.raises(AppValidationError):
            await update_system_config(
                {"openai_api_key": "sk-123"},
                redis=None,
                db=mock_db,
            )

        assert _STORE == {}

    @pytest.mark.asyncio
    async def test_none_values_remove_keys(
        self, mock_db: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A None value for a non-whitelisted key is rejected, not removed."""
        _patch_repo(monkeypatch)

        with pytest.raises(AppValidationError):
            await update_system_config(
                {"llm_model": None},
                redis=None,
                db=mock_db,
            )

        assert _STORE == {}

    @pytest.mark.asyncio
    async def test_missing_root_row_raises(
        self, mock_db: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Updating before setup (no root row) raises ``ValidationError``."""
        _patch_repo(monkeypatch, has_root=False)

        with pytest.raises(AppValidationError, match="setup first"):
            await update_system_config(
                SystemConfigUpdate(),
                redis=None,
                db=mock_db,
            )

    def test_whitelist_matches_response_fields(self) -> None:
        """The whitelist is exactly the response model's fields."""
        assert SYSTEM_CONFIG_WHITELIST == frozenset(
            SystemConfigResponse.model_fields
        )
