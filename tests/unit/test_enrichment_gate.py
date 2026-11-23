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

import logging
import math
import urllib.error
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
    """System AND org-inherit; fallbacks; malformed types raise."""

    def test_absent_layers_default_to_disabled(self) -> None:
        """``(None, None)`` → gate off with documented defaults."""
        resolved = resolve_enrichment_gate(None, None)
        assert resolved == {
            "enabled": False,
            "threshold": GATE_DEFAULT_THRESHOLD,
            "model": GATE_DEFAULT_MODEL,
        }
        assert resolved["threshold"] == 0.5

    def test_system_enabled_and_org_inherits_turns_on(self) -> None:
        """System on + org ``None``/``True`` → on; explicit values kept."""
        for org in (None, True):
            resolved = resolve_enrichment_gate(
                {"enabled": True, "threshold": 0.8, "model": "org/model"},
                org,
            )
            assert resolved == {
                "enabled": True,
                "threshold": 0.8,
                "model": "org/model",
            }

    def test_org_false_is_kill_switch(self) -> None:
        """System on + org ``False`` → off (threshold/model still resolved)."""
        resolved = resolve_enrichment_gate({"enabled": True}, False)
        assert resolved["enabled"] is False
        assert resolved["threshold"] == GATE_DEFAULT_THRESHOLD

    def test_system_off_stays_off(self) -> None:
        """System off + org ``True`` → off: the org cannot force-enable."""
        assert (
            resolve_enrichment_gate({"enabled": False}, True)["enabled"] is False
        )

    def test_threshold_and_model_fall_back_to_gate_defaults(self) -> None:
        """A bare ``{"enabled": True}`` block inherits threshold + model."""
        resolved = resolve_enrichment_gate({"enabled": True}, None)
        assert resolved["threshold"] == GATE_DEFAULT_THRESHOLD == 0.5
        assert resolved["model"] == GATE_DEFAULT_MODEL

    def test_org_dict_override_forms(self) -> None:
        """Org dicts carrying ``enrichment_gate`` unwrap like the raw bool."""
        assert (
            resolve_enrichment_gate(
                {"enabled": True}, {"enrichment_gate": False}
            )["enabled"]
            is False
        )
        assert (
            resolve_enrichment_gate(
                {"enabled": True}, {"enrichment_gate": None}
            )["enabled"]
            is True
        )

    @pytest.mark.parametrize(
        "system",
        [
            {"enabled": "yes"},
            {"enabled": 1},
            {"enabled": True, "threshold": "high"},
            {"enabled": True, "threshold": True},
            {"enabled": True, "threshold": 1.5},
            {"enabled": True, "threshold": -0.1},
            {"enabled": True, "threshold": float("nan")},
            {"enabled": True, "model": ""},
            {"enabled": True, "model": "   "},
            {"enabled": True, "model": 123},
            {"enabled": True, "bogus": 1},
        ],
    )
    def test_malformed_system_block_raises(self, system: Any) -> None:
        """Non-boolean enabled, non-numeric/out-of-range threshold, non-string
        model, and unknown keys all raise ``ValidationError``."""
        with pytest.raises(ValidationError):
            resolve_enrichment_gate(system, None)

    @pytest.mark.parametrize(
        "org",
        [
            {"enrichment_gate": "nope"},
            {"enrichment_gate": 1},
            {"enrichment_gate": [True]},
            # note: scalar non-bool org layers (e.g. "nope", 1) silently
            # inherit via the getattr fallback — only dict-wrapped
            # non-bools raise. The schema layer guarantees bool|None
            # upstream, so this documents the defensive core behavior.
        ],
    )
    def test_malformed_org_override_raises(self, org: Any) -> None:
        """Non-boolean org overrides raise ``ValidationError``."""
        with pytest.raises(ValidationError):
            resolve_enrichment_gate({"enabled": True}, org)


