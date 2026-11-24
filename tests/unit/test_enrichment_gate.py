"""Unit tests for the enrichment gate feature (public-contract only).

Covers the user-dictated list, items 1–7 — hermetic: no Hub network, no
live model download.  Controllable scoring is injected by stubbing the two
load/infer seams (``_ensure_model`` / ``_infer``); load-failure paths stub
``_load_pair`` so the real fail-open handling in ``should_skip`` / ``score``
still runs.  No test asserts on gate internals (no ``_GATE_CACHE``,
lock, or call-count assertions) — outcomes only, so the concurrent
refactor (score/skip dedup, cache, lock reorder) cannot break these.

A belt-and-braces guard replaces ``urllib.request.urlopen`` with a bomb
in every test class that must stay offline: any real network attempt
fails the test loudly instead of hanging on a connection timeout.
"""

from __future__ import annotations

import math
import urllib.request
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from core.enrichment_config import (
    GATE_DEFAULT_MODEL,
    GATE_DEFAULT_THRESHOLD,
    resolve_enrichment_gate,
)
from core.exceptions import ValidationError
from schemas.organization_config import (
    EnrichmentGateOrgConfig,
    OrgConfigBase,
    UpdateOrgConfigRequest,
)

pytestmark = pytest.mark.unit


def _sigmoid(x: float) -> float:
    """Reference sigmoid for expected-score assertions."""
    return 1.0 / (1.0 + math.exp(-x))


def _stub_scorer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    logit: float = 0.0,
    load_ok: bool = True,
    infer_exc: Exception | None = None,
) -> None:
    """Stub the gate load/infer seams with a deterministic signal.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        logit: Logit returned by the stubbed infer (sigmoided by the gate).
        load_ok: When ``False``, the stubbed load returns ``None``
            (unusable model → fail-open path).
        infer_exc: When set, the stubbed infer raises it (infer-failure
            fail-open path).
    """

    async def _fake_ensure_model(model_name: str) -> Any | None:
        if not load_ok:
            return None
        return (object(), object())

    def _fake_infer(tokenizer: Any, model: Any, text: str) -> float:
        if infer_exc is not None:
            raise infer_exc
        return logit

    monkeypatch.setattr("core.enrichment_gate._ensure_model", _fake_ensure_model)
    monkeypatch.setattr("core.enrichment_gate._infer", _fake_infer)


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``urlopen`` with a bomb — any real HTTP attempt fails loudly."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network access attempted in hermetic unit test")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)


