"""Unit tests for core.system_settings — masked listing + raw reveal.

Covers:
- masking: URL userinfo stripped, secret keys bullet-masked (incl. SMTP
  username), non-secret keys returned verbatim, unset/empty → is_set False.
- categories: every key in SYSTEM_SETTING_CATEGORIES is listed.
- reveal: raw value returned; unknown or unset key → NotFoundError.

The module reads the ``OZ_*`` env-backed :class:`core.config.Settings`
singleton — tests install a crafted singleton via ``set_settings`` (the
conftest autouse fixture re-installs the dummy singleton before each
test, so no teardown is needed).
"""

from __future__ import annotations

import pytest

from core.config import Settings, set_settings
from core.exceptions import NotFoundError
from core.system_settings import list_system_settings, reveal_system_setting
from schemas.admin_system import SYSTEM_SETTING_CATEGORIES

pytestmark = pytest.mark.unit

MASK = "\u2022" * 8


def _install(**overrides: object) -> None:
    """Install a Settings singleton with the given field overrides."""
    base: dict[str, object] = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/openzync_test",
        "REDIS_URL": "redis://localhost:6379/1",
        "SECRET_KEY": "a" * 32,
    }
    base.update(overrides)
    set_settings(Settings(**base))  # type: ignore[arg-type]


def _item(resp, key: str):
    return next(i for i in resp.data if i.key == key)


class TestCategories:
    @pytest.mark.asyncio
    async def test_every_category_key_is_listed(self) -> None:
        """Every SYSTEM_SETTING_CATEGORIES key appears in the listing."""
        _install()
        resp = await list_system_settings()
        assert [i.key for i in resp.data] == list(SYSTEM_SETTING_CATEGORIES)
        assert all(i.category == SYSTEM_SETTING_CATEGORIES[i.key] for i in resp.data)


class TestListSystemSettings:
    @pytest.mark.asyncio
    async def test_url_userinfo_stripped(self) -> None:
        _install(
            DATABASE_URL="postgresql+asyncpg://user:pass@db.example.com:5432/mydb"
        )
        resp = await list_system_settings()
        item = _item(resp, "OZ_DATABASE_URL")
        assert item.is_set is True
        assert item.masked_value == "postgresql+asyncpg://db.example.com:5432"
        assert item.category == "Infrastructure"

    @pytest.mark.asyncio
    async def test_url_without_userinfo_kept_verbatim(self) -> None:
        _install(REDIS_URL="redis://cache.example.com:6379/0")
        resp = await list_system_settings()
        item = _item(resp, "OZ_REDIS_URL")
        assert item.masked_value == "redis://cache.example.com:6379/0"

    @pytest.mark.asyncio
    async def test_secret_keys_bullet_masked(self) -> None:
        _install(
            SECRET_KEY="s3cr3t" + "x" * 26,
            SECRET_KEY_PREVIOUS="prev" + "y" * 28,
            SMTP_PASSWORD="pw",
            SMTP_USERNAME="admin@example.com",
        )
        resp = await list_system_settings()
        for key in (
            "OZ_SECRET_KEY",
            "OZ_SECRET_KEY_PREVIOUS",
            "OZ_SMTP_PASSWORD",
            "OZ_SMTP_USERNAME",
        ):
            item = _item(resp, key)
            assert item.is_set is True
            assert item.masked_value == MASK

    @pytest.mark.asyncio
    async def test_non_secret_full_value(self) -> None:
        _install(ENVIRONMENT="production")
        resp = await list_system_settings()
        item = _item(resp, "OZ_ENVIRONMENT")
        assert item.masked_value == "production"

    @pytest.mark.asyncio
    async def test_unset_key_is_set_false(self) -> None:
        _install(ENVIRONMENT="production")
        resp = await list_system_settings()
        # SURREALDB_URL defaults to None — unset.
        item = _item(resp, "OZ_SURREALDB_URL")
        assert item.is_set is False
        assert item.masked_value is None

    @pytest.mark.asyncio
    async def test_empty_value_is_unset(self) -> None:
        _install(SMTP_PASSWORD="")
        resp = await list_system_settings()
        item = _item(resp, "OZ_SMTP_PASSWORD")
        assert item.is_set is False
        assert item.masked_value is None

    @pytest.mark.asyncio
    async def test_returns_all_keys_in_category_order(self) -> None:
        _install()
        resp = await list_system_settings()
        assert [i.key for i in resp.data] == list(SYSTEM_SETTING_CATEGORIES)

    @pytest.mark.asyncio
    async def test_removed_signing_secret_not_listed(self) -> None:
        """``OZ_WEBHOOK_SIGNING_SECRET`` was removed — never listed."""
        _install()
        resp = await list_system_settings()
        assert "OZ_WEBHOOK_SIGNING_SECRET" not in [i.key for i in resp.data]


class TestRevealSystemSetting:
    @pytest.mark.asyncio
    async def test_reveal_returns_raw_value(self) -> None:
        raw_url = "postgresql+asyncpg://user:pass@db.example.com:5432/mydb"
        _install(DATABASE_URL=raw_url)
        resp = await reveal_system_setting("OZ_DATABASE_URL")
        assert resp.key == "OZ_DATABASE_URL"
        assert resp.value == raw_url

    @pytest.mark.asyncio
    async def test_reveal_unknown_key_not_found(self) -> None:
        _install()
        with pytest.raises(NotFoundError):
            await reveal_system_setting("OZ_NOPE")

    @pytest.mark.asyncio
    async def test_reveal_removed_key_not_found(self) -> None:
        _install()
        with pytest.raises(NotFoundError):
            await reveal_system_setting("OZ_WEBHOOK_SIGNING_SECRET")

    @pytest.mark.asyncio
    async def test_reveal_unset_key_not_found(self) -> None:
        _install(ENVIRONMENT="production")
        with pytest.raises(NotFoundError):
            await reveal_system_setting("OZ_SURREALDB_URL")
