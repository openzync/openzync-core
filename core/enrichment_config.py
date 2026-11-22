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

from core.exceptions import ValidationError
from workers.tasks.base import (
    ENRICHMENT_BLOB_TEXT,
    ENRICHMENT_CLASSIFICATION,
    ENRICHMENT_EMBEDDING,
    ENRICHMENT_ENTITIES,
    ENRICHMENT_ENTITY_LINKS,
    ENRICHMENT_FACTS,
    ENRICHMENT_OBSERVATIONS,
    ENRICHMENT_STRUCTURED_EXTRACTION,
    LLM_INVALIDATION_BIT,
)

ENRICHMENT_KEYS: list[str] = [
    "entities",
    "embedding",
    "facts",
    "entity_links",
    "classification",
    "structured",
    "observations",
    "blob_text",
    "llm_invalidation",
]
"""All per-episode enrichment toggles. Order is stable — do not reorder."""

ENRICHMENT_KEY_BITS: dict[str, int] = {
    "entities": ENRICHMENT_ENTITIES,
    "embedding": ENRICHMENT_EMBEDDING,
    "facts": ENRICHMENT_FACTS,
    "entity_links": ENRICHMENT_ENTITY_LINKS,
    "classification": ENRICHMENT_CLASSIFICATION,
    "structured": ENRICHMENT_STRUCTURED_EXTRACTION,
    "observations": ENRICHMENT_OBSERVATIONS,
    "blob_text": ENRICHMENT_BLOB_TEXT,
    "llm_invalidation": LLM_INVALIDATION_BIT,
}
"""Toggle key → ``episodes.enrichment_status`` bit (mark-skipped stamping)."""

_ALL_ON: dict[str, bool] = {key: True for key in ENRICHMENT_KEYS}


def all_enabled() -> dict[str, bool]:
    """Return a fresh all-``True`` enrichment dict (schema defaults, old jobs)."""
    return dict(_ALL_ON)


def _as_dict(layer: dict[str, bool] | Any | None) -> dict[str, bool] | None:
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
    system: dict[str, bool] | Any | None = None,
    org: dict[str, bool] | Any | None = None,
    override: dict[str, bool] | Any | None = None,
    request_param: dict[str, bool] | Any | None = None,
) -> dict[str, bool]:
    """Merge enrichment layers, lowest → highest precedence.

    Precedence: *system* < *org* < *override* < *request_param*.  Any key
    absent from every layer defaults to ``True``.  Unknown keys raise —
    they indicate a client/schema version skew that must not be absorbed.

    Args:
        system: System root default enrichment dict (or config object).
        org: Org/user stored override (or config object).
        override: Per-user stored override (or config object).
        request_param: Per-ingest request param (highest precedence).

    Returns:
        A complete ``dict[str, bool]`` with every key in
        :data:`ENRICHMENT_KEYS`.

    Raises:
        ValidationError: On unknown toggle keys.
    """
    resolved: dict[str, bool] = dict(_ALL_ON)
    for layer in (system, org, override, request_param):
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
            resolved[key] = bool(value)
    return resolved


def validate_enrichment_combo(
    enabled: dict[str, bool],
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