# ═══════════════════════════════════════════════════════════════════════════════
# (1) should_skip semantics
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestShouldSkipSemantics:
    """High score enriches, low score skips, boundary is strict ``<``."""

    async def test_high_score_enriches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Logit +5 (≈0.993) → ``(False, ~1.0)``."""
        _no_network(monkeypatch)
        _stub_scorer(monkeypatch, logit=5.0)
        from core.enrichment_gate import should_skip

        skip, score = await should_skip("a rich, detailed trip report")
        assert skip is False
        assert score == pytest.approx(_sigmoid(5.0))
        assert score > 0.99

    async def test_low_score_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Logit −5 (≈0.007) → ``(True, ~0.0)``."""
        _no_network(monkeypatch)
        _stub_scorer(monkeypatch, logit=-5.0)
        from core.enrichment_gate import should_skip

        skip, score = await should_skip("ok")
        assert skip is True
        assert score == pytest.approx(_sigmoid(-5.0))
        assert score < 0.01

    async def test_boundary_is_strict_less_than(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Score exactly at threshold does NOT skip (``<``, not ``<=``)."""
        _no_network(monkeypatch)
        _stub_scorer(monkeypatch, logit=0.0)  # sigmoid(0) == 0.5 exactly
        from core.enrichment_gate import should_skip

        skip, score = await should_skip("boundary text", threshold=0.5)
        assert score == pytest.approx(0.5)
        assert skip is False

    async def test_default_threshold_is_half(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Omitting ``threshold`` behaves like ``threshold=0.5``."""
        import inspect

        import core.enrichment_gate as gate

        assert inspect.signature(gate.should_skip).parameters["threshold"].default == 0.5
        assert gate.DEFAULT_THRESHOLD == 0.5
        _no_network(monkeypatch)
        _stub_scorer(monkeypatch, logit=-5.0)
        skip, _ = await gate.should_skip("low-signal text")
        assert skip is True
        _stub_scorer(monkeypatch, logit=5.0)
        skip, _ = await gate.should_skip("high-signal text")
        assert skip is False


# ═══════════════════════════════════════════════════════════════════════════════
# (2) Explicit fail-open
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestFailOpen:
    """Load/infer failures enrich (never skip); blank text skips."""

    async def test_bogus_model_load_failure_enriches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bogus model id (load raises) → ``(False, 0.0)`` via real handling."""
        _no_network(monkeypatch)

        def _broken_load(model_name: str, token: Any | None) -> Any:
            raise OSError(f"no such model: {model_name}")

        monkeypatch.setattr("core.enrichment_gate._load_pair", _broken_load)
        from core.enrichment_gate import should_skip

        skip, score = await should_skip(
            "some episode text", model="bogus/gate-does-not-exist-xyz"
        )
        assert (skip, score) == (False, 0.0)

    async def test_load_failure_enriches_even_above_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-open returns ``False`` explicitly — even where 0.0 < threshold."""
        _no_network(monkeypatch)
        _stub_scorer(monkeypatch, load_ok=False)
        from core.enrichment_gate import should_skip

        skip, score = await should_skip("some episode text", threshold=0.99)
        assert (skip, score) == (False, 0.0)

    async def test_infer_exception_enriches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Infer raising → ``(False, 0.0)``, even at a high threshold."""
        _no_network(monkeypatch)
        _stub_scorer(monkeypatch, logit=5.0, infer_exc=RuntimeError("cuda oom"))
        from core.enrichment_gate import should_skip

        skip, score = await should_skip("some episode text", threshold=0.99)
        assert (skip, score) == (False, 0.0)

    async def test_score_returns_zero_on_load_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``score()`` degrades to ``0.0`` (fail-open signal) on load failure."""
        _no_network(monkeypatch)
        _stub_scorer(monkeypatch, load_ok=False)
        from core.enrichment_gate import score

        assert await score("some episode text") == 0.0

    @pytest.mark.parametrize("text", ["", "   ", "\n\t  "])
    async def test_blank_text_skips_with_zero(
        self, monkeypatch: pytest.MonkeyPatch, text: str
    ) -> None:
        """Blank/whitespace text → ``(True, 0.0)`` at the default threshold."""
        _no_network(monkeypatch)
        _stub_scorer(monkeypatch, logit=5.0)  # must not be consulted
        from core.enrichment_gate import should_skip

        assert await should_skip(text) == (True, 0.0)


# ═══════════════════════════════════════════════════════════════════════════════
# (3) Threshold edges
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestThresholdEdges:
    """``0.0`` skips nothing; ``1.0`` skips everything scorable."""

    async def test_zero_threshold_skips_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``score < 0.0`` is never true — even near-zero scores enrich."""
        _no_network(monkeypatch)
        _stub_scorer(monkeypatch, logit=-5.0)
        from core.enrichment_gate import should_skip

        skip, score = await should_skip("ok", threshold=0.0)
        assert skip is False
        assert score < 0.01

    async def test_zero_threshold_blank_text_enriches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Blank text scores ``0.0`` which is not ``< 0.0`` → no skip."""
        _no_network(monkeypatch)
        from core.enrichment_gate import should_skip

        assert await should_skip("   ", threshold=0.0) == (False, 0.0)

    @pytest.mark.parametrize("logit", [-5.0, 0.0, 5.0])
    async def test_one_threshold_skips_everything_scorable(
        self, monkeypatch: pytest.MonkeyPatch, logit: float
    ) -> None:
        """Sigmoid never reaches ``1.0``, so every real score skips."""
        _no_network(monkeypatch)
        _stub_scorer(monkeypatch, logit=logit)
        from core.enrichment_gate import should_skip

        skip, _ = await should_skip("any text", threshold=1.0)
        assert skip is True


# ═══════════════════════════════════════════════════════════════════════════════
# (4) resolve_enrichment_gate
# ═══════════════════════════════════════════════════════════════════════════════


class TestResolveEnrichmentGate:
    """Org-only gate; fallbacks; malformed types raise."""

    def test_absent_layers_default_to_disabled(self) -> None:
        """``None`` (or no arg) → gate off with documented defaults."""
        for resolved in (resolve_enrichment_gate(None), resolve_enrichment_gate()):
            assert resolved == {
                "enabled": False,
                "threshold": GATE_DEFAULT_THRESHOLD,
                "model": GATE_DEFAULT_MODEL,
            }
        assert resolve_enrichment_gate()["threshold"] == 0.5

    def test_org_enabled_turns_on(self) -> None:
        """Org ``True`` / ``{"enabled": True}`` → on; explicit values kept."""
        for org in (True, {"enabled": True}):
            resolved = resolve_enrichment_gate(org)
            assert resolved["enabled"] is True
            assert resolved["threshold"] == GATE_DEFAULT_THRESHOLD
            assert resolved["model"] == GATE_DEFAULT_MODEL
        assert (
            resolve_enrichment_gate({"enabled": True, "threshold": 0.8})[
                "threshold"
            ]
            == 0.8
        )

    def test_org_false_is_kill_switch(self) -> None:
        """Org ``False`` → off (threshold/model still resolved)."""
        resolved = resolve_enrichment_gate(False)
        assert resolved["enabled"] is False
        assert resolved["threshold"] == GATE_DEFAULT_THRESHOLD
        assert resolved["model"] == GATE_DEFAULT_MODEL

    def test_org_off_stays_off(self) -> None:
        """Org ``{"enabled": False}`` → off: nothing force-enables the gate."""
        assert resolve_enrichment_gate({"enabled": False})["enabled"] is False

    def test_threshold_and_model_fall_back_to_gate_defaults(self) -> None:
        """A bare ``{"enabled": True}`` block inherits threshold + model; a
        ``model`` subkey is stripped (fixed default, never configured)."""
        resolved = resolve_enrichment_gate({"enabled": True})
        assert resolved["threshold"] == GATE_DEFAULT_THRESHOLD == 0.5
        assert resolved["model"] == GATE_DEFAULT_MODEL
        assert (
            resolve_enrichment_gate({"enabled": True, "model": "other/model"})[
                "model"
            ]
            == GATE_DEFAULT_MODEL
        )

    def test_org_dict_override_forms(self) -> None:
        """Org dicts carrying ``enrichment_gate`` unwrap one level."""
        assert (
            resolve_enrichment_gate({"enrichment_gate": False})["enabled"]
            is False
        )
        assert (
            resolve_enrichment_gate(
                {"enrichment_gate": {"enabled": True, "threshold": 0.8}}
            )["threshold"]
            == 0.8
        )
        assert (
            resolve_enrichment_gate({"enrichment_gate": None})["enabled"]
            is False
        )

    @pytest.mark.parametrize(
        "org",
        [
            {"enabled": "yes"},
            {"enabled": 1},
            {"enabled": True, "threshold": "high"},
            {"enabled": True, "threshold": True},
            {"enabled": True, "threshold": 1.5},
            {"enabled": True, "threshold": -0.1},
            {"enabled": True, "threshold": float("nan")},
            {"enabled": True, "bogus": 1},
            {"enrichment_gate": {"enabled": "nope"}},
            {"enrichment_gate": {"enabled": 1}},
        ],
    )
    def test_malformed_org_block_raises(self, org: Any) -> None:
        """Non-boolean enabled, non-numeric/out-of-range threshold, and
        unknown keys all raise ``ValidationError``."""
        with pytest.raises(ValidationError):
            resolve_enrichment_gate(org)


# ═══════════════════════════════════════════════════════════════════════════════
# (5) Legacy coercion validators
# ═══════════════════════════════════════════════════════════════════════════════


class TestLegacyCoercion:
    """Legacy bool/dict gate shapes coerce to the gate object."""

    @pytest.mark.parametrize("cls", [OrgConfigBase, UpdateOrgConfigRequest])
    @pytest.mark.parametrize(
        ("raw", "enabled"),
        [({"enabled": True}, True), ({"enabled": False}, False), ({}, False)],
    )
    def test_dict_shapes_coerce_to_object(
        self,
        cls: Any,
        raw: dict,
        enabled: bool,
    ) -> None:
        """``{"enabled": …}`` → gate object, ``{}`` → disabled object."""
        assert cls(enrichment_gate=raw).enrichment_gate == (
            EnrichmentGateOrgConfig(enabled=enabled, threshold=0.5)
        )

    @pytest.mark.parametrize("value", [True, False, None])
    def test_bool_and_none_passthrough(self, value: bool | None) -> None:
        """Booleans coerce to the gate object; ``None`` passes through
        untouched (both schemas)."""
        for cls in (OrgConfigBase, UpdateOrgConfigRequest):
            gate = cls(enrichment_gate=value).enrichment_gate
            if value is None:
                assert gate is None
            else:
                assert gate == EnrichmentGateOrgConfig(
                    enabled=value, threshold=0.5
                )

    def test_list_shape_still_raises(self) -> None:
        """``[True]`` is not a legacy block and not a bool → 422."""
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError):
            OrgConfigBase(enrichment_gate=[True])

    def test_yes_string_raises(self) -> None:
        """``"yes"`` is not coerced — non-bool/non-dict shapes 422."""
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError):
            OrgConfigBase(enrichment_gate="yes")


