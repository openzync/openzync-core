"""Per-episode enrichment configurability — 3-layer resolution + validation.

Layering (lowest → highest precedence)::

    system root default < org/user stored override < per-ingest request param

Every layer is an optional ``dict[str, bool]``; any missing key defaults to
``True`` (all-ON).  Callers extract the raw dict from their config object
(``SystemConfigResponse.enrichment``, ``OrgConfigBase.enrichment``) and pass
it here — this module never touches the DB, Redis, or Pydantic layers.

Disabled steps skip work but still stamp their
``episodes.enrichment_status`` bit (mark-skipped), so ``reconcile_enrichment``
and monitoring see a complete bitmask instead of a perpetually stale episode.
"""

from __future__ import annotations

from typing import Any

from core.enrichment_bits import (
    ENRICHMENT_KEY_BITS,
    ENRICHMENT_KEYS,
    EnrichmentMap,
)
from core.exceptions import ValidationError

__all__ = [
    "ENRICHMENT_KEY_BITS",
    "ENRICHMENT_KEYS",
    "all_enabled",
    "resolve_enrichment",
    "validate_enrichment_combo",
]
"""Re-exported from :mod:`core.enrichment_bits` (canonical home) so
existing ``core.enrichment_config`` import paths keep working."""

_ALL_ON: EnrichmentMap = {key: True for key in ENRICHMENT_KEYS}


def all_enabled() -> EnrichmentMap:
    """Return a fresh all-``True`` enrichment dict (schema defaults, old jobs)."""
    return dict(_ALL_ON)


def _as_dict(layer: EnrichmentMap | Any | None) -> EnrichmentMap | None:
    """Unwrap a layer to a plain dict.

    Accepts a raw ``dict`` or any object carrying an ``enrichment`` attribute
    (``OrgConfigBase``, ``SystemConfigResponse``).  Anything else is a loud
    wiring error — never silently treated as all-ON.
    """
    if layer is None:
        return None
    if isinstance(layer, dict):
        return layer
    nested = getattr(layer, "enrichment", None)
    if nested is None:
        return None
    if isinstance(nested, dict):
        return nested
    raise ValidationError(f"Invalid enrichment layer: {type(layer).__name__}")


def resolve_enrichment(
    system: EnrichmentMap | Any | None = None,
    org: EnrichmentMap | Any | None = None,
    request_param: EnrichmentMap | Any | None = None,
) -> EnrichmentMap:
    """Merge enrichment layers, lowest → highest precedence.

    Precedence: *system* < *org* < *request_param*.  Any key
    absent from every layer defaults to ``True``.  Unknown keys raise —
    they indicate a client/schema version skew that must not be absorbed.

    .. note:: A previous 4-layer signature carried a dead ``override``
       parameter that no caller ever set (always ``None``).  It was
       removed — the documented contract is 3 layers: the per-user
       override IS the ``org`` layer (both read the same
       ``users.config_override`` row).  ``BREAKING:`` positional callers
       passing four args must drop the third.

    Args:
        system: System root default enrichment dict (or config object).
        org: Org/user stored override (or config object).
        request_param: Per-ingest request param (highest precedence).

    Returns:
        A complete ``dict[str, bool]`` with every key in
        :data:`ENRICHMENT_KEYS`.

    Raises:
        ValidationError: On unknown toggle keys or non-boolean values
            (``bool("false")`` is ``True`` — truthy coercion would
            silently enable a toggle the caller tried to disable).
    """
    resolved: EnrichmentMap = dict(_ALL_ON)
    for layer in (system, org, request_param):
        raw = _as_dict(layer)
        if not raw:
            continue
        unknown = set(raw) - set(ENRICHMENT_KEYS)
        if unknown:
            raise ValidationError(
                f"Unknown enrichment keys: {sorted(unknown)}. "
                f"Expected a subset of: {ENRICHMENT_KEYS}."
            )
        for key, value in raw.items():
            if not isinstance(value, bool):
                raise ValidationError(
                    f"Enrichment toggle {key!r} must be a boolean, "
                    f"got {type(value).__name__}."
                )
            resolved[key] = value
    return resolved


def validate_enrichment_combo(
    enabled: EnrichmentMap,
    graph_search_type: str | None = None,
) -> None:
    """Enforce cross-toggle dependencies; raises ``ValidationError`` (→ 422).

    Rules:
    - ``facts`` requires ``entities``.
    - ``entity_links`` requires ``entities``.
    - ``llm_invalidation`` requires ``facts``.
    - ``embedding=False`` is incompatible with ``graph_search_type`` in
      ``{"vector", "hybrid"}`` (retrieval would silently lose its vector leg).

    Args:
        enabled: Complete enrichment dict from :func:`resolve_enrichment`.
        graph_search_type: Effective graph search type (org or system).

    Raises:
        ValidationError: On any violated dependency.
    """
    unknown = set(enabled) - set(ENRICHMENT_KEYS)
    if unknown:
        raise ValidationError(
            f"Unknown enrichment keys: {sorted(unknown)}. "
            f"Expected a subset of: {ENRICHMENT_KEYS}."
        )
    if enabled.get("facts") and not enabled.get("entities"):
        raise ValidationError("enrichment 'facts' requires 'entities' to be enabled.")
    if enabled.get("entity_links") and not enabled.get("entities"):
        raise ValidationError(
            "enrichment 'entity_links' requires 'entities' to be enabled."
        )
    if enabled.get("llm_invalidation") and not enabled.get("facts"):
        raise ValidationError(
            "enrichment 'llm_invalidation' requires 'facts' to be enabled."
        )
    if not enabled.get("embedding", True) and (graph_search_type or "").lower() in {
        "vector",
        "hybrid",
    }:
        raise ValidationError(
            f"enrichment 'embedding' cannot be disabled when graph_search_type "
            f"is {graph_search_type!r} — retrieval would lose its vector leg."
        )


def _assert_schema_key_parity() -> None:
    """Fail import when a ``schemas/`` toggle-key copy drifts from canonical.

    ``schemas/`` must never import from ``core/``, so each schema module
    keeps a local ``ENRICHMENT_KEYS`` copy.  This check (run once at
    import time) makes drift loud instead of silent: a renamed/added key
    in one layer would otherwise 422-or-ignore across layers.
    ``schemas/system_config.py`` needs no separate check — it imports
    the constant from ``schemas/organization_config.py`` structurally.
    """
    from schemas.memory import ENRICHMENT_KEYS as _MEMORY_KEYS
    from schemas.organization_config import ENRICHMENT_KEYS as _ORG_KEYS

    canonical = set(ENRICHMENT_KEYS)
    for name, keys in (
        ("schemas.memory", _MEMORY_KEYS),
        ("schemas.organization_config", _ORG_KEYS),
    ):
        if set(keys) != canonical:
            raise ImportError(
                f"{name}.ENRICHMENT_KEYS drifted from "
                f"core.enrichment_bits.ENRICHMENT_KEYS: "
                f"{sorted(set(keys) ^ canonical)}."
            )


_assert_schema_key_parity()
