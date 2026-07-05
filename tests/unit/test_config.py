"""Unit tests for ``core.config.Settings``.

These tests validate that the pydantic-settings model loads correctly,
respects environment variable overrides (with the ``OZ_`` prefix used by
the actual model), and enforces constraints like ``SECRET_KEY min_length``.

.. note::

    LLM / Embedding / Graph / Behaviour settings were removed from this
    class in favour of per-org DB config.  See
    ``schemas/organization_config.OrgConfigBase``.
"""

from __future__ import annotations

import os

import pytest

# ═══ NOTE: Required env vars must be set BEFORE importing Settings ═══════════
# The ``settings = Settings()`` singleton in core/config.py is evaluated at
# import time, and DATABASE_URL / REDIS_URL / SECRET_KEY have no defaults.
_REQUIRED_ENV: dict[str, str] = {
    "OZ_DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/openzync_test",
    "OZ_REDIS_URL": "redis://localhost:6379/1",
    "OZ_SECRET_KEY": "a" * 32,
}
for _key, _val in _REQUIRED_ENV.items():
    os.environ.setdefault(_key, _val)

from core.config import Settings


# ── Helpers ───────────────────────────────────────────────────────────────────


def _set_required_env(monkeypatch: pytest.MonkeyPatch | None = None) -> None:
    """Ensure all required env vars are present before instantiating Settings."""
    for key, val in _REQUIRED_ENV.items():
        if monkeypatch:
            monkeypatch.setenv(key, val)
        else:
            os.environ.setdefault(key, val)


def _settings(**overrides: str) -> Settings:
    """Instantiate Settings without reading the project ``.env`` file.

    Unit tests must not be influenced by the developer's local ``.env``
    file.  Passing ``_env_file=None`` tells pydantic-settings to skip
    the file entirely.
    """
    return Settings(_env_file=None, **overrides)


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestSettings:
    """Validate the core Settings model."""

    def test_defaults_are_sane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fields with defaults should match expected development values."""
        _set_required_env(monkeypatch)
        # CI sets OZ_ENVIRONMENT=testing — clear it to test the default
        monkeypatch.delenv("OZ_ENVIRONMENT", raising=False)

        s = _settings()
        assert s.ENVIRONMENT == "development"
        assert s.LOG_LEVEL == "INFO"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Settings should pick up ``OZ_``-prefixed env vars."""
        _set_required_env(monkeypatch)
        monkeypatch.setenv("OZ_ENVIRONMENT", "staging")
        monkeypatch.setenv("OZ_LOG_LEVEL", "DEBUG")
        s = _settings()
        assert s.ENVIRONMENT == "staging"
        assert s.LOG_LEVEL == "DEBUG"

    def test_database_url_accepts_postgres_dsn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DATABASE_URL must contain 'postgresql' — pydantic PostgresDsn validator."""
        _set_required_env(monkeypatch)

        s = _settings()
        url = str(s.DATABASE_URL)
        assert "postgresql" in url, f"Expected postgresql DSN, got {url!r}"

    def test_secret_key_min_length_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A SECRET_KEY shorter than 32 characters should raise a validation error."""
        _set_required_env(monkeypatch)
        monkeypatch.delenv("OZ_SECRET_KEY", raising=False)

        with pytest.raises(Exception, match="at least 32 characters"):
            _settings(OZ_SECRET_KEY="too-short")

    def test_rate_limit_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rate-limit configuration should have sensible defaults."""
        _set_required_env(monkeypatch)

        s = _settings()
        assert s.RATE_LIMIT_IP_MAX >= 1
        assert s.RATE_LIMIT_WINDOW_SEC >= 1

    def test_cors_origins_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default CORS origin should point to the local dev server."""
        _set_required_env(monkeypatch)

        s = _settings()
        assert "localhost:3000" in s.CORS_ORIGINS

    def test_frozen_settings_prevent_mutation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Settings instances are frozen — assignment should raise an error.

        Note: pydantic v2 raises ``ValidationError`` (not ``TypeError``) when
        attempting to set an attribute on a frozen model.  We catch the
        generic ``Exception`` to remain compatible across pydantic versions.
        """
        _set_required_env(monkeypatch)

        s = _settings()
        with pytest.raises(Exception, match="frozen"):
            s.ENVIRONMENT = "production"  # type: ignore[misc]
