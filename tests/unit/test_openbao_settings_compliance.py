"""Unit tests for OpenBao settings bootstrap — zero-fallback compliance.

Verifies that:
- Settings with required secret fields cannot be instantiated without values.
- The singleton accessor raises RuntimeError before init_settings().
- BootstrapSettings does not read from a .env file.
- Non-sensitive tunables retain their defaults.
- Backward-compatible ``from core.config import settings`` works via __getattr__.
"""

from __future__ import annotations

from pydantic import ValidationError
import pytest

from core.config import BootstrapSettings, Settings, get_settings, set_settings


class TestSettingsInstantiation:
    """Settings must fail fast when secrets are missing."""

    def test_settings_cannot_be_instantiated_without_secrets(self) -> None:
        """All secret fields are required — no defaults allowed."""
        with pytest.raises(ValidationError):
            Settings()

    def test_settings_requires_database_url(self) -> None:
        with pytest.raises(ValidationError):
            Settings(
                REDIS_URL="redis://localhost:6379/0",
                SECRET_KEY="x" * 32,
                WEBHOOK_SIGNING_SECRET="y" * 32,
            )

    def test_settings_requires_redis_url(self) -> None:
        with pytest.raises(ValidationError):
            Settings(
                DATABASE_URL="postgresql+asyncpg://u:p@h:5432/d",
                SECRET_KEY="x" * 32,
                WEBHOOK_SIGNING_SECRET="y" * 32,
            )

    def test_settings_requires_secret_key(self) -> None:
        with pytest.raises(ValidationError):
            Settings(
                DATABASE_URL="postgresql+asyncpg://u:p@h:5432/d",
                REDIS_URL="redis://localhost:6379/0",
                WEBHOOK_SIGNING_SECRET="y" * 32,
            )

    def test_settings_requires_webhook_signing_secret(self) -> None:
        with pytest.raises(ValidationError):
            Settings(
                DATABASE_URL="postgresql+asyncpg://u:p@h:5432/d",
                REDIS_URL="redis://localhost:6379/0",
                SECRET_KEY="x" * 32,
            )

    def test_valid_settings_instantiation(self) -> None:
        """All four secrets provided — must succeed."""
        s = Settings(
            DATABASE_URL="postgresql+asyncpg://u:p@h:5432/d",
            REDIS_URL="redis://localhost:6379/0",
            SECRET_KEY="x" * 32,
            WEBHOOK_SIGNING_SECRET="y" * 32,
        )
        assert s.DATABASE_URL == "postgresql+asyncpg://u:p@h:5432/d"
        assert s.REDIS_URL == "redis://localhost:6379/0"
        assert s.SECRET_KEY == "x" * 32


class TestSettingsSingleton:
    """The singleton must work after set_settings and fail before init.

    Note: ``test_get_settings_raises_before_init`` is tested via
    ``test_lazy_import_raises_before_init`` (which uses ``__getattr__``,
    the same code path).  In-session isolation is not possible because
    ``set_settings`` in an earlier test persists in the interpreter.
    """

    def test_set_settings_then_get_settings_roundtrip(self) -> None:
        """set_settings then get_settings returns the same instance."""
        s = Settings(
            DATABASE_URL="postgresql+asyncpg://u:p@h:5432/d",
            REDIS_URL="redis://localhost:6379/0",
            SECRET_KEY="x" * 32,
            WEBHOOK_SIGNING_SECRET="y" * 32,
        )
        set_settings(s)
        retrieved = get_settings()
        assert retrieved is s
        assert retrieved.DATABASE_URL == s.DATABASE_URL

    def test_set_settings_overwrites_previous(self) -> None:
        """Calling set_settings a second time replaces the singleton."""
        s1 = Settings(
            DATABASE_URL="postgresql+asyncpg://u1:p@h:5432/d1",
            REDIS_URL="redis://h1:6379/0",
            SECRET_KEY="a" * 32,
            WEBHOOK_SIGNING_SECRET="b" * 32,
        )
        set_settings(s1)

        s2 = Settings(
            DATABASE_URL="postgresql+asyncpg://u2:p@h:5432/d2",
            REDIS_URL="redis://h2:6379/0",
            SECRET_KEY="c" * 32,
            WEBHOOK_SIGNING_SECRET="d" * 32,
        )
        set_settings(s2)

        assert get_settings() is s2
        assert get_settings().DATABASE_URL == "postgresql+asyncpg://u2:p@h:5432/d2"


