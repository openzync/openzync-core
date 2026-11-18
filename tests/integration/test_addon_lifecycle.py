"""Integration tests for the full addon lifecycle + migrations + RLS.

Uses live Postgres 15 + Redis via testcontainers (session ``engine``
fixture). Skips gracefully when no Docker/container runtime is
available. Fixture addons are created in-test under ``./addons/`` (the
default ``ALLOWED_ADDON_SRC``) — never the checked-in ``addons/democrm``.

Flow: install (template-shape + flat) → list → enable → router 200 →
ticket mint → panel 200 + rewritten link → follow link 200 →
cross-panel 403 → disable → API+UI 403 → upgrade → check-updates flag →
uninstall remove_data (table dropped).
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
        sa.Column("organization_id", sa.Uuid, nullable=False),
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
            organization_id=PLATFORM_ORG_ID,
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
            "org_id": str(PLATFORM_ORG_ID),
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


async def _table_exists(app: Any, table: str) -> bool:
    async with app.state.db_session_factory() as session:
        result = await session.execute(
            text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}
        )
        return result.scalar() is not None


class TestAddonLifecycle:
    async def test_full_lifecycle(
        self, superadmin_client: AsyncClient, isolated_app: Any, crm_src: Path
    ) -> None:
        # ── install (disabled by default) ──────────────────────────────
        resp = await superadmin_client.post(
            "/admin/system/addons", json={"local_path": str(crm_src)}
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["enabled"] is False
        assert resp.json()["version"] == "1.0.0"

        # ── list + get (flat shape, panels attached) ───────────────────
        listed = await superadmin_client.get("/admin/system/addons")
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        one = await superadmin_client.get("/admin/system/addons/oztestcrm")
        assert one.status_code == 200
        assert {p["path"] for p in one.json()["panels"]} == {
            "/ui/board.html",
            "/ui/sub/detail.html",
        }

        # ── enable → router 200 ────────────────────────────────────────
        enabled = await superadmin_client.post("/admin/system/addons/oztestcrm/enable")
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        ping = await superadmin_client.get("/v1/ext/oztestcrm/ping")
        assert ping.status_code == 200, ping.text
        assert ping.json() == {"status": "ok"}

        # ── RLS: addon handler calling set_bypass_rls is denied ─────────
        sneaky = await superadmin_client.get("/v1/ext/oztestcrm/sneaky")
        assert sneaky.status_code == 403, sneaky.text
        # Core flows unaffected — addon route + admin API still work.
        assert (await superadmin_client.get("/v1/ext/oztestcrm/ping")).status_code == 200
        assert (await superadmin_client.get("/admin/system/addons")).status_code == 200

        # ── ticket mint → panel 200 + rewritten link ────────────────────
        ticket_resp = await superadmin_client.post(
            "/admin/system/addons/oztestcrm/panels/ticket",
            json={"panel_path": "/ui/board.html"},
        )
        assert ticket_resp.status_code == 201, ticket_resp.text
        ticket = ticket_resp.json()["ticket"]
        panel = await superadmin_client.get(
            f"/v1/ext/oztestcrm/ui/board.html?ticket={ticket}"
        )
        assert panel.status_code == 200, panel.text
        assert f"other.html?ticket={ticket}" in panel.text

        # ── follow rewritten link 200 ──────────────────────────────────
        followed = await superadmin_client.get(
            f"/v1/ext/oztestcrm/ui/other.html?ticket={ticket}"
        )
        assert followed.status_code == 200

        # ── cross-panel 403 (ticket bound to the board scope) ───────────
        detail_ticket = (
            await superadmin_client.post(
                "/admin/system/addons/oztestcrm/panels/ticket",
                json={"panel_path": "/ui/sub/detail.html"},
            )
        ).json()["ticket"]
        cross = await superadmin_client.get(
            f"/v1/ext/oztestcrm/ui/board.html?ticket={detail_ticket}"
        )
        assert cross.status_code == 403, cross.text
        scoped = await superadmin_client.get(
            f"/v1/ext/oztestcrm/ui/sub/detail.html?ticket={detail_ticket}"
        )
        assert scoped.status_code == 200

        # ── disable → API + UI 403 ─────────────────────────────────────
        disabled = await superadmin_client.post(
            "/admin/system/addons/oztestcrm/disable"
        )
        assert disabled.status_code == 200
        assert (await superadmin_client.get("/v1/ext/oztestcrm/ping")).status_code == 403
        gated = await superadmin_client.get(
            f"/v1/ext/oztestcrm/ui/board.html?ticket={ticket}"
        )
        assert gated.status_code == 403, gated.text

        # ── upgrade (local re-copy) ────────────────────────────────────
        await superadmin_client.post("/admin/system/addons/oztestcrm/enable")
        _make_crm_addon(crm_src, "1.1.0")
        upgraded = await superadmin_client.post("/admin/system/addons/oztestcrm/upgrade")
        assert upgraded.status_code == 200, upgraded.text
        assert upgraded.json()["version"] == "1.1.0"
        assert upgraded.json()["update_available"] is False
        assert (
            await superadmin_client.get("/v1/ext/oztestcrm/ping")
        ).status_code == 200

        # ── check-updates flag (bumped source, no upgrade yet) ─────────
        _make_crm_addon(crm_src, "1.2.0")
        checked = await superadmin_client.post("/admin/system/addons/check-updates")
        assert checked.status_code == 200
        flagged = [a for a in checked.json()["data"] if a["id"] == "oztestcrm"][0]
        assert flagged["update_available"] is True
        assert flagged["latest_seen"] == "1.2.0"
        assert (
            await superadmin_client.post("/admin/system/addons/oztestcrm/upgrade")
        ).status_code == 200

        # ── uninstall remove_data (no tables → just the row) ───────────
        gone = await superadmin_client.delete(
            "/admin/system/addons/oztestcrm",
            params={"remove_data": True, "confirm": True},
        )
        assert gone.status_code == 204, gone.text
        assert (
            await superadmin_client.get("/admin/system/addons/oztestcrm")
        ).status_code == 404

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
    async def _install(
        self, client: AsyncClient, src: Path, addon_id: str
    ) -> None:
        resp = await client.post("/admin/system/addons", json={"local_path": str(src)})
        assert resp.status_code == 201, resp.text
        enabled = await client.post(f"/admin/system/addons/{addon_id}/enable")
        assert enabled.status_code == 200, enabled.text

    async def test_install_migrates_and_stamps_version(
        self, superadmin_client: AsyncClient, isolated_app: Any
    ) -> None:
        src = ADDONS_SRC_DIR / "oztestmig"
        shutil.rmtree(src, ignore_errors=True)
        _make_migration_addon(src, "oztestmig")
        try:
            await self._install(superadmin_client, src, "oztestmig")
            assert await _table_exists(isolated_app, "addon_oztestmig_notes")
            async with isolated_app.state.db_session_factory() as session:
                stamped = await session.execute(
                    text('SELECT version_num FROM "addon_alembic_oztestmig"')
                )
                assert stamped.scalar() == "0001"
            # Uninstall with data → downgrade + dropped.
            gone = await superadmin_client.delete(
                "/admin/system/addons/oztestmig",
                params={"remove_data": True, "confirm": True},
            )
            assert gone.status_code == 204, gone.text
            assert not await _table_exists(isolated_app, "addon_oztestmig_notes")
        finally:
            shutil.rmtree(src, ignore_errors=True)
            shutil.rmtree(VAR_ADDONS_DIR / "oztestmig", ignore_errors=True)

    async def test_uninstall_without_data_keeps_tables(
        self, superadmin_client: AsyncClient, isolated_app: Any
    ) -> None:
        src = ADDONS_SRC_DIR / "oztestmigkeep"
        shutil.rmtree(src, ignore_errors=True)
        _make_migration_addon(src, "oztestmigkeep")
        try:
            await self._install(superadmin_client, src, "oztestmigkeep")
            assert await _table_exists(isolated_app, "addon_oztestmigkeep_notes")
            gone = await superadmin_client.delete("/admin/system/addons/oztestmigkeep")
            assert gone.status_code == 204, gone.text
            assert await _table_exists(isolated_app, "addon_oztestmigkeep_notes")
        finally:
            async with isolated_app.state.db_session_factory() as session:
                await session.execute(text('DROP TABLE IF EXISTS "addon_oztestmigkeep_notes" CASCADE'))
                await session.execute(text('DROP TABLE IF EXISTS "addon_alembic_oztestmigkeep" CASCADE'))
                await session.commit()
            shutil.rmtree(src, ignore_errors=True)
            shutil.rmtree(VAR_ADDONS_DIR / "oztestmigkeep", ignore_errors=True)

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
