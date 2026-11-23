"""Integration tests for addon settings endpoints + settings on install/detail.

Uses live Postgres 15 + Redis via testcontainers (session ``engine``
fixture). Skips gracefully when no Docker/container runtime is
available. Fixture addons are created in-test under ``./addons/`` (the
default ``ALLOWED_ADDON_SRC``) — never the checked-in ``addons/democrm``.

Flow: install (201, disabled, defaults) → GET settings (200 defaults +
schema) → PATCH valid/invalid → DELETE (204) → GET 404.
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from core.config import PLATFORM_ORG_ID, get_settings
from repositories.user_repository import UserRepository
from tests.integration.conftest import asgi_transport
from utils.crypto import create_jwt_token

pytestmark = pytest.mark.integration

ADDONS_SRC_DIR = Path("addons")
VAR_ADDONS_DIR = Path("var/addons")


# ═══════════════════════════════════════════════════════════════════════════════
# Fixture-addon builders
# ═══════════════════════════════════════════════════════════════════════════════


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _make_settings_addon(src: Path) -> None:
    """Minimal addon exposing a threshold/mode/verbose settings schema."""
    _write(
        src / "addon.toml",
        """id = "oztestsettings"
version = "1.0.0"
hooks = ["router"]

[settings_schema.threshold]
type = "number"
default = 0.5

[settings_schema.mode]
type = "string"
default = "fast"

[settings_schema.verbose]
type = "boolean"
default = true
""",
    )
    _write(
        src / "pyproject.toml",
        '[project]\nname = "oztestsettings"\nversion = "1.0.0"\n',
    )
    _write(
        src / "hooks.py",
        "from fastapi import APIRouter\nrouter = APIRouter()\n",
    )


@pytest.fixture
def settings_src() -> Any:
    """Create the settings fixture source; clean up everything after."""
    src = ADDONS_SRC_DIR / "oztestsettings"
    assert not src.exists(), "leftover fixture addon — previous run did not clean up"
    _make_settings_addon(src)
    yield src
    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(VAR_ADDONS_DIR / "oztestsettings", ignore_errors=True)


@pytest.fixture
async def superadmin_client(isolated_app: Any) -> Any:
    """Superadmin-authenticated client (admin addon endpoints are gated)."""
    async with isolated_app.state.db_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO organizations (id, name, org_code) "
                "VALUES (:id, 'SYSTEM', 'platform') ON CONFLICT (id) DO NOTHING"
            ),
            {"id": PLATFORM_ORG_ID},
        )
        user = await UserRepository(session).create(
            external_id=f"superadmin_{uuid4().hex[:8]}",
            role="superadmin",
        )
        await session.execute(
            text("UPDATE users SET must_change_password = false WHERE id = :id"),
            {"id": user.id},
        )
        await session.commit()
    token = create_jwt_token(
        data={
            "sub": str(user.id),
            "user_id": str(PLATFORM_ORG_ID),
            "role": "superadmin",
            "type": "access",
        },
        secret=get_settings().SECRET_KEY,
        expires_delta=timedelta(hours=1),
    )
    transport = asgi_transport(isolated_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


class TestAddonSettings:
    async def test_install_get_patch_delete_flow(
        self, superadmin_client: AsyncClient, settings_src: Path
    ) -> None:
        # Install → 201, disabled by default, settings carry schema defaults.
        install = await superadmin_client.post(
            "/admin/system/addons", json={"local_path": str(settings_src)}
        )
        assert install.status_code == 201, install.text
        assert install.json()["enabled"] is False
        assert install.json()["settings"]["threshold"] == 0.5
        assert install.json()["settings_schema"] != []

        # Detail GET includes settings + schema.
        detail = await superadmin_client.get("/admin/system/addons/oztestsettings")
        assert detail.status_code == 200
        assert detail.json()["settings"] == {
            "threshold": 0.5,
            "mode": "fast",
            "verbose": True,
        }
        assert {e["key"] for e in detail.json()["settings_schema"]} == {
            "threshold",
            "mode",
            "verbose",
        }

        # GET settings → 200 defaults + schema.
        got = await superadmin_client.get(
            "/admin/system/addons/oztestsettings/settings"
        )
        assert got.status_code == 200
        assert got.json()["settings"]["threshold"] == 0.5
        assert {e["key"] for e in got.json()["settings_schema"]} == {
            "threshold",
            "mode",
            "verbose",
        }

        # PATCH valid → 200 persists; GET after shows the new value.
        patched = await superadmin_client.patch(
            "/admin/system/addons/oztestsettings/settings",
            json={"settings": {"threshold": 0.7}},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["settings"]["threshold"] == 0.7
        again = await superadmin_client.get(
            "/admin/system/addons/oztestsettings/settings"
        )
        assert again.json()["settings"]["threshold"] == 0.7

        # PATCH unknown key → 422.
        unknown = await superadmin_client.patch(
            "/admin/system/addons/oztestsettings/settings",
            json={"settings": {"nope": 1}},
        )
        assert unknown.status_code == 422

        # PATCH wrong type (string for number) → 422.
        wrong_type = await superadmin_client.patch(
            "/admin/system/addons/oztestsettings/settings",
            json={"settings": {"threshold": "high"}},
        )
        assert wrong_type.status_code == 422

        # PATCH bool for number → 422 (bool is not a number).
        bool_as_number = await superadmin_client.patch(
            "/admin/system/addons/oztestsettings/settings",
            json={"settings": {"threshold": True}},
        )
        assert bool_as_number.status_code == 422

        # DELETE → 204, GET after → 404.
        deleted = await superadmin_client.delete(
            "/admin/system/addons/oztestsettings"
        )
        assert deleted.status_code == 204
        assert (
            await superadmin_client.get("/admin/system/addons/oztestsettings")
        ).status_code == 404
        assert (
            await superadmin_client.get(
                "/admin/system/addons/oztestsettings/settings"
            )
        ).status_code == 404

    async def test_get_settings_unknown_addon_404(
        self, superadmin_client: AsyncClient
    ) -> None:
        resp = await superadmin_client.get(
            "/admin/system/addons/does-not-exist/settings"
        )
        assert resp.status_code == 404