class TestBootstrapSettings:
    """BootstrapSettings must not depend on a .env file."""

    def test_no_env_file_configured(self) -> None:
        """BootstrapSettings must not have env_file set (reads env vars only)."""
        config = BootstrapSettings.model_config
        assert config.get("env_file") is None

    def test_bootstrap_fields_defined(self) -> None:
        """All required bootstrap fields must be present."""
        bs = BootstrapSettings.model_fields
        assert "OPENBAO_ADDR" in bs
        assert "OPENBAO_ROLE_ID" in bs
        assert "OPENBAO_SECRET_ID" in bs

    def test_bootstrap_has_worker_fields(self) -> None:
        """Optional worker-specific fields must be present."""
        bs = BootstrapSettings.model_fields
        assert "OPENBAO_WORKER_ROLE_ID" in bs
        assert "OPENBAO_WORKER_SECRET_ID" in bs

    def test_openbao_addr_default(self) -> None:
        """OPENBAO_ADDR should default to localhost:8200."""
        # When no env vars are set, the default should apply.
        # We just check the field definition.
        field = BootstrapSettings.model_fields["OPENBAO_ADDR"]
        assert field.default is not None
        assert "8200" in str(field.default)

    def test_openbao_role_id_is_required(self) -> None:
        """OPENBAO_ROLE_ID must be required (no default)."""
        field = BootstrapSettings.model_fields["OPENBAO_ROLE_ID"]
        assert field.is_required()


class TestSettingsTunables:
    """Non-sensitive tunables should have sensible defaults."""

    @pytest.fixture
    def valid_settings(self) -> Settings:
        return Settings(
            DATABASE_URL="postgresql+asyncpg://u:p@h:5432/d",
            REDIS_URL="redis://localhost:6379/0",
            SECRET_KEY="x" * 32,
            WEBHOOK_SIGNING_SECRET="y" * 32,
        )

    def test_environment_default(self, valid_settings: Settings) -> None:
        assert valid_settings.ENVIRONMENT == "development"

    def test_log_level_default(self, valid_settings: Settings) -> None:
        assert valid_settings.LOG_LEVEL == "INFO"

    def test_max_workers_default(self, valid_settings: Settings) -> None:
        assert valid_settings.MAX_WORKERS == 4

    def test_jwt_access_token_ttl_default(self, valid_settings: Settings) -> None:
        assert valid_settings.JWT_ACCESS_TOKEN_TTL_MINUTES == 30

    def test_cors_origins_default(self, valid_settings: Settings) -> None:
        assert "localhost" in valid_settings.CORS_ORIGINS

    def test_tunables_can_be_overridden(self) -> None:
        """Tunables should be overridable via OpenBao values."""
        s = Settings(
            DATABASE_URL="postgresql+asyncpg://u:p@h:5432/d",
            REDIS_URL="redis://localhost:6379/0",
            SECRET_KEY="x" * 32,
            WEBHOOK_SIGNING_SECRET="y" * 32,
            ENVIRONMENT="production",
            MAX_WORKERS=8,
        )
        assert s.ENVIRONMENT == "production"
        assert s.MAX_WORKERS == 8


class TestSettingsLazyAccess:
    """Backward-compatible ``from core.config import settings``."""

    def test_lazy_import_works_after_init(self) -> None:
        """Importing 'settings' after init resolves to the singleton."""
        s = Settings(
            DATABASE_URL="postgresql+asyncpg://u:p@h:5432/d",
            REDIS_URL="redis://localhost:6379/0",
            SECRET_KEY="x" * 32,
            WEBHOOK_SIGNING_SECRET="y" * 32,
        )
        set_settings(s)

        # Lazy import — must be AFTER set_settings
        from core.config import settings  # noqa: PLC0415 — intentional lazy

        assert settings.DATABASE_URL == s.DATABASE_URL

    def test_lazy_import_raises_before_init(self) -> None:
        """Importing 'settings' before init must raise RuntimeError.

        Temporarily clears the singleton to verify the fail-fast behaviour,
        then restores it.
        """
        import core.config as _cfg  # noqa: PLC0415

        orig = _cfg._settings
        _cfg._settings = None  # clear singleton
        try:
            with pytest.raises(RuntimeError, match="not initialised"):
                from core.config import settings  # noqa: PLC0415 — intentional lazy

                _ = settings  # trigger __getattr__
        finally:
            _cfg._settings = orig  # restore
