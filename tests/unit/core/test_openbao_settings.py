"""Unit tests for ``init_settings`` — loading system config from OpenBao.

Covers:
- All expected config keys loaded from OpenBao KV
- Integer fields cast from string → int
- Missing optional keys → defaults used
- Missing required keys → ``ValidationError`` from pydantic
- Overwriting existing env vars
- ``init_settings`` returns and stores the singleton
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError as PydanticValidationError

from core.openbao import SYSTEM_KEY_MAPPING


@pytest.mark.unit
class TestInitSettings:
    """``init_settings`` reads system config from OpenBao and populates ``Settings``."""

    @pytest.mark.asyncio
    async def test_loads_all_required_keys(self) -> None:
        """All mandatory settings are loaded from OpenBao KV."""
        from core.openbao_settings import init_settings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/oz",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
            "OZ_SECRET_KEY": "a" * 32,
            "OZ_WEBHOOK_SIGNING_SECRET": "b" * 32,
            "OZ_ENVIRONMENT": "production",
            "OZ_LOG_LEVEL": "DEBUG",
            "OZ_CORS_ORIGINS": "https://app.openzync.tech",
            "OZ_HOSTS_ALLOWED": "api.openzync.tech",
            "OZ_MAX_WORKERS": "8",
            "OZ_JWT_ACCESS_TOKEN_TTL_MINUTES": "15",
            "OZ_JWT_REFRESH_TOKEN_TTL_DAYS": "30",
            "OZ_PROMETHEUS_URL": "http://prometheus:9090",
        }

        settings = await init_settings(mock_bao)

        assert settings.DATABASE_URL == "postgresql+asyncpg://u:p@localhost:5432/oz"
        assert settings.REDIS_URL == "redis://localhost:6379/0"
        assert settings.SECRET_KEY == "a" * 32
        assert settings.WEBHOOK_SIGNING_SECRET == "b" * 32
        assert settings.ENVIRONMENT == "production"
        assert settings.LOG_LEVEL == "DEBUG"
        assert settings.CORS_ORIGINS == "https://app.openzync.tech"
        assert settings.HOSTS_ALLOWED == "api.openzync.tech"
        assert settings.MAX_WORKERS == 8  # int
        assert settings.JWT_ACCESS_TOKEN_TTL_MINUTES == 15  # int
        assert settings.JWT_REFRESH_TOKEN_TTL_DAYS == 30  # int
        assert settings.PROMETHEUS_URL == "http://prometheus:9090"

    @pytest.mark.asyncio
    async def test_integer_fields_are_cast(self) -> None:
        """Integer-typed fields are cast from string to int."""
        from core.openbao_settings import init_settings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/oz",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
            "OZ_SECRET_KEY": "a" * 32,
            "OZ_WEBHOOK_SIGNING_SECRET": "b" * 32,
            "OZ_MAX_WORKERS": "12",
            "OZ_JWT_ACCESS_TOKEN_TTL_MINUTES": "60",
            "OZ_JWT_REFRESH_TOKEN_TTL_DAYS": "14",
            "OZ_FALKORDB_MAX_CONNECTIONS": "50",
            "OZ_FALKORDB_SOCKET_TIMEOUT": "15",
            "OZ_RATE_LIMIT_IP_MAX": "100",
            "OZ_RATE_LIMIT_WINDOW_SEC": "120",
            "OZ_SMTP_PORT": "465",
        }

        settings = await init_settings(mock_bao)

        assert settings.MAX_WORKERS == 12
        assert settings.JWT_ACCESS_TOKEN_TTL_MINUTES == 60
        assert settings.JWT_REFRESH_TOKEN_TTL_DAYS == 14
        assert settings.FALKORDB_MAX_CONNECTIONS == 50
        assert settings.FALKORDB_SOCKET_TIMEOUT == 15
        assert settings.RATE_LIMIT_IP_MAX == 100
        assert settings.RATE_LIMIT_WINDOW_SEC == 120
        assert settings.SMTP_PORT == 465

    @pytest.mark.asyncio
    async def test_missing_optional_keys_use_defaults(self) -> None:
        """Optional settings that are not in OpenBao fall back to pydantic defaults."""
        from core.openbao_settings import init_settings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/oz",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
            "OZ_SECRET_KEY": "a" * 32,
            "OZ_WEBHOOK_SIGNING_SECRET": "b" * 32,
            # Intentionally omitting ENVIRONMENT, LOG_LEVEL, MAX_WORKERS, etc.
        }

        settings = await init_settings(mock_bao)

        assert settings.ENVIRONMENT == "development"
        assert settings.LOG_LEVEL == "INFO"
        assert settings.MAX_WORKERS == 4
        assert settings.JWT_ACCESS_TOKEN_TTL_MINUTES == 30
        assert settings.JWT_REFRESH_TOKEN_TTL_DAYS == 7
        assert settings.CORS_ORIGINS == "http://localhost:3000"
        assert settings.PROMETHEUS_URL == "http://localhost:9090"

    @pytest.mark.asyncio
    async def test_missing_required_keys_raises_validation_error(self) -> None:
        """When a required field (e.g. DATABASE_URL) is missing, ``init_settings`` raises."""
        from core.openbao_settings import init_settings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            # No DATABASE_URL
            "OZ_REDIS_URL": "redis://localhost:6379/0",
            "OZ_SECRET_KEY": "a" * 32,
            "OZ_WEBHOOK_SIGNING_SECRET": "b" * 32,
        }

        with pytest.raises(PydanticValidationError):
            await init_settings(mock_bao)

    @pytest.mark.asyncio
    async def test_missing_secret_key_raises_validation_error(self) -> None:
        """Missing SECRET_KEY raises ``ValidationError``."""
        from core.openbao_settings import init_settings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/oz",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
            "OZ_WEBHOOK_SIGNING_SECRET": "b" * 32,
        }

        with pytest.raises(PydanticValidationError):
            await init_settings(mock_bao)

    @pytest.mark.asyncio
    async def test_missing_webhook_signing_secret_raises_validation_error(self) -> None:
        """Missing WEBHOOK_SIGNING_SECRET raises ``ValidationError``."""
        from core.openbao_settings import init_settings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/oz",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
            "OZ_SECRET_KEY": "a" * 32,
        }

        with pytest.raises(PydanticValidationError):
            await init_settings(mock_bao)

    @pytest.mark.asyncio
    async def test_empty_openbao_config_raises_validation_error(self) -> None:
        """An empty config dict from OpenBao causes validation to fail on required keys."""
        from core.openbao_settings import init_settings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {}

        with pytest.raises(PydanticValidationError):
            await init_settings(mock_bao)

    @pytest.mark.asyncio
    async def test_singleton_is_set(self) -> None:
        """After ``init_settings``, ``get_settings()`` returns the instance."""
        from core.config import get_settings, set_settings
        from core.openbao_settings import init_settings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/oz",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
            "OZ_SECRET_KEY": "a" * 32,
            "OZ_WEBHOOK_SIGNING_SECRET": "b" * 32,
        }

        settings = await init_settings(mock_bao)
        assert get_settings() is settings

    @pytest.mark.asyncio
    async def test_overwrites_existing_singleton(self) -> None:
        """Calling ``init_settings`` a second time replaces the singleton."""
        from core.config import get_settings, set_settings
        from core.openbao_settings import init_settings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/oz",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
            "OZ_SECRET_KEY": "a" * 32,
            "OZ_WEBHOOK_SIGNING_SECRET": "b" * 32,
            "OZ_ENVIRONMENT": "staging",
        }

        settings = await init_settings(mock_bao)
        assert get_settings().ENVIRONMENT == "staging"

    @pytest.mark.asyncio
    async def test_read_system_config_is_called(self) -> None:
        """``init_settings`` calls ``read_system_config`` once."""
        from core.openbao_settings import init_settings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/oz",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
            "OZ_SECRET_KEY": "a" * 32,
            "OZ_WEBHOOK_SIGNING_SECRET": "b" * 32,
        }

        await init_settings(mock_bao)
        mock_bao.read_system_config.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_none_values_in_integer_fields_are_cast_from_string(self) -> None:
        """A string value for an int field is cast correctly."""
        from core.openbao_settings import init_settings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/oz",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
            "OZ_SECRET_KEY": "a" * 32,
            "OZ_WEBHOOK_SIGNING_SECRET": "b" * 32,
            "OZ_MAX_WORKERS": "8",
        }

        settings = await init_settings(mock_bao)
        assert settings.MAX_WORKERS == 8

    @pytest.mark.asyncio
    async def test_int_field_with_none_still_assigned_to_pydantic(self) -> None:
        """If OpenBao stores ``null`` for an int field, pydantic validates it and raises."""
        from core.openbao_settings import init_settings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/oz",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
            "OZ_SECRET_KEY": "a" * 32,
            "OZ_WEBHOOK_SIGNING_SECRET": "b" * 32,
            "OZ_MAX_WORKERS": None,
        }

        with pytest.raises(PydanticValidationError):
            await init_settings(mock_bao)

    @pytest.mark.asyncio
    async def test_key_mapping_only_processes_known_keys(self) -> None:
        """Unknown keys in the OpenBao response are silently ignored."""
        from core.openbao_settings import init_settings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/oz",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
            "OZ_SECRET_KEY": "a" * 32,
            "OZ_WEBHOOK_SIGNING_SECRET": "b" * 32,
            "OZ_UNKNOWN_KEY": "should-be-ignored",
            "SOME_RANDOM_KEY": "also-ignored",
        }

        settings = await init_settings(mock_bao)
        # Should not raise — unknown keys are dropped by the mapping
        assert settings.DATABASE_URL == "postgresql+asyncpg://u:p@localhost:5432/oz"

    @pytest.mark.asyncio
    async def test_openbao_connection_error_propagates(self) -> None:
        """If OpenBao is unreachable, the connection error propagates."""
        from core.openbao_exceptions import OpenBaoConnectionError
        from core.openbao_settings import init_settings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.side_effect = OpenBaoConnectionError("Unreachable")

        with pytest.raises(OpenBaoConnectionError, match="Unreachable"):
            await init_settings(mock_bao)
