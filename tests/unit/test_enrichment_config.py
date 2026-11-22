"""Unit tests for enrichment toggle resolution + combo validation.

Covers the user-dictated list A (config resolution) and B (combo rules):

A. resolve_enrichment contract — all-None/empty defaults, layer precedence,
   unknown-key and non-bool rejection (core + every schema), key-set parity,
   3-arg signature.
B. validate_enrichment_combo dependency rules.

No I/O: pure function + Pydantic validation only.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError as PydanticValidationError

from core.enrichment_bits import ENRICHMENT_KEYS
from core.enrichment_config import (
    resolve_enrichment,
    validate_enrichment_combo,
)
from core.exceptions import ValidationError

pytestmark = pytest.mark.unit


def _all_on() -> dict[str, bool]:
    """Fresh all-True enrichment map."""
    return {key: True for key in ENRICHMENT_KEYS}


# ═══════════════════════════════════════════════════════════════════════════════
# A(1). all-None / all-empty resolve to all 9 True
# ═══════════════════════════════════════════════════════════════════════════════


class TestResolveDefaults:
    """Missing layers default every toggle to ``True``."""

    def test_all_none_resolves_all_true(self) -> None:
        """``resolve_enrichment(None, None, None)`` → all 9 ``True``."""
        resolved = resolve_enrichment(None, None, None)
        assert resolved == _all_on()
        assert len(resolved) == 9

    def test_all_empty_dicts_resolve_all_true(self) -> None:
        """``resolve_enrichment({}, {}, {})`` → all 9 ``True``."""
        resolved = resolve_enrichment({}, {}, {})
        assert resolved == _all_on()

    def test_mixed_none_and_empty_resolve_all_true(self) -> None:
        """Any mix of ``None`` / ``{}`` layers → all 9 ``True``."""
        resolved = resolve_enrichment(None, {}, None)
        assert resolved == _all_on()


# ═══════════════════════════════════════════════════════════════════════════════
# A(2). Precedence: system < org < per-request, per-layer win
# ═══════════════════════════════════════════════════════════════════════════════


class TestResolvePrecedence:
    """Each layer wins over every layer below it."""

    def test_system_layer_applies(self) -> None:
        """A system-only override is honoured."""
        resolved = resolve_enrichment({"facts": False}, None, None)
        assert resolved["facts"] is False
        assert resolved["entities"] is True

    def test_org_overrides_system(self) -> None:
        """Org stored override wins over the system default."""
        resolved = resolve_enrichment({"facts": False}, {"facts": True}, None)
        assert resolved["facts"] is True

    def test_request_overrides_org_and_system(self) -> None:
        """Per-request param wins over org and system."""
        resolved = resolve_enrichment(
            {"facts": False, "entities": False},
            {"facts": True, "entities": False},
            {"entities": True},
        )
        assert resolved["facts"] is True
        assert resolved["entities"] is True

    def test_request_can_disable_org_enabled(self) -> None:
        """Precedence works in the disable direction too."""
        resolved = resolve_enrichment(None, {"facts": True}, {"facts": False})
        assert resolved["facts"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# A(3). Unknown keys → ValidationError in core + each schema (→ 422)
# ═══════════════════════════════════════════════════════════════════════════════


class TestUnknownKeys:
    """Unknown toggle keys are loud everywhere (client/schema skew)."""

    def test_core_rejects_unknown_key(self) -> None:
        """``resolve_enrichment`` raises on an unknown key."""
        with pytest.raises(ValidationError, match="Unknown enrichment keys"):
            resolve_enrichment(None, None, {"bogus_key": True})  # type: ignore[dict-item]

    @pytest.mark.parametrize("layer", ["system", "org", "request_param"])
    def test_core_rejects_unknown_key_in_every_layer(self, layer: str) -> None:
        """No layer absorbs a skew key — system, org, and request alike."""
        kwargs: dict[str, dict[str, bool] | None] = {
            "system": None,
            "org": None,
            "request_param": None,
        }
        kwargs[layer] = {"bogus_key": True}  # type: ignore[dict-item]
        with pytest.raises(ValidationError, match="Unknown enrichment keys"):
            resolve_enrichment(**kwargs)  # type: ignore[arg-type]

    def test_memory_schema_rejects_unknown_key(self) -> None:
        """``IngestMemoryRequest`` unknown key → pydantic error (→ 422)."""
        from schemas.memory import IngestMemoryRequest

        with pytest.raises(PydanticValidationError, match="Unknown enrichment keys"):
            IngestMemoryRequest(
                session_id="s",
                messages=[{"role": "user", "content": "hi"}],
                enrichments={"bogus_key": True},  # type: ignore[dict-item]
            )

    def test_org_base_schema_rejects_unknown_key(self) -> None:
        """``OrgConfigBase`` unknown key → pydantic error (→ 422)."""
        from schemas.organization_config import OrgConfigBase

        with pytest.raises(PydanticValidationError, match="Unknown enrichment keys"):
            OrgConfigBase(enrichment={"bogus_key": True})  # type: ignore[dict-item]

    def test_org_update_schema_rejects_unknown_key(self) -> None:
        """``UpdateOrgConfigRequest`` unknown key → pydantic error (→ 422)."""
        from schemas.organization_config import UpdateOrgConfigRequest

        with pytest.raises(PydanticValidationError, match="Unknown enrichment keys"):
            UpdateOrgConfigRequest(enrichment={"bogus_key": True})  # type: ignore[dict-item]

    def test_system_schema_rejects_unknown_key(self) -> None:
        """``SystemConfigUpdate`` unknown key → pydantic error (→ 422)."""
        from schemas.system_config import SystemConfigUpdate

        with pytest.raises(PydanticValidationError, match="Unknown enrichment keys"):
            SystemConfigUpdate(enrichment={"bogus_key": True})  # type: ignore[dict-item]


# ═══════════════════════════════════════════════════════════════════════════════
# A(4). Non-bool values → ValidationError (never coerce truthy strings)
# ═══════════════════════════════════════════════════════════════════════════════


class TestNonBoolValues:
    """``bool("false")`` is ``True`` — only real booleans pass."""

    @pytest.mark.parametrize("bad", ["false", "true", 0, 1, 0.0, [], None])
    def test_core_rejects_non_bool(self, bad: object) -> None:
        """``resolve_enrichment`` raises for truthy/falsy non-booleans."""
        with pytest.raises(ValidationError, match="must be a boolean"):
            resolve_enrichment(None, None, {"facts": bad})  # type: ignore[dict-item]

    @pytest.mark.parametrize("bad", ["false", 0, 1])
    def test_memory_schema_rejects_non_bool(self, bad: object) -> None:
        """``IngestMemoryRequest`` non-bool toggle → pydantic error (→ 422)."""
        from schemas.memory import IngestMemoryRequest

        with pytest.raises(PydanticValidationError, match="must be a boolean"):
            IngestMemoryRequest(
                session_id="s",
                messages=[{"role": "user", "content": "hi"}],
                enrichments={"facts": bad},  # type: ignore[dict-item]
            )

    @pytest.mark.parametrize("bad", ["false", 0, 1])
    def test_org_base_schema_rejects_non_bool(self, bad: object) -> None:
        """``OrgConfigBase`` non-bool toggle → pydantic error (→ 422)."""
        from schemas.organization_config import OrgConfigBase

        with pytest.raises(PydanticValidationError):
            OrgConfigBase(enrichment={"facts": bad})  # type: ignore[dict-item]

    @pytest.mark.parametrize("bad", ["false", 0, 1])
    def test_system_schema_rejects_non_bool(self, bad: object) -> None:
        """``SystemConfigUpdate`` non-bool toggle → pydantic error (→ 422)."""
        from schemas.system_config import SystemConfigUpdate

        with pytest.raises(PydanticValidationError):
            SystemConfigUpdate(enrichment={"facts": bad})  # type: ignore[dict-item]

    @pytest.mark.parametrize("bad", ["false", 0, 1])
    def test_org_update_schema_rejects_non_bool(self, bad: object) -> None:
        """``UpdateOrgConfigRequest`` non-bool toggle → pydantic error.

        (``None`` stays legal here — it is a staged null-reset, not a
        value; see the org-config null-reset tests.)
        """
        from schemas.organization_config import UpdateOrgConfigRequest

        with pytest.raises(PydanticValidationError):
            UpdateOrgConfigRequest(enrichment={"facts": bad})  # type: ignore[dict-item]


# ═══════════════════════════════════════════════════════════════════════════════
# A(5). Key-set parity: bits == memory == org == system
# ═══════════════════════════════════════════════════════════════════════════════


class TestKeySetParity:
    """Every toggle-key copy matches the canonical registry."""

    def test_all_key_sets_match_canonical(self) -> None:
        """bits, memory, org, and system key sets are identical."""
        from schemas.memory import ENRICHMENT_KEYS as MEMORY_KEYS
        from schemas.organization_config import ENRICHMENT_KEYS as ORG_KEYS
        from schemas.system_config import ENRICHMENT_KEYS as SYSTEM_KEYS

        canonical = set(ENRICHMENT_KEYS)
        assert set(MEMORY_KEYS) == canonical
        assert set(ORG_KEYS) == canonical
        assert set(SYSTEM_KEYS) == canonical
        assert len(canonical) == 9


# ═══════════════════════════════════════════════════════════════════════════════
# A(6). resolve_enrichment is 3-arg (system, org, request_param)
# ═══════════════════════════════════════════════════════════════════════════════


class TestResolveSignature:
    """The 3-layer contract — the dead 4th ``override`` arg stays removed."""

    def test_resolve_enrichment_is_three_arg(self) -> None:
        """Exactly ``(system, org, request_param)`` — no more, no less."""
        params = list(inspect.signature(resolve_enrichment).parameters)
        assert params == ["system", "org", "request_param"]


# ═══════════════════════════════════════════════════════════════════════════════
# B(7-9). Cross-toggle dependency rules
# ═══════════════════════════════════════════════════════════════════════════════


class TestComboDependencies:
    """facts/entity_links need entities; llm_invalidation needs facts."""

    def test_facts_without_entities_errors(self) -> None:
        """B(7): ``facts=True`` with ``entities=False`` → 422."""
        enabled = _all_on()
        enabled["entities"] = False
        with pytest.raises(ValidationError, match="requires 'entities'"):
            validate_enrichment_combo(enabled)

    def test_entity_links_without_entities_errors(self) -> None:
        """B(8): ``entity_links=True`` with ``entities=False`` → 422."""
        enabled = _all_on()
        enabled["entities"] = False
        enabled["facts"] = False
        enabled["llm_invalidation"] = False
        with pytest.raises(ValidationError, match="requires 'entities'"):
            validate_enrichment_combo(enabled)

    def test_llm_invalidation_without_facts_errors(self) -> None:
        """B(9): ``llm_invalidation=True`` with ``facts=False`` → 422."""
        enabled = _all_on()
        enabled["facts"] = False
        with pytest.raises(ValidationError, match="requires 'facts'"):
            validate_enrichment_combo(enabled)


# ═══════════════════════════════════════════════════════════════════════════════
# B(10). embedding=False vs graph_search_type
# ═══════════════════════════════════════════════════════════════════════════════


class TestEmbeddingSearchCompat:
    """Disabling the embedding leg must not silently drop vector retrieval."""

    @pytest.mark.parametrize("search_type", ["vector", "hybrid"])
    def test_embedding_off_with_vector_search_errors(self, search_type: str) -> None:
        """B(10): ``embedding=False`` + vector/hybrid → 422."""
        enabled = _all_on()
        enabled["embedding"] = False
        with pytest.raises(ValidationError, match="cannot be disabled"):
            validate_enrichment_combo(enabled, graph_search_type=search_type)

    @pytest.mark.parametrize("search_type", ["bm25", None])
    def test_embedding_off_with_bm25_or_unset_ok(self, search_type: str | None) -> None:
        """B(10): ``embedding=False`` + bm25/unset → fine."""
        enabled = _all_on()
        enabled["embedding"] = False
        validate_enrichment_combo(enabled, graph_search_type=search_type)


# ═══════════════════════════════════════════════════════════════════════════════
# B(11). all-on / all-off pass; request-schema unknown key → 422
# ═══════════════════════════════════════════════════════════════════════════════


class TestComboHappyPaths:
    """Boundary combos validate cleanly."""

    def test_all_on_ok(self) -> None:
        """B(11): every toggle on → no error."""
        validate_enrichment_combo(_all_on())

    def test_all_off_ok(self) -> None:
        """B(11): every toggle off (no search type) → no error."""
        validate_enrichment_combo({key: False for key in ENRICHMENT_KEYS})

    def test_ingest_request_unknown_key_422(self) -> None:
        """B(11): ``IngestMemoryRequest`` unknown key → pydantic error."""
        from schemas.memory import IngestMemoryRequest

        with pytest.raises(PydanticValidationError, match="Unknown enrichment keys"):
            IngestMemoryRequest(
                session_id="s",
                messages=[{"role": "user", "content": "hi"}],
                enrichments={"nope": False},  # type: ignore[dict-item]
            )
