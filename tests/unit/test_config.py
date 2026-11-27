"""Unit tests for ``core.config.Settings``.

These tests validate that the pydantic-settings model loads from ``OZ_*``
env vars, enforces constraints like ``SECRET_KEY min_length``, and that
the singleton accessors (``set_settings`` / ``get_settings``) work as
expected.

.. note::

    ``Settings`` is a :class:`pydantic_settings.BaseSettings`: every field
    binds to its ``OZ_*`` env var via ``validation_alias``.  Required
    fields (``DATABASE_URL``, ``REDIS_URL``, ``SECRET_KEY``,
    ``S3_ENDPOINT_URL``, ``S3_ACCESS_KEY_ID``, ``S3_SECRET_ACCESS_KEY``)
    have no defaults — the process fails fast at startup when they are
    missing.

    LLM / Embedding / Graph / Behaviour settings were removed from this
    class in favour of per-user DB config.  See
    ``schemas/organization_config.PlatformConfigBase``.
"""

from __future__ import annotations

import pytest

from core.config import Settings, get_settings, set_settings

# ── Helpers ───────────────────────────────────────────────────────────────────

_DEFAULT_REQUIRED: dict[str, str] = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/openzync_test",
    "REDIS_URL": "redis://localhost:6379/1",
    "SECRET_KEY": "a" * 32,
    # ⚠️ BREAKING (blob org→env migration): global S3 backend is required —
    # per-org blob storage was removed, Settings fails fast without these.
    "S3_ENDPOINT_URL": "http://minio:9000",
    "S3_ACCESS_KEY_ID": "test-key",
    "S3_SECRET_ACCESS_KEY": "test-secret",
}


def _settings(**overrides: str) -> Settings:
    """Instantiate ``Settings`` with required fields + optional overrides.

    Required fields are always filled.  Callers pass overrides for the
    specific field they want to test.
    """
    kwargs = {**_DEFAULT_REQUIRED, **overrides}
    return Settings(**kwargs)


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestSettings:
    """Validate the core Settings model."""

    def test_defaults_are_sane(self) -> None:
        """Fields with defaults should match expected development values."""
        s = _settings()
        assert s.ENVIRONMENT == "development"
        assert s.LOG_LEVEL == "INFO"

    def test_constructor_override(self) -> None:
        """Settings should accept explicit constructor kwargs."""
        s = _settings(ENVIRONMENT="staging", LOG_LEVEL="DEBUG")
        assert s.ENVIRONMENT == "staging"
        assert s.LOG_LEVEL == "DEBUG"

    def test_database_url_accepts_postgres_dsn(self) -> None:
        """DATABASE_URL must contain 'postgresql'."""
        s = _settings()
        url = str(s.DATABASE_URL)
        assert "postgresql" in url, f"Expected postgresql DSN, got {url!r}"

    def test_secret_key_min_length_enforced(self) -> None:
        """A SECRET_KEY shorter than 32 characters should raise a validation error."""
        with pytest.raises(Exception, match="at least 32 characters"):
            _settings(SECRET_KEY="too-short")

    def test_rate_limit_defaults(self) -> None:
        """Rate-limit configuration should have sensible defaults."""
        s = _settings()
        assert s.RATE_LIMIT_IP_MAX >= 1
        assert s.RATE_LIMIT_WINDOW_SEC >= 1

    def test_cors_origins_default(self) -> None:
        """Default CORS origin should point to the local dev server."""
        s = _settings()
        assert "localhost:3000" in s.CORS_ORIGINS

    def test_settings_allows_mutation(self) -> None:
        """Settings instances are **not** frozen — assignment is allowed.

        The :class:`Settings` singleton is created once at startup and
        exposed read-only through :func:`get_settings`, but the model
        itself does not enforce ``frozen=True``.  Mutation of a local
        instance is permitted.
        """
        s = _settings()
        s.ENVIRONMENT = "production"  # should not raise
        assert s.ENVIRONMENT == "production"

    def test_required_fields_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Creating Settings without required fields (or env) should raise."""
        # BaseSettings falls back to ``OZ_*`` env vars AND the ``.env``
        # file — defeat both so the "missing" case is actually exercised.
        monkeypatch.setitem(Settings.model_config, "env_file", "/dev/null")
        for var in ("OZ_DATABASE_URL", "OZ_REDIS_URL", "OZ_SECRET_KEY"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(Exception, match="DATABASE_URL"):
            Settings(
                REDIS_URL="redis://localhost:6379/1",
                SECRET_KEY="a" * 32,
            )

    def test_env_binding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``OZ_*`` env vars populate the matching fields."""
        monkeypatch.setenv("OZ_ENVIRONMENT", "staging")
        monkeypatch.setenv("OZ_LOG_LEVEL", "DEBUG")
        s = _settings()
        assert s.ENVIRONMENT == "staging"
        assert s.LOG_LEVEL == "DEBUG"


