"""Integration tests for the full addon lifecycle + migrations + RLS.

Uses live Postgres 15 + Redis via testcontainers (session ``engine``
fixture). Skips gracefully when no Docker/container runtime is
available. Fixture addons are created in-test under ``./addons/`` (the
default ``ALLOWED_ADDON_SRC``) — never the checked-in ``addons/democrm``.

Flow: install → remove_data confirm-guard 422 → policy 422 + no-row.
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


def _make_crm_addon(src: Path, version: str) -> None:
    """Template-shape source: flat addon.toml + hooks.py + ui/ panels."""
    _write(
        src / "addon.toml",
        f"""id = "oztestcrm"
version = "{version}"
hooks = ["router"]

[[panels]]
id = "board"
title = "Board"
path = "/ui/board.html"

[[panels]]
id = "detail"
title = "Detail"
path = "/ui/sub/detail.html"
""",
    )
    _write(src / "pyproject.toml", f'[project]\nname = "oztestcrm"\nversion = "{version}"\n')
    _write(
        src / "hooks.py",
        """from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from core.rls import set_bypass_rls
from dependencies.db import get_db

router = APIRouter()


@router.get("/ping")
async def ping() -> dict:
    return {"status": "ok"}


@router.get("/sneaky")
async def sneaky(db: AsyncSession = Depends(get_db)) -> dict:
    await set_bypass_rls(db)
    return {"status": "bypassed"}
""",
    )
    _write(
        src / "ui" / "board.html",
        """<html><head><link href="styles.css"></head><body>
<a href="other.html">other</a>
<img src="logo.png">
<a href="/v1/ext/oztestcrm/ping">api</a>
</body></html>""",
    )
    _write(src / "ui" / "other.html", "<html><body>other</body></html>")
    _write(src / "ui" / "logo.png", "PNGDATA")
    _write(src / "ui" / "styles.css", "body { color: red; }")
    _write(src / "ui" / "sub" / "detail.html", "<html><body>detail</body></html>")


def _make_migration_addon(src: Path, addon_id: str, version: str = "1.0.0") -> None:
    """Flat addon shape with an alembic migrations/ chain."""
    slug = addon_id.replace("-", "")
    _write(
        src / "addon.toml",
        f"""id = "{addon_id}"
version = "{version}"
hooks = ["router"]
tables = ["notes"]
""",
    )
    _write(src / "pyproject.toml", f'[project]\nname = "{slug}"\nversion = "{version}"\n')
    _write(src / "hooks.py", "from fastapi import APIRouter\nrouter = APIRouter()\n")
    _write(
        src / "migrations" / "versions" / "0001_init.py",
        f'''"""init."""
revision = "0001"
down_revision = None


def upgrade():
    from alembic import op
    import sqlalchemy as sa

    op.create_table(
        "addon_{addon_id}_notes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Uuid, nullable=False),
        sa.Column("body", sa.Text),
    )


def downgrade():
    from alembic import op

    op.drop_table("addon_{addon_id}_notes")
''',
    )


@pytest.fixture
def crm_src() -> Any:
    """Create the democrm-like fixture source; clean up everything after."""
    src = ADDONS_SRC_DIR / "oztestcrm"
    assert not src.exists(), "leftover fixture addon — previous run did not clean up"
    _make_crm_addon(src, "1.0.0")
    yield src
    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(VAR_ADDONS_DIR / "oztestcrm", ignore_errors=True)


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


class TestAddonLifecycle:
    async def test_remove_data_requires_confirm(
        self, superadmin_client: AsyncClient, crm_src: Path
    ) -> None:
        await superadmin_client.post(
            "/admin/system/addons", json={"local_path": str(crm_src)}
        )
        resp = await superadmin_client.delete(
            "/admin/system/addons/oztestcrm",
            params={"remove_data": True, "confirm": False},
        )
        assert resp.status_code == 422
        cleanup = await superadmin_client.delete("/admin/system/addons/oztestcrm")
        assert cleanup.status_code == 204


class TestAddonMigrations:
    async def test_policy_violation_rejected(
        self, superadmin_client: AsyncClient
    ) -> None:
        src = ADDONS_SRC_DIR / "oztestbad"
        shutil.rmtree(src, ignore_errors=True)
        _make_migration_addon(src, "oztestbad")
        # Rewrite the migration to create a table outside the allowlist.
        mig = src / "migrations" / "versions" / "0001_init.py"
        mig.write_text(mig.read_text().replace("addon_oztestbad_notes", "addon_oztestbad_evil"))
        try:
            resp = await superadmin_client.post(
                "/admin/system/addons", json={"local_path": str(src)}
            )
            assert resp.status_code == 422, resp.text
            # The failed install rolls back with the request transaction —
            # no row persists (fail-closed: nothing half-installed).
            one = await superadmin_client.get("/admin/system/addons/oztestbad")
            assert one.status_code == 404
        finally:
            await superadmin_client.delete("/admin/system/addons/oztestbad")
            shutil.rmtree(src, ignore_errors=True)
            shutil.rmtree(VAR_ADDONS_DIR / "oztestbad", ignore_errors=True)
