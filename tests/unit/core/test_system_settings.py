"""Unit tests for core.system_settings — masked listing + raw reveal.

Covers:
- masking: URL userinfo stripped, secret keys bullet-masked (incl. SMTP
  username), non-secret keys returned verbatim, unset/empty → is_set False.
- categories: every SYSTEM_KEY_MAPPING key has a category.
- reveal: raw value returned; unknown or unset key → NotFoundError.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core.exceptions import NotFoundError
from core.openbao import SYSTEM_KEY_MAPPING
from core.system_settings import list_system_settings, reveal_system_setting
from schemas.admin_system import SYSTEM_SETTING_CATEGORIES

pytestmark = pytest.mark.unit

MASK = "\u2022" * 8


def _bao(raw: dict) -> AsyncMock:
    bao = AsyncMock()
    bao.read_system_config.return_value = raw
    return bao


def _item(resp, key: str):
    return next(i for i in resp.data if i.key == key)


class TestCategories:
    def test_every_mapping_key_has_a_category(self) -> None:
        """SYSTEM_KEY_MAPPING and SYSTEM_SETTING_CATEGORIES cover the same keys."""
        assert set(SYSTEM_KEY_MAPPING) == set(SYSTEM_SETTING_CATEGORIES)


class TestListSystemSettings:
    @pytest.mark.asyncio
    async def test_url_userinfo_stripped(self) -> None:
        bao = _bao(
            {"OZ_DATABASE_URL": "postgresql+asyncpg://user:pass@db.example.com:5432/mydb"}
        )
        resp = await list_system_settings(bao)
        item = _item(resp, "OZ_DATABASE_URL")
        assert item.is_set is True
        assert item.masked_value == "postgresql+asyncpg://db.example.com:5432"
        assert item.category == "Infrastructure"

    @pytest.mark.asyncio
    async def test_url_without_userinfo_kept_verbatim(self) -> None:
        bao = _bao({"OZ_REDIS_URL": "redis://cache.example.com:6379/0"})
        resp = await list_system_settings(bao)
        item = _item(resp, "OZ_REDIS_URL")
        assert item.masked_value == "redis://cache.example.com:6379/0"

    @pytest.mark.asyncio
    async def test_secret_keys_bullet_masked(self) -> None:
        bao = _bao(
            {
                "OZ_SECRET_KEY": "s3cr3t",
                "OZ_WEBHOOK_SIGNING_SECRET": "whsec-abc",
                "OZ_SMTP_PASSWORD": "pw",
                "OZ_ROOT_PASSWORD": "root",
            }
        )
        resp = await list_system_settings(bao)
        for key in (
            "OZ_SECRET_KEY",
            "OZ_WEBHOOK_SIGNING_SECRET",
            "OZ_SMTP_PASSWORD",
            "OZ_ROOT_PASSWORD",
        ):
            item = _item(resp, key)
            assert item.is_set is True
            assert item.masked_value == MASK

    @pytest.mark.asyncio
    async def test_smtp_username_masked(self) -> None:
        bao = _bao({"OZ_SMTP_USERNAME": "admin@example.com"})
        resp = await list_system_settings(bao)
        item = _item(resp, "OZ_SMTP_USERNAME")
        assert item.masked_value == MASK

    @pytest.mark.asyncio
    async def test_non_secret_full_value(self) -> None:
        bao = _bao({"OZ_ENVIRONMENT": "production"})
        resp = await list_system_settings(bao)
        item = _item(resp, "OZ_ENVIRONMENT")
        assert item.masked_value == "production"

    @pytest.mark.asyncio
    async def test_unset_key_is_set_false(self) -> None:
        bao = _bao({"OZ_ENVIRONMENT": "production"})
        resp = await list_system_settings(bao)
        item = _item(resp, "OZ_DATABASE_URL")
        assert item.is_set is False
        assert item.masked_value is None

    @pytest.mark.asyncio
    async def test_empty_value_is_unset(self) -> None:
        bao = _bao({"OZ_LOG_LEVEL": ""})
        resp = await list_system_settings(bao)
        item = _item(resp, "OZ_LOG_LEVEL")
        assert item.is_set is False
        assert item.masked_value is None

    @pytest.mark.asyncio
    async def test_returns_all_mapping_keys_in_order(self) -> None:
        bao = _bao({})
        resp = await list_system_settings(bao)
        assert [i.key for i in resp.data] == list(SYSTEM_KEY_MAPPING)


class TestRevealSystemSetting:
    @pytest.mark.asyncio
    async def test_reveal_returns_raw_value(self) -> None:
        raw_url = "postgresql+asyncpg://user:pass@db.example.com:5432/mydb"
        bao = _bao({"OZ_DATABASE_URL": raw_url})
        resp = await reveal_system_setting("OZ_DATABASE_URL", bao)
        assert resp.key == "OZ_DATABASE_URL"
        assert resp.value == raw_url

    @pytest.mark.asyncio
    async def test_reveal_unknown_key_not_found(self) -> None:
        with pytest.raises(NotFoundError):
            await reveal_system_setting("OZ_NOPE", _bao({}))

    @pytest.mark.asyncio
    async def test_reveal_unset_key_not_found(self) -> None:
        bao = _bao({"OZ_ENVIRONMENT": "production"})
        with pytest.raises(NotFoundError):
            await reveal_system_setting("OZ_DATABASE_URL", bao)
