"""Unit tests for per-user configuration resolution (``core.platform_config``).

Covers:
- ``get_platform_config`` — cache → DB (``users.config_override``) → cache write
- Redis cache hit → no DB read
- Redis unavailable → fallback to DB
- Skip-cache flag bypasses Redis
- Empty override → all-None config (graph_backend defaults to falkordb)
- No DB on a cache miss → RuntimeError
- Poisoned stored config → ValidationError (field names only, never values)
- ``update_platform_config`` — shallow merge, None removal, cache invalidation
- ``build_cache_key`` helper

The DB layer (``UserRepository``) is faked in-memory — these are unit
tests (no I/O).  Redis is an ``AsyncMock``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from core.exceptions import ValidationError as PlatformConfigValidationError
from core.platform_config import (
    CACHE_KEY_PREFIX,
    PLATFORM_CONFIG_CACHE_TTL,
    build_cache_key,
    get_platform_config,
    update_platform_config,
)
from schemas.platform_config import PlatformConfigBase, UpdatePlatformConfigRequest

USER_ID = UUID("00000000-0000-0000-0000-000000000001")
CACHE_KEY = f"{CACHE_KEY_PREFIX}:{USER_ID}"

_STORE: dict[UUID, dict[str, Any]] = {}
"""In-memory ``users.config_override`` keyed by user id."""


@pytest.fixture(autouse=True)
def _clean_store():
    """Reset the fake DB store before each test."""
    _STORE.clear()
    yield
    _STORE.clear()


def _patch_repo(monkeypatch: pytest.MonkeyPatch, store: dict | None = None) -> None:
    """Patch ``UserRepository`` (as imported in ``core.platform_config``) with a fake.

    Uses the module-level ``_STORE`` unless an explicit *store* is given.
    """
    backing = _STORE if store is None else store

    class _FakeRepo:
        def __init__(self, db: Any) -> None:
            self._db = db

        async def get_config_override(self, user_id: UUID) -> dict[str, Any]:
            return dict(backing.get(user_id, {}))

        async def set_config_override(
            self, user_id: UUID, config: dict[str, Any]
        ) -> dict[str, Any]:
            backing[user_id] = dict(config)
            return dict(config)

    monkeypatch.setattr(
        "repositories.user_repository.UserRepository", _FakeRepo
    )


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Return a mock async Redis client."""
    r = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.setex = AsyncMock(return_value=True)
    r.delete = AsyncMock(return_value=True)
    return r


@pytest.fixture
def mock_db() -> AsyncMock:
    """Return a placeholder DB session (never touched — repo is faked)."""
    return AsyncMock()


