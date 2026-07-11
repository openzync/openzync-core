"""Unit tests for ``core.config.Settings``.

These tests validate that the pydantic model loads correctly, enforces
constraints like ``SECRET_KEY min_length``, and that the singleton accessors
(``set_settings`` / ``get_settings``) work as expected.

.. note::

    ``Settings`` is now a :class:`pydantic.BaseModel` (not
    ``pydantic_settings.BaseSettings``).  It has **no** env-var fallback —
    all values come from OpenBao.  Required fields (``DATABASE_URL``,
    ``REDIS_URL``, ``SECRET_KEY``, ``WEBHOOK_SIGNING_SECRET``) have no
    defaults and must be passed explicitly.

    LLM / Embedding / Graph / Behaviour settings were removed from this
    class in favour of per-org DB config.  See
    ``schemas/organization_config.OrgConfigBase``.
"""

from __future__ import annotations

import pytest

from core.config import Settings, get_settings, set_settings


# ── Helpers ───────────────────────────────────────────────────────────────────

_DEFAULT_REQUIRED: dict[str, str] = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/openzync_test",
    "REDIS_URL": "redis://localhost:6379/1",
    "SECRET_KEY": "a" * 32,
    "WEBHOOK_SIGNING_SECRET": "b" * 32,
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

    def test_webhook_signing_secret_min_length_enforced(self) -> None:
        """WEBHOOK_SIGNING_SECRET shorter than 32 characters should raise."""
        with pytest.raises(Exception, match="at least 32 characters"):
            _settings(WEBHOOK_SIGNING_SECRET="short")

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

    def test_required_fields_missing(self) -> None:
        """Creating Settings without required fields should raise."""
        with pytest.raises(Exception, match="DATABASE_URL"):
            Settings(
                REDIS_URL="redis://localhost:6379/1",
                SECRET_KEY="a" * 32,
                WEBHOOK_SIGNING_SECRET="b" * 32,
            )


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

    def test_get_settings_raises_before_init(self) -> None:
        """``get_settings()`` must raise ``RuntimeError`` before initialisation."""
        with pytest.raises(RuntimeError, match="not initialised"):
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

        assert settings.DATABASE_URL == _DEFAULT_REQUIRED["DATABASE_URL"]

    def test_backward_compatible_import_raises_before_init(self) -> None:
        """Importing ``settings`` before ``init_settings`` raises ``RuntimeError``."""
        with pytest.raises(RuntimeError, match="not initialised"):
            from core.config import settings  # noqa: F811  — intentional lazy import
            _ = settings  # noqa:  — trigger the lazy access
