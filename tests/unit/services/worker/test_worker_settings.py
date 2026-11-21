"""Unit tests for worker configuration — queue naming, env bootstrap, singleton.

Tests cover:
- ``get_queue_name`` formatting and validation
- ``WorkerSettings`` model defaults and derived properties
- ``WorkerSettings.from_env`` factory reading ``OZ_*`` env vars
- ``init_worker_settings`` singleton initialisation
- ``get_worker_settings`` accessor with guard
- ``_SettingsProxy`` transparent forwarding
- Default values for all worker-specific fields
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_worker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defeat ``.env`` + real-env leakage for ``WorkerSettings`` construction.

    ``WorkerSettings`` reads ``OZ_*`` vars (and the repo ``.env`` by
    default).  Unit tests see explicit kwargs / ``setenv`` values only.
    """
    from services.worker.worker_settings import WorkerSettings

    monkeypatch.setitem(WorkerSettings.model_config, "env_file", "/dev/null")
    for var in list(os.environ):
        if var.startswith("OZ_"):
            monkeypatch.delenv(var, raising=False)

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
    """``WorkerSettings`` default values (explicit required kwargs only)."""

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
# WorkerSettings.from_env
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestWorkerSettingsFromEnv:
    """``WorkerSettings.from_env`` factory (``OZ_*`` env vars, no .env)."""

    def _seed_env(self, monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
        """Set the required DB/Redis env vars plus any extras."""
        monkeypatch.setenv("OZ_DATABASE_URL", "postgresql+asyncpg://localhost/db")
        monkeypatch.setenv("OZ_REDIS_URL", "redis://localhost:6379/0")
        for key, value in extra.items():
            monkeypatch.setenv(key, value)

    def test_reads_required_config_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DB/Redis URLs come from the environment."""
        from services.worker.worker_settings import WorkerSettings

        self._seed_env(
            monkeypatch,
            OZ_ENVIRONMENT="prod",
            OZ_LOG_LEVEL="WARNING",
        )

        ws = WorkerSettings.from_env()

        assert ws.DATABASE_URL == "postgresql+asyncpg://localhost/db"
        assert ws.REDIS_URL == "redis://localhost:6379/0"
        assert ws.ENV == "prod"
        assert ws.LOG_LEVEL == "WARNING"

    def test_converts_int_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Integer fields are cast from string values."""
        from services.worker.worker_settings import WorkerSettings

        self._seed_env(
            monkeypatch,
            OZ_MAX_WORKERS="8",
            OZ_FALKORDB_MAX_CONNECTIONS="15",
            OZ_FALKORDB_SOCKET_TIMEOUT="30",
        )

        ws = WorkerSettings.from_env()

        assert ws.MAX_WORKERS == 8
        assert ws.FALKORDB_MAX_CONNECTIONS == 15
        assert ws.FALKORDB_SOCKET_TIMEOUT == 30

    def test_handles_oz_environment_mapping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``OZ_ENVIRONMENT`` maps to the ``ENV`` field."""
        from services.worker.worker_settings import WorkerSettings

        self._seed_env(monkeypatch, OZ_ENVIRONMENT="staging")

        ws = WorkerSettings.from_env()

        assert ws.ENV == "staging"

    def test_skips_missing_keys_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing optional keys fall back to their defaults."""
        from services.worker.worker_settings import WorkerSettings

        self._seed_env(monkeypatch)

        ws = WorkerSettings.from_env()

        # Missing keys use their default values
        assert ws.LOG_LEVEL == "INFO"  # default
        assert ws.MAX_WORKERS == 4  # default

    def test_sets_deployment_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Queue/job defaults apply when unset in the environment."""
        from services.worker.worker_settings import WorkerSettings

        self._seed_env(monkeypatch)

        ws = WorkerSettings.from_env()

        assert ws.JOB_TIMEOUT_DEFAULT == 300
        assert ws.HIGH_QUEUE_NAME == "high"
        assert ws.LOW_QUEUE_NAME == "low"

    def test_falkordb_url_sets_when_provided(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FalkorDB URL from the environment is stored."""
        from services.worker.worker_settings import WorkerSettings

        self._seed_env(monkeypatch, OZ_FALKORDB_URL="redis://falkordb:6379")

        ws = WorkerSettings.from_env()

        assert ws.FALKORDB_URL == "redis://falkordb:6379"

    def test_surrealdb_url_sets_when_provided(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SurrealDB URL from the environment is stored."""
        from services.worker.worker_settings import WorkerSettings

        self._seed_env(monkeypatch, OZ_SURREALDB_URL="ws://surrealdb:8000/rpc")

        ws = WorkerSettings.from_env()

        assert ws.SURREALDB_URL == "ws://surrealdb:8000/rpc"


# ═══════════════════════════════════════════════════════════════════════════════
# Singleton management
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestWorkerSettingsSingleton:
    """``get_worker_settings`` and ``init_worker_settings``."""

    def test_init_stores_singleton(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``init_worker_settings`` reads env and stores the instance."""
        monkeypatch.setenv("OZ_DATABASE_URL", "postgresql+asyncpg://localhost/db")
        monkeypatch.setenv("OZ_REDIS_URL", "redis://localhost:6379/0")

        from services.worker.worker_settings import (
            get_worker_settings,
            init_worker_settings,
        )

        ws = init_worker_settings()
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
        from services.worker.worker_settings import WorkerSettings, _SettingsProxy

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
        from services.worker.worker_settings import WorkerSettings, _SettingsProxy

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