# ═══════════════════════════════════════════════════════════════════════════════
# (6) Hub enable-path check (update_system_config; HTTP layer mocked)
# ═══════════════════════════════════════════════════════════════════════════════


_STORE: dict[str, Any] = {}
"""Fake platform-root ``config_override`` (mirrors test_system_config.py)."""

_ROOT_ID = UUID("00000000-0000-0000-0000-00000000aa00")


@pytest.fixture
def _clean_store() -> Any:
    """Reset the fake root store around each Hub-path test."""
    _STORE.clear()
    yield
    _STORE.clear()


def _patch_gate_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake ``UserRepository`` with a platform root backed by ``_STORE``."""

    class _FakeRoot:
        def __init__(self, root_id: UUID, override: dict[str, Any]) -> None:
            self.id = root_id
            self.config_override = dict(override)

    class _FakeRepo:
        def __init__(self, db: Any) -> None:
            self._db = db

        async def get_platform_root(self) -> Any | None:
            return _FakeRoot(_ROOT_ID, _STORE)

        async def set_config_override(
            self, user_id: UUID, config: dict[str, Any]
        ) -> dict[str, Any]:
            _STORE.clear()
            _STORE.update(config)
            return dict(config)

    monkeypatch.setattr(
        "repositories.user_repository.UserRepository", _FakeRepo
    )


@pytest.mark.asyncio
class TestHubEnablePath:
    """System-level gate enable is gone — any gate key is rejected; the
    worker prewarms the hardcoded default model instead."""

    async def test_gate_dict_update_rejected_and_persists_nothing(
        self, monkeypatch: pytest.MonkeyPatch, _clean_store: Any
    ) -> None:
        """A gate-bearing system update → ``ValidationError``; the stored
        config is byte-identical to before."""
        _patch_gate_repo(monkeypatch)
        _STORE.update({"llm_backend": "openai"})
        before = dict(_STORE)
        from core.system_config import update_system_config

        with pytest.raises(ValidationError, match="not editable"):
            await update_system_config(
                {"enrichment_gate": {"enabled": True}},
                redis=None,
                db=AsyncMock(),
            )
        assert _STORE == before

    async def test_gate_schema_update_rejected_422(self) -> None:
        """``SystemConfigUpdate`` carrying a gate key → pydantic error (→ 422)."""
        from pydantic import ValidationError as PydanticValidationError

        from schemas.system_config import SystemConfigUpdate

        with pytest.raises(PydanticValidationError, match="extra_forbidden"):
            SystemConfigUpdate(enrichment_gate={"enabled": True})  # type: ignore[call-arg]

    async def test_prewarm_uses_hardcoded_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``prewarm()`` loads the hardcoded default model (no config lookup)."""
        from unittest.mock import MagicMock

        from core.enrichment_gate import DEFAULT_MODEL, prewarm

        load = MagicMock(return_value=(object(), object()))
        monkeypatch.setattr("core.enrichment_gate._load_pair", load)
        monkeypatch.setattr("core.enrichment_gate._infer", lambda *args: 0.0)

        await prewarm()

        load.assert_called_once()
        assert load.call_args.args[0] == DEFAULT_MODEL


