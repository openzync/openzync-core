"""Unit tests for AddonService (services/addon_service.py).

Repo, installer, loader, mounts, and migrations are all mocked — no DB,
no Redis, no git. Pins: same-version 422, local re-copy, generation
bumps, unmount-on-disable, check-updates flags, uninstall/remove_data.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from core.addons.installer import InstalledSource
from core.exceptions import ConflictError, NotFoundError, ValidationError
from models.addon_state import AddonState
from services.addon_service import AddonService, _slug


def _row(**overrides: Any) -> AddonState:
    base: dict[str, Any] = {
        "id": "demo",
        "version": "1.0.0",
        "source_type": "local",
        "source_ref": "addons/demo",
        "installed_sha": "local-abc",
        "latest_seen": "1.0.0",
        "enabled": False,
        "update_available": False,
        "generation": 3,
    }
    base.update(overrides)
    return AddonState(**base)


def _repo(row: AddonState | None = None, rows: list[AddonState] | None = None) -> Any:
    repo = SimpleNamespace()
    repo.session = object()
    repo.get_by_id = AsyncMock(return_value=row)
    repo.get_all = AsyncMock(return_value=rows or [])
    repo.create = AsyncMock(side_effect=lambda r: r)
    repo.delete = AsyncMock()
    return repo


def _installed(version: str = "1.0.0") -> InstalledSource:
    from pathlib import Path

    return InstalledSource(
        addon_id="demo", version=version, installed_sha=f"local-{version}", path=Path("/tmp/x")
    )


def _manifest_stub() -> SimpleNamespace:
    return SimpleNamespace(id="demo", panels=(), tables=())


@pytest.fixture
def app() -> FastAPI:
    return FastAPI()


class TestInstall:
    async def test_happy_path_disabled_by_default(self, app: FastAPI) -> None:
        repo = _repo(row=None)
        service = AddonService(repo=repo, app=app)  # type: ignore[arg-type]
        with (
            patch("services.addon_service.install_source", AsyncMock(return_value=_installed())),
            patch("services.addon_service.parse_manifest", return_value=_manifest_stub()),
            patch("services.addon_service.load_addon", return_value=SimpleNamespace()),
            patch("services.addon_service.resolve_addon_router", return_value=None),
            patch("services.addon_service.resolve_worker_ops", return_value={}),
            patch("core.addons.mount.mount_placeholder") as placeholder,
            patch("core.addons.alembic_runner.migrate_up", AsyncMock()) as migrate,
        ):
            row = await service.install("local", "addons/demo", None)
        assert row.enabled is False
        assert row.latest_seen == "1.0.0"
        repo.create.assert_awaited_once()
        placeholder.assert_called_once_with(app, "demo")
        migrate.assert_awaited_once()

    async def test_local_with_tag_422(self, app: FastAPI) -> None:
        service = AddonService(repo=_repo(), app=app)  # type: ignore[arg-type]
        with pytest.raises(ValidationError, match="tag is only valid"):
            await service.install("local", "addons/demo", "v1.0.0")

    async def test_duplicate_409(self, app: FastAPI) -> None:
        service = AddonService(repo=_repo(row=_row()), app=app)  # type: ignore[arg-type]
        with patch(
            "services.addon_service.install_source", AsyncMock(return_value=_installed())
        ):
            with pytest.raises(ConflictError):
                await service.install("local", "addons/demo", None)


class TestEnableDisable:
    async def test_enable_bumps_generation_and_mounts(self, app: FastAPI) -> None:
        row = _row(enabled=False, generation=3)
        service = AddonService(repo=_repo(row=row), app=app)  # type: ignore[arg-type]
        with (
            patch("services.addon_service.parse_manifest", return_value=_manifest_stub()),
            patch("services.addon_service.load_addon", return_value=SimpleNamespace()),
            patch("services.addon_service.resolve_addon_router", return_value=None),
            patch("services.addon_service.resolve_worker_ops", return_value={"op": 1}),
            patch("core.addons.mount.mount_addon") as mount,
        ):
            out = await service.enable("demo")
            first_gen = out.generation
            second = await service.enable("demo")
        assert out.enabled is True
        assert first_gen > 0
        assert second.generation > first_gen
        assert mount.call_count == 2

    async def test_enable_unknown_404(self, app: FastAPI) -> None:
        service = AddonService(repo=_repo(row=None), app=app)  # type: ignore[arg-type]
        with pytest.raises(NotFoundError):
            await service.enable("nope")

    async def test_disable_unmounts_placeholders_bumps_generation(self, app: FastAPI) -> None:
        row = _row(enabled=True, generation=8)
        service = AddonService(repo=_repo(row=row), app=app)  # type: ignore[arg-type]
        with (
            patch("core.addons.mount.unmount_prefix") as unmount,
            patch("core.addons.mount.mount_placeholder") as placeholder,
        ):
            out = await service.disable("demo")
        assert out.enabled is False
        assert out.generation > 0
        unmount.assert_called_once_with(app, "/v1/ext/demo")
        placeholder.assert_called_once_with(app, "demo")


class TestUpgrade:
    async def test_local_same_version_422(self, app: FastAPI) -> None:
        row = _row(version="1.0.0")
        service = AddonService(repo=_repo(row=row), app=app)  # type: ignore[arg-type]
        with patch(
            "services.addon_service.install_source", AsyncMock(return_value=_installed("1.0.0"))
        ):
            with pytest.raises(ValidationError, match="already at latest"):
                await service.upgrade("demo")

    async def test_local_upgrade_recopies_and_reloads_when_enabled(self, app: FastAPI) -> None:
        row = _row(version="1.0.0", enabled=True)
        service = AddonService(repo=_repo(row=row), app=app)  # type: ignore[arg-type]
        with (
            patch("services.addon_service.install_source", AsyncMock(return_value=_installed("1.1.0"))),
            patch("services.addon_service.parse_manifest", return_value=_manifest_stub()),
            patch("services.addon_service.load_addon", return_value=SimpleNamespace()),
            patch("services.addon_service.resolve_addon_router", return_value=None),
            patch("services.addon_service.resolve_worker_ops", return_value={}),
            patch("core.addons.alembic_runner.migrate_up", AsyncMock()),
            patch("core.addons.mount.mount_addon") as mount,
        ):
            out = await service.upgrade("demo")
        assert out.version == "1.1.0"
        assert out.latest_seen == "1.1.0"
        assert out.update_available is False
        mount.assert_called_once()

    async def test_local_upgrade_skips_reload_when_disabled(self, app: FastAPI) -> None:
        row = _row(version="1.0.0", enabled=False)
        service = AddonService(repo=_repo(row=row), app=app)  # type: ignore[arg-type]
        with (
            patch("services.addon_service.install_source", AsyncMock(return_value=_installed("1.1.0"))),
            patch("services.addon_service.parse_manifest", return_value=_manifest_stub()),
            patch("core.addons.alembic_runner.migrate_up", AsyncMock()),
            patch("core.addons.mount.mount_addon") as mount,
        ):
            out = await service.upgrade("demo")
        assert out.version == "1.1.0"
        mount.assert_not_called()

    async def test_local_upgrade_with_tag_422(self, app: FastAPI) -> None:
        service = AddonService(repo=_repo(row=_row()), app=app)  # type: ignore[arg-type]
        with pytest.raises(ValidationError, match="tag is only valid"):
            await service.upgrade("demo", tag="v2")

    async def test_github_upgrade_pins_seen_to_sha(self, app: FastAPI) -> None:
        from pathlib import Path

        row = _row(source_type="github", source_ref="org/addon", version="1.0.0")
        installed = InstalledSource(
            addon_id="demo", version="2.0.0", installed_sha="v2.0.0", path=Path("/tmp/x")
        )
        service = AddonService(repo=_repo(row=row), app=app)  # type: ignore[arg-type]
        with (
            patch("services.addon_service.install_source", AsyncMock(return_value=installed)),
            patch("services.addon_service.parse_manifest", return_value=_manifest_stub()),
            patch("core.addons.alembic_runner.migrate_up", AsyncMock()),
        ):
            out = await service.upgrade("demo", tag="v2.0.0")
        assert out.version == "2.0.0"
        assert out.latest_seen == "v2.0.0"


class TestCheckUpdates:
    async def test_local_newer_source_flags_update(self, app: FastAPI) -> None:
        row = _row(version="1.0.0", latest_seen="1.0.0", update_available=False)
        service = AddonService(repo=_repo(rows=[row]), app=app)  # type: ignore[arg-type]
        with patch("services.addon_service.local_source_version", return_value="1.1.0"):
            rows = await service.check_updates()
        assert rows[0].update_available is True
        assert rows[0].latest_seen == "1.1.0"

    async def test_local_same_version_no_flag(self, app: FastAPI) -> None:
        row = _row(version="1.0.0", update_available=False)
        service = AddonService(repo=_repo(rows=[row]), app=app)  # type: ignore[arg-type]
        with patch("services.addon_service.local_source_version", return_value="1.0.0"):
            rows = await service.check_updates()
        assert rows[0].update_available is False

    async def test_github_failure_never_fails_cron(self, app: FastAPI) -> None:
        row = _row(source_type="github", source_ref="org/addon")
        service = AddonService(repo=_repo(rows=[row]), app=app)  # type: ignore[arg-type]
        with patch(
            "services.addon_service.latest_github_tag",
            AsyncMock(side_effect=ConnectionError("down")),
        ):
            rows = await service.check_updates()
        assert rows[0].update_available is False

    async def test_github_new_tag_flags_update(self, app: FastAPI) -> None:
        row = _row(source_type="github", source_ref="org/addon", installed_sha="v1.0.0")
        service = AddonService(repo=_repo(rows=[row]), app=app)  # type: ignore[arg-type]
        with patch(
            "services.addon_service.latest_github_tag", AsyncMock(return_value="v2.0.0")
        ):
            rows = await service.check_updates()
        assert rows[0].latest_seen == "v2.0.0"
        assert rows[0].update_available is True


class TestUninstall:
    async def test_without_data_skips_migrations(self, app: FastAPI) -> None:
        row = _row()
        service = AddonService(repo=_repo(row=row), app=app)  # type: ignore[arg-type]
        with (
            patch("core.addons.mount.unmount_prefix") as unmount,
            patch("core.addons.alembic_runner.migrate_down", AsyncMock()) as down,
            patch("core.addons.alembic_runner.drop_addon_tables", AsyncMock()) as drop,
        ):
            await service.uninstall("demo", remove_data=False)
        down.assert_not_awaited()
        drop.assert_not_awaited()
        unmount.assert_called_once_with(app, "/v1/ext/demo")

    async def test_with_data_downgrades_and_drops(self, app: FastAPI) -> None:
        row = _row()
        service = AddonService(repo=_repo(row=row), app=app)  # type: ignore[arg-type]
        with (
            patch("services.addon_service.parse_manifest", return_value=_manifest_stub()),
            patch("core.addons.mount.unmount_prefix"),
            patch("core.addons.alembic_runner.migrate_down", AsyncMock()) as down,
            patch("core.addons.alembic_runner.drop_addon_tables", AsyncMock()) as drop,
        ):
            await service.uninstall("demo", remove_data=True)
        down.assert_awaited_once()
        drop.assert_awaited_once()

    async def test_unknown_404(self, app: FastAPI) -> None:
        service = AddonService(repo=_repo(row=None), app=app)  # type: ignore[arg-type]
        with pytest.raises(NotFoundError):
            await service.uninstall("nope", remove_data=False)


class TestPanelTicket:
    def _service(self, app: FastAPI, row: AddonState) -> AddonService:
        from core.addons.manifest import AddonManifest, AddonPanel

        manifest = AddonManifest(
            id="demo",
            version="1.0.0",
            min_core_version="0.0.0",
            hooks=frozenset(),
            permissions=frozenset(),
            panels=(AddonPanel(id="board", title="Board", path="/ui/board.html"),),
        )
        service = AddonService(repo=_repo(row=row), app=app)  # type: ignore[arg-type]
        patcher = patch("services.addon_service.parse_manifest", return_value=manifest)
        patcher.start()
        return service

    async def test_mint_ok_with_ui_root_scope(self, app: FastAPI) -> None:
        import orjson

        redis = AsyncMock()
        app.state.redis = redis
        service = self._service(app, _row(enabled=True))
        ticket = await service.issue_panel_ticket("demo", "/ui/board.html", "viewer-1")
        assert len(ticket) >= 32
        _, payload, *_ = redis.set.await_args.args
        assert orjson.loads(payload)["allowed_dir"] == "/ui"

    async def test_disabled_403(self, app: FastAPI) -> None:
        from core.exceptions import AuthorizationError

        service = self._service(app, _row(enabled=False))
        with pytest.raises(AuthorizationError):
            await service.issue_panel_ticket("demo", "/ui/board.html", "v")

    async def test_unknown_panel_404(self, app: FastAPI) -> None:
        service = self._service(app, _row(enabled=True))
        with pytest.raises(NotFoundError):
            await service.issue_panel_ticket("demo", "/ui/nope.html", "v")

    async def test_no_redis_503(self, app: FastAPI) -> None:
        from core.exceptions import ServiceUnavailableError

        if hasattr(app.state, "redis"):
            delattr(app.state, "redis")
        service = self._service(app, _row(enabled=True))
        with pytest.raises(ServiceUnavailableError):
            await service.issue_panel_ticket("demo", "/ui/board.html", "v")


class TestHelpers:
    def test_slug_local(self) -> None:
        assert _slug(_row(source_type="local", source_ref="whatever"), "demo") == "demo"

    def test_slug_github(self) -> None:
        assert _slug(_row(source_type="github", source_ref="org/addon"), "demo") == "org__addon"

    async def test_health_loaded_flag(self, app: FastAPI) -> None:
        from core.addons.registry import publish_registry

        service = AddonService(repo=_repo(row=_row()), app=app)  # type: ignore[arg-type]
        assert (await service.health("demo"))["loaded"] is False
        publish_registry(app, {"demo": SimpleNamespace(enabled=True)})  # type: ignore[dict-item]
        assert (await service.health("demo"))["loaded"] is True
