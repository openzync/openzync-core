"""Unit tests for core.system_config — the platform config whitelist.

Covers:
- whitelist: a secret key in the update body is rejected (422 at schema
  level, ValidationError at the core level for plain dicts).
- defaults: empty OpenBao record → all None (registration mode lives in
  ``instance_settings`` now).
- cache: read populates the cache; update invalidates it and re-reads
  fresh from OpenBao.
- secrets are never part of the response.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from core.exceptions import ValidationError as AppValidationError
from core.system_config import get_system_config, update_system_config
from schemas.system_config import (
    SystemConfigResponse,
    SystemConfigUpdate,
)

pytestmark = pytest.mark.unit


class TestSystemConfigSchema:
    """Schema-level whitelist enforcement."""

    def test_secret_key_in_update_body_rejected_422(self) -> None:
        """A secret key (openai_api_key) fails schema validation (→ 422)."""
        with pytest.raises(ValidationError):
            SystemConfigUpdate(llm_model="x", openai_api_key="sk-123")  # type: ignore[call-arg]

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
        """SystemConfigResponse defaults to all None."""
        cfg = SystemConfigResponse()
        assert cfg.llm_backend is None
        assert cfg.llm_model is None
        assert cfg.graph_backend is None


class TestGetSystemConfig:
    """get_system_config resolution + caching."""

    @pytest.mark.asyncio
    async def test_defaults_when_openbao_empty(self) -> None:
        """No OpenBao record → all-None defaults."""
        redis = AsyncMock()
        bao = AsyncMock()
        bao.read_system_config.return_value = {}

        cfg = await get_system_config(redis, bao)

        assert cfg.llm_backend is None
        assert cfg.llm_model is None
        assert cfg.graph_backend is None

    @pytest.mark.asyncio
    async def test_whitelisted_keys_returned_secrets_never(self) -> None:
        """Only whitelisted keys are read out of the raw OpenBao payload."""
        redis = AsyncMock()
        bao = AsyncMock()
        bao.read_system_config.return_value = {
            "llm_backend": "openai",
            "llm_model": "gpt-4o-mini",
            "OZ_DATABASE_URL": "postgresql://secret",
            "openai_api_key": "sk-secret",
            "SMTP_PASSWORD": "smtp-secret",
        }

        cfg = await get_system_config(redis, bao)

        assert cfg.llm_backend == "openai"
        assert cfg.llm_model == "gpt-4o-mini"
        # Secrets never surface in the response model — by construction.
        assert "openai_api_key" not in SystemConfigResponse.model_fields
        assert "OZ_DATABASE_URL" not in cfg.model_dump()
        assert "SMTP_PASSWORD" not in cfg.model_dump()

    @pytest.mark.asyncio
    async def test_cache_hit_skips_openbao(self) -> None:
        """A warm cache short-circuits the OpenBao read."""
        redis = AsyncMock()
        redis.get.return_value = SystemConfigResponse(
            llm_backend="openai",
            graph_backend="falkordb",
        ).model_dump_json()
        bao = AsyncMock()

        cfg = await get_system_config(redis, bao)

        assert cfg.llm_backend == "openai"
        bao.read_system_config.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_miss_populates_cache(self) -> None:
        """A cache miss populates the cache with the whitelisted config."""
        redis = AsyncMock()
        redis.get.return_value = None
        bao = AsyncMock()
        bao.read_system_config.return_value = {"llm_model": "gpt-4o-mini"}

        await get_system_config(redis, bao)

        redis.setex.assert_awaited_once()
        cache_key, ttl, payload = redis.setex.call_args.args
        assert cache_key == "system_config"
        assert ttl == 300
        assert "gpt-4o-mini" in payload
        # The raw OpenBao payload never lands in the cache verbatim.
        assert "OZ_" not in payload


class TestUpdateSystemConfig:
    """update_system_config merge + invalidation."""

    @pytest.mark.asyncio
    async def test_update_merges_invalidates_and_returns_fresh(self) -> None:
        """Update writes the merged config, invalidates cache, re-reads."""
        redis = AsyncMock()
        bao = AsyncMock()
        # Existing system secret (with unrelated OZ_ keys to merge over)
        bao.read_system_config.return_value = {
            "OZ_SECRET_KEY": "keep-me",  # noqa: S105 — test fixture secret
            "llm_model": "old-model",
        }

        with patch(
            "core.system_config.get_system_config",
            new=AsyncMock(
                return_value=SystemConfigResponse(
                    llm_backend="openai",
                    llm_model="gpt-4o-mini",
                ),
            ),
        ) as mock_get:
            result = await update_system_config(
                SystemConfigUpdate(
                    llm_backend="openai",
                    llm_model="gpt-4o-mini",
                ),
                bao,
                redis,
            )

        assert result.llm_backend == "openai"
        # Merge preserved the unrelated OZ_ key
        written = bao.write_system_config.call_args.args[0]
        assert written["OZ_SECRET_KEY"] == "keep-me"  # noqa: S105 — test fixture secret
        assert written["llm_model"] == "gpt-4o-mini"
        redis.delete.assert_awaited_once_with("system_config")
        mock_get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_rejects_secret_key_from_plain_dict(self) -> None:
        """A plain-dict update with a non-whitelisted key → ValidationError."""
        bao = AsyncMock()

        with pytest.raises(AppValidationError):
            await update_system_config(
                {"openai_api_key": "sk-123"},
                bao,
            )

        bao.write_system_config.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_none_values_remove_keys(self) -> None:
        """A None value removes the key from the stored config."""
        redis = AsyncMock()
        bao = AsyncMock()
        bao.read_system_config.return_value = {"llm_model": "gpt-4o-mini"}

        with patch(
            "core.system_config.get_system_config",
            new=AsyncMock(return_value=SystemConfigResponse()),
        ):
            await update_system_config(
                SystemConfigUpdate(llm_model=None),
                bao,
                redis,
            )

        written = bao.write_system_config.call_args.args[0]
        assert "llm_model" not in written
