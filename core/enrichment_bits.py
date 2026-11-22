"""Canonical enrichment-status bitmask constants + toggle-key registry.

Single source of truth for ``episodes.enrichment_status`` bits and the
per-episode enrichment toggle keys.  Dependency-free (stdlib only) so both
``core/`` and ``workers/`` can import it without layering cycles —
``core/enrichment_config.py`` previously imported these from
``workers.tasks.base`` (core → workers), which inverted the layering.

Import paths:

- New code: ``from core.enrichment_bits import ENRICHMENT_FACTS, ...``.
- ``workers.tasks.base`` re-exports every name defined here, so all
  existing worker import paths keep working unchanged.
- ``schemas/`` keeps local key-set copies (schemas must never import
  from ``core/``); parity is enforced by the import-time assert in
  :mod:`core.enrichment_config`.
"""

from __future__ import annotations

# ── Enrichment-status bitmask constants ───────────────────────────────────────
# These correspond to the ``episodes.enrichment_status`` integer bitmask column.
# Each bit represents one enrichment step.  Workers check their bit before
# running and set it after completion (or after a permanent failure).

# note: Bit positions are shared across the team and must not be
# reassigned without updating all workers.  Current allocation:
#   bit 0 = entity extraction (extract_entities)
#   bit 1 = embedding generation (embed_episode)
#   bit 2 = fact extraction (extract_facts)
# bit 3 = entity-episode linking (link_entities_to_episode)
#   bit 4 = dialog classification (classify_dialog)
#   bit 5 = structured extraction (extract_structured)
#   bit 6 = deferred graph-topology observations (compute_observations)
#           NOTE: bit 6 is RESERVED but NOT included in ENRICHMENT_ALL.
#           The observations pass is non-blocking and deferred — including
#           it in the ALL mask would gate "fully enriched" status on an
#           unimplemented worker.
#   bit 8 = LLM-driven fact invalidation (inside enrich_episode's facts
#           savepoint).  Deliberately NOT in ENRICHMENT_ALL — it is part of
#           the single LLM enrichment pass, not an independently gating step.

ENRICHMENT_ENTITIES: int = 1 << 0  # bit 0
ENRICHMENT_EMBEDDING: int = 1 << 1  # bit 1
ENRICHMENT_FACTS: int = 1 << 2  # bit 2
ENRICHMENT_ENTITY_LINKS: int = 1 << 3  # bit 3
ENRICHMENT_CLASSIFICATION: int = 1 << 4  # bit 4
ENRICHMENT_STRUCTURED_EXTRACTION: int = 1 << 5  # bit 5
ENRICHMENT_OBSERVATIONS: int = 1 << 6  # bit 6 — reserved, not in ALL
ENRICHMENT_BLOB_TEXT: int = 1 << 7  # bit 7 — blob text extraction complete
LLM_INVALIDATION_BIT: int = 1 << 8  # bit 8 — LLM-driven fact invalidation

ENRICHMENT_ALL: int = (
    ENRICHMENT_ENTITIES
    | ENRICHMENT_EMBEDDING
    | ENRICHMENT_FACTS
    | ENRICHMENT_ENTITY_LINKS
    | ENRICHMENT_CLASSIFICATION
    | ENRICHMENT_STRUCTURED_EXTRACTION
)
"""Bitmask with all active enrichment bits set.
Use this to check if an episode is fully enriched.

Bit 6 (``ENRICHMENT_OBSERVATIONS``) is intentionally excluded — the
observations pass is non-blocking and deferred.
"""

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

# note: values must be real booleans — never coerce with ``bool()``.
# ``bool("false")`` is ``True`` and would silently enable a toggle the
# caller tried to disable.  Validate with ``isinstance(value, bool)``.
EnrichmentMap = dict[str, bool]
"""Per-episode enrichment toggles (every key defaults to ``True``)."""