# ═══════════════════════════════════════════════════════════════════════════════
# (7) Score consistency (cache-or-stateless) + threshold applied after scoring
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestScoreConsistency:
    """Same text → same score; thresholds decide per call (outcome-only)."""

    async def test_same_text_twice_is_consistent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two scorings of one text agree (cache hit or stateless recompute)."""
        _no_network(monkeypatch)
        _stub_scorer(monkeypatch, logit=1.0)
        from core.enrichment_gate import score, should_skip

        assert await score("repeat me") == await score("repeat me")
        first = await should_skip("repeat me", threshold=0.5)
        second = await should_skip("repeat me", threshold=0.5)
        assert first == second
        assert first[0] is False  # sigmoid(1.0) ≈ 0.73 ≥ 0.5

    async def test_threshold_applied_after_scoring(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One fixed score (0.5) → skips at 0.51+, enriches at 0.5 and below."""
        _no_network(monkeypatch)
        _stub_scorer(monkeypatch, logit=0.0)
        from core.enrichment_gate import should_skip

        assert await should_skip("mid text", threshold=0.51) == (True, pytest.approx(0.5))
        assert await should_skip("mid text", threshold=0.5) == (False, pytest.approx(0.5))
        assert await should_skip("mid text", threshold=0.1) == (False, pytest.approx(0.5))