@pytest.mark.unit
class TestGetPlatformConfig:
    """Reading org config — cache-first, DB-authoritative."""

    @pytest.mark.asyncio
    async def test_fetches_from_db_and_caches(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First call fetches from the DB and writes to Redis cache."""
        _STORE[USER_ID] = {"llm_backend": "openai", "llm_model": "gpt-4"}
        _patch_repo(monkeypatch)

        config = await get_platform_config(USER_ID, redis=mock_redis, db=mock_db)

        assert config.llm_backend == "openai"
        assert config.llm_model == "gpt-4"
        mock_redis.setex.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cache_hit_skips_db(
        self,
        mock_redis: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Config is returned from cache when available — no DB read."""
        cached_config = PlatformConfigBase(llm_backend="anthropic", llm_model="claude-opus-4")
        mock_redis.get.return_value = cached_config.model_dump_json()
        _patch_repo(monkeypatch)

        # db=None proves the DB is never touched on a cache hit.
        config = await get_platform_config(USER_ID, redis=mock_redis, db=None)

        assert config.llm_backend == "anthropic"
        assert config.llm_model == "claude-opus-4"

    @pytest.mark.asyncio
    async def test_cache_miss_fetches_from_db(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When cache is empty, config is fetched from the DB."""
        mock_redis.get.return_value = None
        _STORE[USER_ID] = {"llm_backend": "ollama"}
        _patch_repo(monkeypatch)

        config = await get_platform_config(USER_ID, redis=mock_redis, db=mock_db)

        assert config.llm_backend == "ollama"

    @pytest.mark.asyncio
    async def test_skip_cache_bypasses_redis(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``skip_cache=True`` skips both cache read and write."""
        _STORE[USER_ID] = {"llm_backend": "azure"}
        _patch_repo(monkeypatch)

        config = await get_platform_config(
            USER_ID,
            redis=mock_redis,
            db=mock_db,
            skip_cache=True,
        )

        assert config.llm_backend == "azure"
        mock_redis.get.assert_not_awaited()
        mock_redis.setex.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_redis_skips_cache(
        self,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``redis`` is ``None``, caching is skipped entirely."""
        _STORE[USER_ID] = {"llm_backend": "openai"}
        _patch_repo(monkeypatch)

        config = await get_platform_config(USER_ID, redis=None, db=mock_db)

        assert config.llm_backend == "openai"

    @pytest.mark.asyncio
    async def test_redis_unavailable_falls_back_to_db(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When cache read fails, fall back to the DB (no crash)."""
        mock_redis.get.side_effect = ConnectionError("Redis down")
        _STORE[USER_ID] = {"llm_backend": "openai"}
        _patch_repo(monkeypatch)

        config = await get_platform_config(USER_ID, redis=mock_redis, db=mock_db)

        assert config.llm_backend == "openai"

    @pytest.mark.asyncio
    async def test_empty_override_returns_all_none(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When no config is stored, every field is ``None`` except graph_backend→falkordb."""
        _patch_repo(monkeypatch)

        config = await get_platform_config(USER_ID, redis=mock_redis, db=mock_db)

        # All fields except graph_backend should be None — empty override
        # defaults graph_backend to falkordb.
        for field_name in PlatformConfigBase.model_fields:
            if field_name == "graph_backend":
                assert getattr(config, field_name) == "falkordb", f"{field_name} should be falkordb"
            else:
                assert getattr(config, field_name) is None, f"{field_name} should be None"

    @pytest.mark.asyncio
    async def test_no_db_on_cache_miss_raises(
        self,
        mock_redis: AsyncMock,
    ) -> None:
        """A cache miss with ``db=None`` raises ``RuntimeError``."""
        mock_redis.get.return_value = None
        with pytest.raises(RuntimeError, match="requires a DB session"):
            await get_platform_config(USER_ID, redis=mock_redis, db=None)

    @pytest.mark.asyncio
    async def test_poisoned_stored_config_raises(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stored values failing schema validation raise, naming fields only."""
        _STORE[USER_ID] = {"llm_backend": "not-a-backend"}
        _patch_repo(monkeypatch)

        with pytest.raises(PlatformConfigValidationError, match="llm_backend"):
            await get_platform_config(USER_ID, redis=mock_redis, db=mock_db)

    @pytest.mark.asyncio
    async def test_cache_write_failure_logged(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cache write failure is logged but does not fail the request."""
        _STORE[USER_ID] = {"llm_backend": "openai"}
        _patch_repo(monkeypatch)
        mock_redis.setex.side_effect = ConnectionError("Redis write failed")

        # Should not raise
        config = await get_platform_config(USER_ID, redis=mock_redis, db=mock_db)
        assert config.llm_backend == "openai"

    @pytest.mark.asyncio
    async def test_cache_ttl_applied(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The cache TTL is passed to ``redis.setex``."""
        _STORE[USER_ID] = {"llm_backend": "openai"}
        _patch_repo(monkeypatch)
        config = await get_platform_config(USER_ID, redis=mock_redis, db=mock_db)

        mock_redis.setex.assert_awaited_once_with(
            CACHE_KEY,
            PLATFORM_CONFIG_CACHE_TTL,
            config.model_dump_json(),
        )

    @pytest.mark.asyncio
    async def test_corrupted_cache_ignored(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Corrupted cache data is ignored and the DB is used instead."""
        mock_redis.get.return_value = "invalid json {{{"
        _STORE[USER_ID] = {"llm_backend": "openai"}
        _patch_repo(monkeypatch)

        config = await get_platform_config(USER_ID, redis=mock_redis, db=mock_db)

        assert config.llm_backend == "openai"


@pytest.mark.unit
class TestUpdatePlatformConfig:
    """Updating org config — write to DB, invalidate cache, re-read."""

    @pytest.mark.asyncio
    async def test_updates_single_field(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Updating a single field persists it and invalidates cache."""
        _STORE[USER_ID] = {"llm_backend": "openai"}
        _patch_repo(monkeypatch)
        update = UpdatePlatformConfigRequest(llm_backend="anthropic")

        config = await update_platform_config(
            USER_ID,
            update,
            redis=mock_redis,
            db=mock_db,
        )

        assert config.llm_backend == "anthropic"
        assert _STORE[USER_ID]["llm_backend"] == "anthropic"
        mock_redis.delete.assert_awaited_once_with(CACHE_KEY)

    @pytest.mark.asyncio
    async def test_updates_multiple_fields(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Multiple fields can be updated in one call."""
        _STORE[USER_ID] = {"llm_backend": "openai", "llm_model": "gpt-4"}
        _patch_repo(monkeypatch)
        update = UpdatePlatformConfigRequest(llm_backend="anthropic", llm_model="claude-3-5-sonnet")

        config = await update_platform_config(
            USER_ID,
            update,
            redis=mock_redis,
            db=mock_db,
        )

        assert config.llm_backend == "anthropic"
        assert config.llm_model == "claude-3-5-sonnet"

    @pytest.mark.asyncio
    async def test_none_value_removes_field(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Setting a field to ``None`` removes it from the stored config."""
        _STORE[USER_ID] = {"llm_backend": "openai", "llm_model": "gpt-4"}
        _patch_repo(monkeypatch)
        update = UpdatePlatformConfigRequest(llm_backend=None)

        config = await update_platform_config(
            USER_ID,
            update,
            redis=mock_redis,
            db=mock_db,
        )

        assert config.llm_backend is None  # removed
        assert config.llm_model == "gpt-4"  # preserved
        assert "llm_backend" not in _STORE[USER_ID]

    @pytest.mark.asyncio
    async def test_shallow_merge_preserves_existing_fields(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Updating one field preserves others that weren't touched."""
        _STORE[USER_ID] = {
            "llm_backend": "openai",
            "llm_model": "gpt-4",
            "llm_temperature": 0.7,
        }
        _patch_repo(monkeypatch)
        update = UpdatePlatformConfigRequest(llm_temperature=0.9)

        config = await update_platform_config(
            USER_ID,
            update,
            redis=mock_redis,
            db=mock_db,
        )

        assert config.llm_temperature == 0.9
        assert _STORE[USER_ID]["llm_backend"] == "openai"

    @pytest.mark.asyncio
    async def test_accepts_dict_instead_of_schema(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``update_platform_config`` accepts a plain dict."""
        _STORE[USER_ID] = {"llm_backend": "openai"}
        _patch_repo(monkeypatch)

        config = await update_platform_config(
            USER_ID,
            {"llm_backend": "azure"},
            redis=mock_redis,
            db=mock_db,
        )

        assert config.llm_backend == "azure"

    @pytest.mark.asyncio
    async def test_cache_invalidation_failure_logged(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cache invalidation failure is logged but does not fail the update."""
        _STORE[USER_ID] = {"llm_backend": "openai"}
        _patch_repo(monkeypatch)
        mock_redis.delete.side_effect = ConnectionError("Redis delete failed")

        config = await update_platform_config(
            USER_ID,
            UpdatePlatformConfigRequest(llm_backend="anthropic"),
            redis=mock_redis,
            db=mock_db,
        )

        assert config.llm_backend == "anthropic"

    @pytest.mark.asyncio
    async def test_no_redis_skips_cache_invalidation(
        self,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``redis`` is ``None``, cache invalidation is skipped."""
        _STORE[USER_ID] = {"llm_backend": "openai"}
        _patch_repo(monkeypatch)

        config = await update_platform_config(
            USER_ID,
            UpdatePlatformConfigRequest(llm_backend="anthropic"),
            redis=None,
            db=mock_db,
        )

        assert config.llm_backend == "anthropic"

    @pytest.mark.asyncio
    async def test_no_db_raises(
        self,
        mock_redis: AsyncMock,
    ) -> None:
        """Updating without a DB session raises ``RuntimeError``."""
        with pytest.raises(RuntimeError, match="requires a DB session"):
            await update_platform_config(
                USER_ID,
                {"llm_backend": "ollama"},
                redis=mock_redis,
                db=None,
            )

    @pytest.mark.asyncio
    async def test_absent_keys_preserved_not_deleted(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keys omitted from the payload keep their stored values.

        Omitting a key is not deletion: absent keys never enter the
        stored payload, so unrelated keys survive the update.
        """
        _STORE[USER_ID] = {
            "llm_backend": "openai",
            "llm_model": "gpt-4",
        }
        _patch_repo(monkeypatch)

        await update_platform_config(
            USER_ID,
            UpdatePlatformConfigRequest(llm_temperature=0.9),
            redis=mock_redis,
            db=mock_db,
        )

        assert _STORE[USER_ID] == {
            "llm_backend": "openai",
            "llm_model": "gpt-4",
            "llm_temperature": 0.9,
        }


@pytest.mark.unit
class TestCacheKey:
    """``build_cache_key`` helper."""

    def test_builds_correct_key(self) -> None:
        """The cache key follows the pattern ``platform_config:<uuid>``."""
        key = build_cache_key(USER_ID)
        assert key == CACHE_KEY

    def test_consistent_across_calls(self) -> None:
        """Same user ID always produces the same key."""
        assert build_cache_key(USER_ID) == build_cache_key(USER_ID)

    def test_different_users_different_keys(self) -> None:
        """Different user IDs produce different keys."""
        other_id = UUID("00000000-0000-0000-0000-000000000002")
        assert build_cache_key(USER_ID) != build_cache_key(other_id)

    def test_cache_key_matches_module_constant(self) -> None:
        """The key starts with the module-level prefix."""
        key = build_cache_key(USER_ID)
        assert key.startswith(CACHE_KEY_PREFIX)


@pytest.mark.unit
class TestPlatformConfigConstants:
    """Module-level constants."""

    def test_cache_ttl_default(self) -> None:
        assert PLATFORM_CONFIG_CACHE_TTL == 300

    def test_cache_key_prefix(self) -> None:
        assert CACHE_KEY_PREFIX == "platform_config"


@pytest.mark.unit
class TestEnrichmentNullReset:
    """Null-reset semantics for the nested ``enrichment`` override.

    Top-level ``{"enrichment": null}`` removes the whole key; nested
    ``{"enrichment": {"<toggle>": null}}`` resets just that toggle
    (restoring its default ``True``) without touching siblings; unknown
    nested keys are a 422.
    """

    @pytest.mark.asyncio
    async def test_top_level_null_removes_enrichment_key(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """G(27): ``{"enrichment": null}`` drops the stored override."""
        _STORE[USER_ID] = {"enrichment": {"facts": False}}
        _patch_repo(monkeypatch)

        config = await update_platform_config(
            USER_ID,
            UpdatePlatformConfigRequest(enrichment=None),
            redis=mock_redis,
            db=mock_db,
        )

        assert config.enrichment is None
        assert "enrichment" not in _STORE[USER_ID]

    @pytest.mark.asyncio
    async def test_nested_null_resets_only_that_toggle(
        self,
        mock_redis: AsyncMock,
        mock_db: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """G(28): ``{"enrichment": {"facts": null}}`` resets facts only."""
        _STORE[USER_ID] = {"enrichment": {"facts": False, "entities": False}}
        _patch_repo(monkeypatch)

        config = await update_platform_config(
            USER_ID,
            UpdatePlatformConfigRequest(enrichment={"facts": None}),
            redis=mock_redis,
            db=mock_db,
        )

        assert config.enrichment == {"entities": False}
        assert _STORE[USER_ID]["enrichment"] == {"entities": False}

    def test_unknown_nested_key_422(self) -> None:
        """G(29): ``{"enrichment": {"bogus": ...}}`` fails validation."""
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError, match="Unknown enrichment keys"):
            UpdatePlatformConfigRequest(enrichment={"bogus": True})  # type: ignore[dict-item]