class TestSettingsSingleton:
    """Validate ``set_settings`` / ``get_settings`` singleton accessors."""

    # ═══════════════════════════════════════════════════════════════════
    # IMPORTANT: These tests explicitly manipulate the module-level
    # ``_settings`` singleton.  The session-scoped autouse fixture in
    # ``conftest.py`` pre-initialises the singleton with dummy values.
    # These tests reset it before/after to avoid interfering with other
    # tests in the session.
    # ═══════════════════════════════════════════════════════════════════

    @pytest.fixture(autouse=True)
    def _reset_singleton(self) -> None:
        """Reset the singleton before and after each test in this class."""
        import core.config as cfg

        cfg._settings = None  # noqa: SLF001  — intentional for testing
        yield
        cfg._settings = None  # noqa: SLF001  — restore for session

    def test_get_settings_lazy_creates_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``get_settings()`` builds the singleton from env on first call."""
        monkeypatch.setenv(
            "OZ_DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/test"
        )
        monkeypatch.setenv("OZ_REDIS_URL", "redis://localhost:6379/1")
        monkeypatch.setenv("OZ_SECRET_KEY", "a" * 32)
        monkeypatch.setenv("OZ_S3_ENDPOINT_URL", "http://minio:9000")
        monkeypatch.setenv("OZ_S3_ACCESS_KEY_ID", "test-key")
        monkeypatch.setenv("OZ_S3_SECRET_ACCESS_KEY", "test-secret")
        s = get_settings()
        assert isinstance(s, Settings)
        assert get_settings() is s

    def test_get_settings_raises_without_required_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No singleton and no env → fail-fast ``ValidationError``."""
        monkeypatch.setitem(Settings.model_config, "env_file", "/dev/null")
        for var in ("OZ_DATABASE_URL", "OZ_REDIS_URL", "OZ_SECRET_KEY"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(Exception, match="DATABASE_URL"):
            get_settings()

    def test_set_settings_roundtrip(self) -> None:
        """``set_settings()`` + ``get_settings()`` roundtrip."""
        s = _settings()
        set_settings(s)
        assert get_settings() is s

    def test_get_settings_returns_same_instance(self) -> None:
        """Multiple calls to ``get_settings()`` return the same instance."""
        s = _settings()
        set_settings(s)
        assert get_settings() is get_settings()

    def test_backward_compatible_settings_import(self) -> None:
        """``from core.config import settings`` works via ``__getattr__``."""
        set_settings(_settings())
        from core.config import settings  # noqa: F811  — intentional lazy import

        assert _DEFAULT_REQUIRED["DATABASE_URL"] == settings.DATABASE_URL

    def test_unknown_attribute_raises_attribute_error(self) -> None:
        """Anything but ``settings`` raises ``AttributeError``."""
        import core.config as cfg

        with pytest.raises(AttributeError, match="no attribute"):
            cfg.nonexistent_attribute  # noqa: B018  — intentional lookup
