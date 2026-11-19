"""Unit tests for addon dispatch (workers/addon_dispatch.py).

Covers dedup-key determinism, disabled-addon fail-closed, unknown-op 404,
and db/session injection by signature. Redis + session factory are faked.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.exceptions import AuthorizationError, NotFoundError
from workers.addon_dispatch import _dedup_key, addon_dispatch


def _record(**overrides) -> SimpleNamespace:
    base: dict = {"enabled": True, "worker_ops": {}, "module": SimpleNamespace()}
    base.update(overrides)
    return SimpleNamespace(**base)


def _ctx(redis: AsyncMock, record: SimpleNamespace | None) -> dict:
    return {"redis": redis, "addons": {"demo": record} if record is not None else {}}


def _session_cm(session: object) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class TestDedupKey:
    def test_deterministic_and_canonical(self) -> None:
        assert _dedup_key("a", "op", {"x": 1, "y": 2}) == _dedup_key(
            "a", "op", {"y": 2, "x": 1}
        )

    def test_key_shape(self) -> None:
        key = _dedup_key("demo", "ingest", {"x": 1})
        assert key.startswith("addon:dispatch:demo:ingest:")
        digest = key.rsplit(":", 1)[-1]
        assert len(digest) == 32
        int(digest, 16)

    def test_distinct_inputs_distinct_keys(self) -> None:
        assert _dedup_key("a", "op", {"x": 1}) != _dedup_key("a", "op", {"x": 2})
        assert _dedup_key("a", "op1", {}) != _dedup_key("a", "op2", {})


class TestDispatch:
    async def test_unknown_addon_fail_closed(self) -> None:
        redis = AsyncMock()
        redis.set = AsyncMock(return_value=True)
        with pytest.raises(AuthorizationError):
            await addon_dispatch(_ctx(redis, None), "demo", "op")

    async def test_disabled_addon_fail_closed(self) -> None:
        redis = AsyncMock()
        redis.set = AsyncMock(return_value=True)
        with pytest.raises(AuthorizationError):
            await addon_dispatch(_ctx(redis, _record(enabled=False)), "demo", "op")

    async def test_missing_redis_raises(self) -> None:
        with pytest.raises(RuntimeError, match="ctx\\['redis'\\]"):
            await addon_dispatch({}, "demo", "op")

    async def test_unknown_op_404(self) -> None:
        redis = AsyncMock()
        redis.set = AsyncMock(return_value=True)
        with pytest.raises(NotFoundError):
            await addon_dispatch(_ctx(redis, _record(worker_ops={})), "demo", "nope")

    async def test_non_callable_op_returns_none_result(self) -> None:
        redis = AsyncMock()
        redis.set = AsyncMock(return_value=True)
        out = await addon_dispatch(
            _ctx(redis, _record(worker_ops={"op": 42})), "demo", "op"
        )
        assert out == {"status": "ok", "result": None}

    async def test_dedup_hit_short_circuits(self) -> None:
        handler = MagicMock()
        redis = AsyncMock()
        redis.set = AsyncMock(return_value=None)
        out = await addon_dispatch(
            _ctx(redis, _record(worker_ops={"op": handler})), "demo", "op", x=1
        )
        assert out == {"status": "duplicate"}
        handler.assert_not_called()

    async def test_redis_failure_propagates(self) -> None:
        redis = AsyncMock()
        redis.set = AsyncMock(side_effect=ConnectionError("down"))
        with pytest.raises(ConnectionError):
            await addon_dispatch(_ctx(redis, _record()), "demo", "op")

    async def test_sync_handler_no_db(self) -> None:
        def greet(name: str) -> str:
            return f"hi {name}"

        redis = AsyncMock()
        redis.set = AsyncMock(return_value=True)
        out = await addon_dispatch(
            _ctx(redis, _record(worker_ops={"op": greet})), "demo", "op", name="bob"
        )
        assert out == {"status": "ok", "result": "hi bob"}

    async def test_async_handler_no_db(self) -> None:
        async def greet(name: str) -> str:
            return f"hi {name}"

        redis = AsyncMock()
        redis.set = AsyncMock(return_value=True)
        out = await addon_dispatch(
            _ctx(redis, _record(worker_ops={"op": greet})), "demo", "op", name="bob"
        )
        assert out == {"status": "ok", "result": "hi bob"}

    async def test_db_param_injected(self) -> None:
        seen: dict = {}

        async def handler(x: int, db=None) -> str:
            seen["db"] = db
            return f"got {x}"

        assert "db" in inspect.signature(handler).parameters
        session = object()
        factory = MagicMock(return_value=_session_cm(session))
        redis = AsyncMock()
        redis.set = AsyncMock(return_value=True)
        with (
            patch(
                "core.addons.sessions.resolve_addon_factory", return_value=factory
            ),
            patch("core.rls.init_context", AsyncMock()),
        ):
            out = await addon_dispatch(
                {**_ctx(redis, _record(worker_ops={"op": handler})), "engine": object()},
                "demo",
                "op",
                x=1,
            )
        assert out == {"status": "ok", "result": "got 1"}
        assert seen["db"] is session

    async def test_session_param_injected(self) -> None:
        seen: dict = {}

        def handler(session=None) -> str:
            seen["session"] = session
            return "ok"

        session = object()
        factory = MagicMock(return_value=_session_cm(session))
        redis = AsyncMock()
        redis.set = AsyncMock(return_value=True)
        with (
            patch(
                "core.addons.sessions.resolve_addon_factory", return_value=factory
            ),
            patch("core.rls.init_context", AsyncMock()),
        ):
            await addon_dispatch(
                {**_ctx(redis, _record(worker_ops={"op": handler})), "engine": object()},
                "demo",
                "op",
            )
        assert seen["session"] is session

    async def test_record_ops_beat_module_attr(self) -> None:
        def from_record() -> str:
            return "record"

        def from_module() -> str:
            return "module"

        module = SimpleNamespace(WORKER_OPS={"op": from_module})
        redis = AsyncMock()
        redis.set = AsyncMock(return_value=True)
        out = await addon_dispatch(
            _ctx(redis, _record(worker_ops={"op": from_record}, module=module)),
            "demo",
            "op",
        )
        assert out == {"status": "ok", "result": "record"}
