"""Unit tests for worker configuration — queue naming, OpenBao bootstrap, singleton.

Tests cover:
- ``get_queue_name`` formatting and validation
- ``WorkerSettings`` model defaults and derived properties
- ``WorkerSettings.from_openbao`` factory with key mapping
- ``init_worker_settings_from_bao`` singleton initialisation
- ``get_worker_settings`` accessor with guard
- ``_SettingsProxy`` transparent forwarding
- Default values for all worker-specific fields
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# get_queue_name
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestGetQueueName:
    """``get_queue_name`` — queue name formatting and validation."""

    def test_produces_correct_format(self) -> None:
        """Returns ``OpenZync:{env}:queue:{type}``."""
        from services.worker.worker_settings import get_queue_name

        name = get_queue_name("staging", "high")
        assert name == "OpenZync:staging:queue:high"

    def test_low_queue(self) -> None:
        """Low queue type produces correct name."""
        from services.worker.worker_settings import get_queue_name

        name = get_queue_name("prod", "low")
        assert name == "OpenZync:prod:queue:low"

    def test_development_env(self) -> None:
        """Development environment works as expected."""
        from services.worker.worker_settings import get_queue_name

        name = get_queue_name("development", "high")
        assert name == "OpenZync:development:queue:high"

    def test_invalid_queue_type_raises_value_error(self) -> None:
        """Queue type other than 'high'/'low' raises ValueError."""
        from services.worker.worker_settings import get_queue_name

        with pytest.raises(ValueError, match="queue_type must be 'high' or 'low'"):
            get_queue_name("dev", "medium")

    def test_raises_on_empty_queue_type(self) -> None:
        """Empty string as queue_type raises ValueError."""
        from services.worker.worker_settings import get_queue_name

        with pytest.raises(ValueError, match="queue_type must be 'high' or 'low'"):
            get_queue_name("dev", "")


# ═══════════════════════════════════════════════════════════════════════════════
# WorkerSettings model — defaults
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestWorkerSettingsDefaults:
    """``WorkerSettings`` default values (no OpenBao)."""

    def test_minimal_instantiation(self) -> None:
        """WorkerSettings can be created with only required fields."""
        from services.worker.worker_settings import WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        assert ws.DATABASE_URL == "postgresql+asyncpg://localhost/test"
        assert ws.REDIS_URL == "redis://localhost:6379/0"

    def test_environment_default(self) -> None:
        """ENV defaults to 'development'."""
        from services.worker.worker_settings import WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        assert ws.ENV == "development"

    def test_concurrency_defaults(self) -> None:
        """MAX_WORKERS defaults to 4."""
        from services.worker.worker_settings import WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        assert ws.MAX_WORKERS == 4

    def test_job_timeout_default(self) -> None:
        """JOB_TIMEOUT_DEFAULT defaults to 300 seconds."""
        from services.worker.worker_settings import WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        assert ws.JOB_TIMEOUT_DEFAULT == 300

    def test_queue_name_defaults(self) -> None:
        """Queue name fields default to 'high' and 'low'."""
        from services.worker.worker_settings import WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        assert ws.HIGH_QUEUE_NAME == "high"
        assert ws.LOW_QUEUE_NAME == "low"

    def test_poll_delay_default(self) -> None:
        """POLL_DELAY defaults to 0.5 seconds."""
        from services.worker.worker_settings import WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        assert ws.POLL_DELAY == 0.5

    def test_prometheus_port_default(self) -> None:
        """PROMETHEUS_PORT defaults to 9095."""
        from services.worker.worker_settings import WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        assert ws.PROMETHEUS_PORT == 9095

    def test_health_port_default(self) -> None:
        """HEALTH_PORT defaults to 8081."""
        from services.worker.worker_settings import WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        assert ws.HEALTH_PORT == 8081

    def test_log_level_default(self) -> None:
        """LOG_LEVEL defaults to 'INFO'."""
        from services.worker.worker_settings import WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        assert ws.LOG_LEVEL == "INFO"

    def test_structlog_format_default(self) -> None:
        """STRUCTLOG_FORMAT defaults to 'json'."""
        from services.worker.worker_settings import WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        assert ws.STRUCTLOG_FORMAT == "json"

    def test_auto_community_detection_default(self) -> None:
        """AUTO_RUN_COMMUNITY_DETECTION defaults to False."""
        from services.worker.worker_settings import WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        assert ws.AUTO_RUN_COMMUNITY_DETECTION is False

    def test_falkordb_defaults(self) -> None:
        """FalkorDB defaults to None URL."""
        from services.worker.worker_settings import WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        assert ws.FALKORDB_URL is None
        assert ws.FALKORDB_MAX_CONNECTIONS == 10
        assert ws.FALKORDB_SOCKET_TIMEOUT == 10


# ═══════════════════════════════════════════════════════════════════════════════
# WorkerSettings — derived properties
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestWorkerSettingsProperties:
    """``WorkerSettings`` derived properties (high_queue_full, low_queue_full)."""

    def test_high_queue_full_includes_env(self) -> None:
        """``high_queue_full`` returns a namespaced queue name."""
        from services.worker.worker_settings import WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/test",
            REDIS_URL="redis://localhost:6379/0",
            ENV="prod",
        )
        assert ws.high_queue_full == "OpenZync:prod:queue:high"

    def test_low_queue_full_includes_env(self) -> None:
        """``low_queue_full`` returns a namespaced queue name."""
        from services.worker.worker_settings import WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/test",
            REDIS_URL="redis://localhost:6379/0",
            ENV="staging",
        )
        assert ws.low_queue_full == "OpenZync:staging:queue:low"

    def test_high_queue_full_includes_custom_name(self) -> None:
        """Custom ``HIGH_QUEUE_NAME`` is reflected in ``high_queue_full``."""
        from services.worker.worker_settings import WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/test",
            REDIS_URL="redis://localhost:6379/0",
            ENV="dev",
            HIGH_QUEUE_NAME="high",
        )
        assert ws.high_queue_full == "OpenZync:dev:queue:high"


# ═══════════════════════════════════════════════════════════════════════════════
# WorkerSettings.from_openbao
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestWorkerSettingsFromOpenBao:
    """``WorkerSettings.from_openbao`` factory."""

    @pytest.mark.asyncio
    async def test_reads_system_config_from_bao(self) -> None:
        """System config is fetched from OpenBao."""
        from services.worker.worker_settings import WorkerSettings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://bao-host/db",
            "OZ_REDIS_URL": "redis://bao-host:6379/0",
            "OZ_ENVIRONMENT": "prod",
            "OZ_LOG_LEVEL": "WARNING",
        }

        ws = await WorkerSettings.from_openbao(mock_bao)

        mock_bao.read_system_config.assert_awaited_once()
        assert ws.DATABASE_URL == "postgresql+asyncpg://bao-host/db"
        assert ws.REDIS_URL == "redis://bao-host:6379/0"
        assert ws.ENV == "prod"
        assert ws.LOG_LEVEL == "WARNING"

    @pytest.mark.asyncio
    async def test_converts_int_fields(self) -> None:
        """Integer fields are cast from string values."""
        from services.worker.worker_settings import WorkerSettings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://localhost/db",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
            "OZ_MAX_WORKERS": "8",
            "OZ_FALKORDB_MAX_CONNECTIONS": "15",
            "OZ_FALKORDB_SOCKET_TIMEOUT": "30",
        }

        ws = await WorkerSettings.from_openbao(mock_bao)

        assert ws.MAX_WORKERS == 8
        assert ws.FALKORDB_MAX_CONNECTIONS == 15
        assert ws.FALKORDB_SOCKET_TIMEOUT == 30

    @pytest.mark.asyncio
    async def test_handles_oz_environment_mapping(self) -> None:
        """``OZ_ENVIRONMENT`` maps to ``ENV`` field."""
        from services.worker.worker_settings import WorkerSettings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://localhost/db",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
            "OZ_ENVIRONMENT": "staging",
        }

        ws = await WorkerSettings.from_openbao(mock_bao)

        assert ws.ENV == "staging"

    @pytest.mark.asyncio
    async def test_skips_missing_keys_gracefully(self) -> None:
        """Missing optional keys are not included."""
        from services.worker.worker_settings import WorkerSettings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://localhost/db",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
        }

        ws = await WorkerSettings.from_openbao(mock_bao)

        # Missing keys use their default values
        assert ws.LOG_LEVEL == "INFO"  # default
        assert ws.MAX_WORKERS == 4  # default

    @pytest.mark.asyncio
    async def test_sets_deployment_defaults(self) -> None:
        """Deployment-specific defaults are applied when not in OpenBao."""
        from services.worker.worker_settings import WorkerSettings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://localhost/db",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
        }

        ws = await WorkerSettings.from_openbao(mock_bao)

        assert ws.JOB_TIMEOUT_DEFAULT == 300
        assert ws.HIGH_QUEUE_NAME == "high"
        assert ws.LOW_QUEUE_NAME == "low"

    @pytest.mark.asyncio
    async def test_fails_fast_on_drift(self) -> None:
        """Worker keys not in SYSTEM_KEY_MAPPING raise ValueError."""
        from services.worker.worker_settings import WorkerSettings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://localhost/db",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
        }

        with patch(
            "services.worker.worker_settings.SYSTEM_KEY_MAPPING",
            {},  # empty — no keys match
        ):
            with pytest.raises(ValueError, match="Worker keys not found"):
                await WorkerSettings.from_openbao(mock_bao)

    @pytest.mark.asyncio
    async def test_falkordb_url_sets_when_provided(self) -> None:
        """FalkorDB URL from OpenBao is stored."""
        from services.worker.worker_settings import WorkerSettings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://localhost/db",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
            "OZ_FALKORDB_URL": "redis://falkordb:6379",
        }

        ws = await WorkerSettings.from_openbao(mock_bao)

        assert ws.FALKORDB_URL == "redis://falkordb:6379"

    @pytest.mark.asyncio
    async def test_surrealdb_url_sets_when_provided(self) -> None:
        """SurrealDB URL from OpenBao is stored."""
        from services.worker.worker_settings import WorkerSettings

        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://localhost/db",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
            "OZ_SURREALDB_URL": "ws://surrealdb:8000/rpc",
        }

        ws = await WorkerSettings.from_openbao(mock_bao)

        assert ws.SURREALDB_URL == "ws://surrealdb:8000/rpc"


# ═══════════════════════════════════════════════════════════════════════════════
# Singleton management
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestWorkerSettingsSingleton:
    """``get_worker_settings`` and ``init_worker_settings_from_bao``."""

    @pytest.mark.asyncio
    async def test_init_stores_singleton(self) -> None:
        """``init_worker_settings_from_bao`` stores the instance."""
        mock_bao = AsyncMock()
        mock_bao.read_system_config.return_value = {
            "OZ_DATABASE_URL": "postgresql+asyncpg://localhost/db",
            "OZ_REDIS_URL": "redis://localhost:6379/0",
        }

        from services.worker.worker_settings import (
            get_worker_settings,
            init_worker_settings_from_bao,
        )

        ws = await init_worker_settings_from_bao(mock_bao)
        retrieved = get_worker_settings()

        assert retrieved is ws
        assert retrieved.DATABASE_URL == "postgresql+asyncpg://localhost/db"

    def test_get_raises_before_init(self) -> None:
        """``get_worker_settings`` raises RuntimeError before init."""
        from services.worker.worker_settings import get_worker_settings

        with (
            patch("services.worker.worker_settings._settings", None),
            pytest.raises(RuntimeError, match="WorkerSettings not initialised"),
        ):
            get_worker_settings()


# ═══════════════════════════════════════════════════════════════════════════════
# _SettingsProxy
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestSettingsProxy:
    """``_SettingsProxy`` transparent forwarding."""

    def test_proxy_forwards_attribute_access(self) -> None:
        """``settings.ENV`` delegates to ``get_worker_settings().ENV``."""
        from services.worker.worker_settings import _SettingsProxy, get_worker_settings

        with patch.object(get_worker_settings(), "ENV", "test-prod"):
            proxy = _SettingsProxy()
            assert proxy.ENV == "test-prod"

    def test_proxy_forwards_to_singleton(self) -> None:
        """Proxy attribute access goes through get_worker_settings()."""
        from services.worker.worker_settings import _SettingsProxy, WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/db",
            REDIS_URL="redis://localhost:6379/0",
            MAX_WORKERS=12,
        )

        with patch(
            "services.worker.worker_settings.get_worker_settings",
            return_value=ws,
        ):
            proxy = _SettingsProxy()
            assert proxy.MAX_WORKERS == 12
            assert proxy.DATABASE_URL == "postgresql+asyncpg://localhost/db"

    def test_proxy_raises_on_unknown_attribute(self) -> None:
        """Unknown attribute raises AttributeError."""
        from services.worker.worker_settings import _SettingsProxy, WorkerSettings

        ws = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost/db",
            REDIS_URL="redis://localhost:6379/0",
        )

        with patch(
            "services.worker.worker_settings.get_worker_settings",
            return_value=ws,
        ):
            proxy = _SettingsProxy()
            with pytest.raises(AttributeError):
                _ = proxy.nonexistent_field  # noqa


# ═══════════════════════════════════════════════════════════════════════════════
# Module-level settings proxy
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestModuleSettings:
    """Module-level ``settings`` proxy import."""

    def test_settings_is_proxy_instance(self) -> None:
        """``settings`` is a ``_SettingsProxy``, not a ``WorkerSettings``."""
        from services.worker.worker_settings import _SettingsProxy, settings

        assert isinstance(settings, _SettingsProxy)

    def test_settings_requires_init_before_access(self) -> None:
        """Accessing ``settings.*`` before init raises RuntimeError."""
        from services.worker.worker_settings import settings

        with (
            patch("services.worker.worker_settings._settings", None),
            pytest.raises(RuntimeError),
        ):
            _ = settings.ENV  # noqa
