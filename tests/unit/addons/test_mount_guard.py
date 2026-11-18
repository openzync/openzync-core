"""Unit tests for addon mounting + request gating.

Covers ``unmount_prefix`` tracked-identity removal (regression: FastAPI
>=0.163 appends a lazy ``_IncludedRouter`` wrapper with no ``.path`` —
path matching alone cannot find it), placeholder-vs-real precedence,
and the ``require_addon_enabled`` yield-dependency (marker set/reset,
denial paths). No I/O, no server.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, Request

from core.addons import mount as mount_mod
from core.addons.mount import (
    _track_new_routes,
    mount_addon,
    mount_placeholder,
    unmount_prefix,
)
from core.addons.registry import AddonRecord, publish_registry
from core.addons.request_scope import (
    addon_request_active,
    enter_addon_request,
    exit_addon_request,
    scoped_addon_request,
)
from core.exceptions import AuthorizationError, NotFoundError
from dependencies.addons import require_addon_enabled


def _manifest(addon_id: str = "demo"):
    from core.addons.manifest import AddonManifest

    return AddonManifest(
        id=addon_id,
        version="1.0.0",
        min_core_version="0.0.0",
        hooks=frozenset(),
        permissions=frozenset(),
    )


def _request(app: FastAPI) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})


def _routed_paths(app: FastAPI) -> list[str]:
    return [getattr(r, "path", "") or "" for r in app.router.routes]


class TestUnmountPrefix:
    def test_removes_tracked_wrapper_by_identity(self) -> None:
        # Regression: the appended wrapper has no `.path`; only the
        # tracked identity removes it.
        app = FastAPI()
        router = APIRouter()

        @router.get("/ping")
        async def ping() -> dict:
            return {"status": "ok"}

        before = len(app.router.routes)
        app.include_router(router, prefix="/v1/ext/demo")
        _track_new_routes(app, "/v1/ext/demo", before)
        wrappers = app.router.routes[before:]
        assert all(getattr(r, "path", None) is None for r in wrappers)
        unmount_prefix(app, "/v1/ext/demo")
        assert len(app.router.routes) == before
        assert "/v1/ext/demo/ping" not in _routed_paths(app)

    def test_path_fallback_removes_untracked_routes(self) -> None:
        app = FastAPI()

        @app.get("/v1/ext/demo/legacy")
        async def legacy() -> dict:
            return {}

        unmount_prefix(app, "/v1/ext/demo")
        assert "/v1/ext/demo/legacy" not in _routed_paths(app)

    def test_other_prefixes_untouched(self) -> None:
        app = FastAPI()

        @app.get("/v1/ext/other/ping")
        async def ping() -> dict:
            return {}

        before = len(app.router.routes)
        unmount_prefix(app, "/v1/ext/demo")
        assert len(app.router.routes) == before


class TestPlaceholderPrecedence:
    def test_placeholder_then_real_router_replaces(self, tmp_path) -> None:
        app = FastAPI()
        mount_placeholder(app, "demo")
        placeholder_tracked = list(app.state._oz_ext_mounts["/v1/ext/demo"])
        assert len(placeholder_tracked) == 1
        assert all(r in app.router.routes for r in placeholder_tracked)

        real = APIRouter()

        @real.get("/ping")
        async def ping() -> dict:
            return {"status": "ok"}

        record = AddonRecord(
            manifest=_manifest(),
            module=SimpleNamespace(),
            version="1.0.0",
            enabled=True,
            generation=1,
            router=real,
        )
        mount_addon(app, record, tmp_path / "nosuchdir")
        # Placeholder wrapper stripped by identity, real router tracked instead.
        assert all(r not in app.router.routes for r in placeholder_tracked)
        live = app.state._oz_ext_mounts["/v1/ext/demo"]
        assert len(live) == 1
        assert all(r in app.router.routes for r in live)
        assert live[0] not in placeholder_tracked

    def test_ui_dir_mounts_panel_route(self, tmp_path) -> None:
        app = FastAPI()
        (tmp_path / "ui").mkdir()
        (tmp_path / "ui" / "index.html").write_text("<html></html>")
        record = AddonRecord(
            manifest=_manifest(),
            module=SimpleNamespace(),
            version="1.0.0",
            enabled=True,
            generation=1,
            router=None,
        )
        mount_addon(app, record, tmp_path)
        live = app.state._oz_ext_mounts["/v1/ext/demo"]
        assert len(live) == 1  # UI wrapper only (no API router)
        assert live[0] in app.router.routes
        unmount_prefix(app, "/v1/ext/demo")
        assert live[0] not in app.router.routes

    def test_broken_factory_mounts_nothing_but_survives(self, tmp_path, caplog=None) -> None:
        module = SimpleNamespace()
        record = AddonRecord(
            manifest=_manifest(),
            module=module,
            version="1.0.0",
            enabled=True,
            generation=1,
            router=None,
        )
        app = FastAPI()
        mount_addon(app, record, tmp_path / "nosuchdir")
        assert "/v1/ext/demo/ping" not in _routed_paths(app)


class TestRequestScope:
    def test_default_inactive(self) -> None:
        assert addon_request_active() is False

    def test_scoped_context_manager_sets_and_resets(self) -> None:
        with scoped_addon_request():
            assert addon_request_active() is True
        assert addon_request_active() is False

    def test_exception_still_resets(self) -> None:
        with pytest.raises(RuntimeError), scoped_addon_request():
            raise RuntimeError("boom")
        assert addon_request_active() is False

    def test_manual_enter_exit(self) -> None:
        token = enter_addon_request()
        assert addon_request_active() is True
        exit_addon_request(token)
        assert addon_request_active() is False


class TestRequireAddonEnabled:
    def _app_with(self, **overrides) -> FastAPI:
        from types import ModuleType

        kwargs: dict = {
            "manifest": _manifest(),
            "module": ModuleType("demo"),
            "version": "1.0.0",
            "enabled": True,
            "generation": 5,
        }
        kwargs.update(overrides)
        app = FastAPI()
        publish_registry(app, {"demo": AddonRecord(**kwargs)})  # type: ignore[arg-type]
        return app

    async def test_gate_sets_and_resets_marker(self) -> None:
        app = self._app_with()
        gate = require_addon_enabled("demo")
        gen = gate(_request(app))
        assert await gen.__anext__() == "demo"
        assert addon_request_active() is True
        await gen.aclose()
        assert addon_request_active() is False

    async def test_exception_inside_still_resets(self) -> None:
        app = self._app_with()
        gate = require_addon_enabled("demo")
        gen = gate(_request(app))
        await gen.__anext__()
        with pytest.raises(RuntimeError):
            await gen.athrow(RuntimeError("boom"))
        assert addon_request_active() is False

    async def test_unknown_addon_403(self) -> None:
        gate = require_addon_enabled("nope")
        with pytest.raises(AuthorizationError):
            await gate(_request(FastAPI())).__anext__()

    async def test_disabled_addon_403(self) -> None:
        app = self._app_with(enabled=False)
        gate = require_addon_enabled("demo")
        with pytest.raises(AuthorizationError):
            await gate(_request(app)).__anext__()
        assert addon_request_active() is False

    async def test_stale_generation_404(self) -> None:
        app = self._app_with()
        gate = require_addon_enabled("demo", generation=4)
        with pytest.raises(NotFoundError):
            await gate(_request(app)).__anext__()
        assert addon_request_active() is False

    async def test_current_generation_ok(self) -> None:
        app = self._app_with()
        gate = require_addon_enabled("demo", generation=5)
        gen = gate(_request(app))
        assert await gen.__anext__() == "demo"
        await gen.aclose()

    def test_mount_module_reference(self) -> None:
        assert mount_mod.PANEL_TICKET_TTL_S == 300
