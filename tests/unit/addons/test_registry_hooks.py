"""Unit tests for the addon registry + hook resolvers.

Covers atomic publish, generation bumps, service-override precedence,
enablement gating (core/addons/registry.py) and the router/worker hook
resolvers (core/addons/hooks.py). No I/O.
"""

from __future__ import annotations

from types import ModuleType, SimpleNamespace

import pytest
from fastapi import FastAPI

from core.addons.hooks import resolve_addon_router, resolve_worker_ops
from core.addons.manifest import AddonManifest
from core.addons.registry import (
    get_enabled,
    get_registry,
    next_generation,
    publish_registry,
    resolve_service_override,
)
from core.exceptions import AuthorizationError, NotFoundError


def _manifest(addon_id: str = "demo") -> AddonManifest:
    return AddonManifest(
        id=addon_id,
        version="1.0.0",
        min_core_version="0.0.0",
        hooks=frozenset(),
        permissions=frozenset(),
    )


def _record(**overrides) -> object:
    from core.addons.registry import AddonRecord

    base = {
        "manifest": _manifest(),
        "module": ModuleType("demo"),
        "version": "1.0.0",
        "enabled": True,
        "generation": 7,
    }
    base.update(overrides)
    return AddonRecord(**base)  # type: ignore[arg-type]


class TestPublishAtomic:
    def test_empty_registry_by_default(self) -> None:
        assert get_registry(FastAPI()) == {}

    def test_publish_swaps_reference(self) -> None:
        app = FastAPI()
        records = {"demo": _record()}
        publish_registry(app, records)
        assert get_registry(app)["demo"] is records["demo"]

    def test_publish_copies_caller_dict(self) -> None:
        app = FastAPI()
        records: dict = {"demo": _record()}
        publish_registry(app, records)
        records["other"] = _record()
        assert "other" not in get_registry(app)

    def test_next_generation_monotonic(self) -> None:
        assert next_generation() < next_generation()


class TestGetEnabled:
    def test_unknown_id_404(self) -> None:
        with pytest.raises(NotFoundError):
            get_enabled(FastAPI(), "nope")

    def test_disabled_403(self) -> None:
        app = FastAPI()
        publish_registry(app, {"demo": _record(enabled=False)})
        with pytest.raises(AuthorizationError):
            get_enabled(app, "demo")

    def test_stale_generation_404(self) -> None:
        app = FastAPI()
        publish_registry(app, {"demo": _record(enabled=True, generation=9)})
        with pytest.raises(NotFoundError):
            get_enabled(app, "demo", generation=8)

    def test_current_generation_ok(self) -> None:
        app = FastAPI()
        publish_registry(app, {"demo": _record(enabled=True, generation=9)})
        assert get_enabled(app, "demo", generation=9).version == "1.0.0"

    def test_no_generation_pin_ok(self) -> None:
        app = FastAPI()
        publish_registry(app, {"demo": _record(enabled=True)})
        assert get_enabled(app, "demo").enabled is True


class TestServiceOverride:
    def test_record_override_wins(self) -> None:
        app = FastAPI()
        publish_registry(app, {"demo": _record(service_overrides={"search": "X"})})
        assert resolve_service_override(app, "search", "D") == "X"

    def test_disabled_record_skipped(self) -> None:
        app = FastAPI()
        publish_registry(app, {"demo": _record(enabled=False, service_overrides={"s": "X"})})
        assert resolve_service_override(app, "s", "D") == "D"

    def test_default_fallback(self) -> None:
        assert resolve_service_override(FastAPI(), "s", "D") == "D"

    def test_first_enabled_wins(self) -> None:
        app = FastAPI()
        publish_registry(
            app,
            {
                "a": _record(service_overrides={"s": "A"}),
                "b": _record(service_overrides={"s": "B"}),
            },
        )
        assert resolve_service_override(app, "s", "D") == "A"


class TestResolveRouter:
    def test_plain_attr_preferred(self) -> None:
        module = SimpleNamespace(router="ATTR", register_router=lambda: "FACTORY")
        assert resolve_addon_router(module) == "ATTR"  # type: ignore[arg-type]

    def test_factory_fallback(self) -> None:
        module = SimpleNamespace(register_router=lambda: "FACTORY")
        assert resolve_addon_router(module) == "FACTORY"  # type: ignore[arg-type]

    def test_missing_hook_returns_none(self) -> None:
        assert resolve_addon_router(SimpleNamespace()) is None  # type: ignore[arg-type]

    def test_broken_factory_raises_loudly(self) -> None:
        def boom() -> object:
            raise RuntimeError("broken")

        with pytest.raises(RuntimeError, match="broken"):
            resolve_addon_router(SimpleNamespace(register_router=boom))  # type: ignore[arg-type]


class TestResolveWorkerOps:
    def test_attr_preferred_and_copied(self) -> None:
        ops = {"ingest": object()}
        module = SimpleNamespace(WORKER_OPS=ops, register_worker=lambda: {"other": 1})
        resolved = resolve_worker_ops(module)  # type: ignore[arg-type]
        assert resolved == ops
        assert resolved is not ops

    def test_factory_fallback(self) -> None:
        module = SimpleNamespace(register_worker=lambda: {"op": 1})
        assert resolve_worker_ops(module) == {"op": 1}  # type: ignore[arg-type]

    def test_missing_hook_empty(self) -> None:
        assert resolve_worker_ops(SimpleNamespace()) == {}  # type: ignore[arg-type]

    def test_non_dict_factory_result_empty(self) -> None:
        module = SimpleNamespace(register_worker=lambda: ["not", "a", "dict"])
        assert resolve_worker_ops(module) == {}  # type: ignore[arg-type]


class TestDispatchOpPrecedence:
    """Record snapshot > module attr > register_worker() factory.

    Exercises the same resolution expression ``addon_dispatch`` uses.
    """

    def _ops_for(self, record: object) -> dict:
        module = getattr(record, "module")
        return getattr(record, "worker_ops", None) or resolve_worker_ops(module)

    def test_record_snapshot_wins_over_module(self) -> None:
        module = SimpleNamespace(WORKER_OPS={"op": "MODULE"})
        record = _record(module=module, worker_ops={"op": "RECORD"})
        assert self._ops_for(record) == {"op": "RECORD"}

    def test_module_attr_used_when_record_empty(self) -> None:
        module = SimpleNamespace(WORKER_OPS={"op": "MODULE"})
        record = _record(module=module, worker_ops={})
        assert self._ops_for(record) == {"op": "MODULE"}

    def test_factory_used_when_no_attr(self) -> None:
        module = SimpleNamespace(register_worker=lambda: {"op": "FACTORY"})
        record = _record(module=module, worker_ops={})
        assert self._ops_for(record) == {"op": "FACTORY"}