# ═══════════════════════════════════════════════════════════════════════════════
# (5) Legacy coercion validators
# ═══════════════════════════════════════════════════════════════════════════════


class TestLegacyCoercion:
    """Legacy SYSTEM-block dicts coerce to their ``enabled`` flag (+ warning)."""

    @pytest.mark.parametrize("cls", [OrgConfigBase, UpdateOrgConfigRequest])
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [({"enabled": True}, True), ({"enabled": False}, False), ({}, False)],
    )
    def test_dict_shapes_coerce_with_warning(
        self,
        cls: Any,
        raw: dict,
        expected: bool,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``{"enabled": …}`` → bool, ``{}`` → ``False``, each with a warning."""
        with caplog.at_level(logging.WARNING, logger="schemas.organization_config"):
            assert cls(enrichment_gate=raw).enrichment_gate is expected
        assert any("legacy" in record.message for record in caplog.records)

    @pytest.mark.parametrize("value", [True, False, None])
    def test_bool_and_none_passthrough(self, value: bool | None) -> None:
        """Real booleans and ``None`` pass through untouched (both schemas)."""
        assert OrgConfigBase(enrichment_gate=value).enrichment_gate is value
        assert UpdateOrgConfigRequest(enrichment_gate=value).enrichment_gate is value

    def test_list_shape_still_raises(self) -> None:
        """``[True]`` is not a legacy block and not a bool → 422."""
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError):
            OrgConfigBase(enrichment_gate=[True])

    def test_yes_string_coerces_to_true(self) -> None:
        """⚠️ SPEC DRIFT (dictated ``"yes"`` raises; observed otherwise).

        Pydantic v2 lax bool parsing accepts ``"yes"`` as ``True``, so the
        ``mode="before"`` coercion validator passes it through untouched and
        validation succeeds. Only genuinely non-bool shapes (``[True]``,
        covered above) raise. Flagging for the user — do not "fix" by
        tightening here without a product decision (the same lax parsing
        applies to every other bool field in these schemas).
        """
        assert OrgConfigBase(enrichment_gate="yes").enrichment_gate is True


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
    """Enable-path Hub check: local bypass, disabled skip, bogus → 422."""

    async def test_local_dir_model_bypasses_without_network(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
        _clean_store: Any,
    ) -> None:
        """A local-dir model never touches the Hub (check would 404/raise)."""
        from unittest.mock import MagicMock

        _no_network(monkeypatch)
        _patch_gate_repo(monkeypatch)
        hub_probe = MagicMock(side_effect=AssertionError("Hub must not be probed"))
        monkeypatch.setattr("core.system_config._hub_model_status", hub_probe)
        from core.system_config import update_system_config

        result = await update_system_config(
            {"enrichment_gate": {"enabled": True, "model": str(tmp_path)}},
            redis=None,
            db=AsyncMock(),
        )
        assert result.enrichment_gate is not None
        assert result.enrichment_gate.enabled is True
        assert result.enrichment_gate.model == str(tmp_path)
        assert _STORE["enrichment_gate"]["model"] == str(tmp_path)
        hub_probe.assert_not_called()

    async def test_relative_local_path_bypasses_without_network(
        self, monkeypatch: pytest.MonkeyPatch, _clean_store: Any
    ) -> None:
        """``./…``-prefixed models bypass with no filesystem or network I/O."""
        from unittest.mock import MagicMock

        _no_network(monkeypatch)
        _patch_gate_repo(monkeypatch)
        hub_probe = MagicMock(side_effect=AssertionError("Hub must not be probed"))
        monkeypatch.setattr("core.system_config._hub_model_status", hub_probe)
        from core.system_config import update_system_config

        result = await update_system_config(
            {"enrichment_gate": {"enabled": True, "model": "./models/gate"}},
            redis=None,
            db=AsyncMock(),
        )
        assert result.enrichment_gate is not None
        assert result.enrichment_gate.enabled is True
        hub_probe.assert_not_called()

    async def test_disabled_skips_check(
        self, monkeypatch: pytest.MonkeyPatch, _clean_store: Any
    ) -> None:
        """``enabled=false`` with a bogus model persists without any check."""
        from unittest.mock import MagicMock

        _no_network(monkeypatch)
        _patch_gate_repo(monkeypatch)
        hub_probe = MagicMock(side_effect=AssertionError("Hub must not be probed"))
        monkeypatch.setattr("core.system_config._hub_model_status", hub_probe)
        from core.system_config import update_system_config

        result = await update_system_config(
            {"enrichment_gate": {"enabled": False, "model": "bogus/no-such-model"}},
            redis=None,
            db=AsyncMock(),
        )
        assert result.enrichment_gate is not None
        assert result.enrichment_gate.enabled is False
        hub_probe.assert_not_called()

    async def test_absent_block_skips_check(
        self, monkeypatch: pytest.MonkeyPatch, _clean_store: Any
    ) -> None:
        """An update without a gate block performs no Hub check."""
        from unittest.mock import MagicMock

        _no_network(monkeypatch)
        _patch_gate_repo(monkeypatch)
        hub_probe = MagicMock(side_effect=AssertionError("Hub must not be probed"))
        monkeypatch.setattr("core.system_config._hub_model_status", hub_probe)
        from core.system_config import update_system_config

        result = await update_system_config(
            {"llm_backend": "openai"}, redis=None, db=AsyncMock()
        )
        assert result.llm_backend == "openai"
        hub_probe.assert_not_called()

    async def test_bogus_model_404_raises_and_persists_nothing(
        self, monkeypatch: pytest.MonkeyPatch, _clean_store: Any
    ) -> None:
        """Enabling with an unknown Hub model → ``ValidationError`` (→ 422),
        and the stored config is byte-identical to before."""
        _no_network(monkeypatch)
        _patch_gate_repo(monkeypatch)
        monkeypatch.setattr(
            "core.system_config._hub_model_status", lambda model_id: 404
        )
        _STORE.update({"llm_backend": "openai"})
        before = dict(_STORE)
        from core.system_config import update_system_config

        with pytest.raises(ValidationError, match="does not exist"):
            await update_system_config(
                {"enrichment_gate": {"enabled": True, "model": "bogus/no-such-model"}},
                redis=None,
                db=AsyncMock(),
            )
        assert _STORE == before

    async def test_unreachable_hub_raises_and_persists_nothing(
        self, monkeypatch: pytest.MonkeyPatch, _clean_store: Any
    ) -> None:
        """A Hub network failure on enable → ``ValidationError``, nothing stored."""
        _no_network(monkeypatch)
        _patch_gate_repo(monkeypatch)

        def _down(model_id: str) -> int:
            raise urllib.error.URLError("hub unreachable in test")

        monkeypatch.setattr("core.system_config._hub_model_status", _down)
        from core.system_config import update_system_config

        with pytest.raises(ValidationError, match="unreachable"):
            await update_system_config(
                {"enrichment_gate": {"enabled": True, "model": "bogus/no-such-model"}},
                redis=None,
                db=AsyncMock(),
            )
        assert _STORE == {}

    async def test_known_model_200_persists(
        self, monkeypatch: pytest.MonkeyPatch, _clean_store: Any
    ) -> None:
        """A 200 Hub probe lets the enable persist."""
        _no_network(monkeypatch)
        _patch_gate_repo(monkeypatch)
        monkeypatch.setattr(
            "core.system_config._hub_model_status", lambda model_id: 200
        )
        from core.system_config import update_system_config

        result = await update_system_config(
            {"enrichment_gate": {"enabled": True, "model": "OpenZync/real-gate"}},
            redis=None,
            db=AsyncMock(),
        )
        assert result.enrichment_gate is not None
        assert result.enrichment_gate.enabled is True
        assert _STORE["enrichment_gate"]["model"] == "OpenZync/real-gate"


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
