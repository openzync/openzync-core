"""Context block formatting — transforms retrieved results into LLM-ready text.

Provides two formatting modes:

- **Text** (``format_text``): Human-readable plain text with section headers,
  source-prefixed lines, and natural-language presentation.  Ideal for
  instruct-style LLM prompts.

- **JSON** (``format_json``): Structured JSON object with typed arrays for
  each source category.  Ideal for function-calling or structured output
  patterns.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# ── Constants ──────────────────────────────────────────────────────────────────

SEPARATOR: str = "─" * 72
"""Visual section separator for text formatting."""

MAX_CONTENT_CHARS: int = 2000
"""Truncation limit for episode content in text format."""


# ── Public API ──────────────────────────────────────────────────────────────────


def format_text(
    episodes: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    communities: list[dict[str, Any]],
) -> str:
    """Format retrieval results as a plain-text context block.

    Sections are ordered by likely relevance: recent episodes first,
    then extracted facts, then entity graph, then community summaries.

    Each section is visually separated with a horizontal rule and
    prefixed with a source label for provenance tracing.

    Args:
        episodes: RRF-merged episode results (list of dicts with
            ``content``, ``role``, ``created_at``).
        facts: RRF-merged fact results (list of dicts with ``content``,
            ``subject``, ``predicate``, ``object``, ``confidence``).
        entities: Graph entity results (list of dicts with ``name``,
            ``type``, ``summary``).
        communities: Community summary results (list of dicts —
            currently unused, reserved for future community detection).

    Returns:
        A plain-text string suitable for LLM context injection.
    """
    parts: list[str] = []

    # ── Recent Episodes ─────────────────────────────────────────────────
    if episodes:
        parts.append(f"Recent Episodes ({len(episodes)}):")
        parts.append(SEPARATOR)
        for i, ep in enumerate(episodes, start=1):
            role = ep.get("role", "unknown")
            content = ep.get("content", "")
            reranker = ep.get("reranker_score")
            rrf = ep.get("rrf_score")
            score_val = ep.get("score")
            if reranker is not None:
                score = reranker
            elif rrf is not None:
                score = rrf
            else:
                score = score_val
            score_str = f" [score={score:.4f}]" if score is not None else ""

            # Truncate very long content
            if len(content) > MAX_CONTENT_CHARS:
                content = content[:MAX_CONTENT_CHARS] + "..."

            # Indent multi-line content for readability
            content_lines = content.split("\n")
            indented_content = "\n    ".join(content_lines)

            parts.append(f"  {i}. [{role}]{score_str}")
            parts.append(f"    {indented_content}")
            parts.append("")  # blank line between items
        parts.append("")

    # ── Facts ────────────────────────────────────────────────────────────
    if facts:
        parts.append(f"Facts ({len(facts)}):")
        parts.append(SEPARATOR)
        for i, fact in enumerate(facts, start=1):
            content = fact.get("content", "")
            confidence = fact.get("confidence")
            reranker = fact.get("reranker_score")
            rrf = fact.get("rrf_score")
            score_val = fact.get("score")
            if reranker is not None:
                score = reranker
            elif rrf is not None:
                score = rrf
            else:
                score = score_val
            confidence_str = (
                f" (confidence={confidence:.2f})"
                if confidence is not None
                else ""
            )
            score_str = f" [score={score:.4f}]" if score is not None else ""

            # ⚠️ BREAKING: context text shape gains validity dates (Zep parity).
            validity_str = _validity_suffix(fact)

            parts.append(f"  {i}. {content}{validity_str}{confidence_str}{score_str}")
        parts.append("")

    # ── Entities ─────────────────────────────────────────────────────────
    if entities:
        parts.append(f"Entities ({len(entities)}):")
        parts.append(SEPARATOR)
        for i, entity in enumerate(entities, start=1):
            name = entity.get("name", "unknown")
            entity_type = entity.get("type", "")
            summary = entity.get("summary", "")
            distance = entity.get("distance")

            type_str = f" ({entity_type})" if entity_type else ""
            dist_str = f" [distance={distance}]" if distance is not None else ""
            summary_str = f" — {summary}" if summary else ""

            parts.append(f"  {i}. {name}{type_str}{dist_str}{summary_str}")
        parts.append("")

    # ── Communities ──────────────────────────────────────────────────────
    if communities:
        parts.append(f"Community Summaries ({len(communities)}):")
        parts.append(SEPARATOR)
        for i, community in enumerate(communities, start=1):
            name = community.get("name", f"Community {i}")
            summary = community.get("summary", "")

            parts.append(f"  {i}. {name}: {summary}")
        parts.append("")

    # Remove trailing newline
    text = "\n".join(parts).strip()

    return text if text else "No context found."


def format_json(
    episodes: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    communities: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Format retrieval results as a structured JSON object.

    Each source category becomes a typed array.  Items are cleaned to
    remove internal-only fields (like ``rrf_score`` and ``score`` from
    episodes/facts, and ``distance`` from entities) before returning so
    that the JSON is clean for LLM consumption.

    Args:
        episodes: RRF-merged episode results.
        facts: RRF-merged fact results.
        entities: Graph entity results.
        communities: Community summary results.

    Returns:
        A dict with ``episodes``, ``facts``, ``entities``, and
        ``communities`` keys, each containing a list of cleaned item
        dicts.
    """
    return {
        "episodes": _clean_episodes(episodes),
        "facts": _clean_facts(facts),
        "entities": _clean_entities(entities),
        "communities": _clean_communities(communities),
    }


# ── Internal Helpers ───────────────────────────────────────────────────────────


def _parse_validity_date(value: Any) -> datetime | None:
    """Coerce a validity value to a datetime, or ``None`` when unparseable.

    Accepts tz-aware datetimes (as returned by asyncpg) and ISO-8601
    strings.  Parse failures are swallowed so a malformed value never
    breaks context formatting.

    Args:
        value: Raw ``valid_from``/``valid_to`` value from a fact dict.

    Returns:
        The parsed datetime, or ``None`` if the value is not a datetime
        and not an ISO-8601 string.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _validity_suffix(fact: dict[str, Any]) -> str:
    """Build a Zep-style validity date suffix for a fact, or ``""``.

    ``valid_from``/``valid_to`` are rendered as ``YYYY-MM-DD`` dates;
    ``invalid_at`` is not rendered (retraction is handled upstream by the
    retrieval predicate, not by the context text).

    Args:
        fact: RRF-merged fact result dict.

    Returns:
        A suffix like ``" (valid: 2026-01-01 to 2026-06-01)"``, or
        ``""`` when neither component is present/parseable.
    """
    valid_from = _parse_validity_date(fact.get("valid_from"))
    valid_to = _parse_validity_date(fact.get("valid_to"))
    if valid_from is not None and valid_to is not None:
        return f" (valid: {valid_from:%Y-%m-%d} to {valid_to:%Y-%m-%d})"
    if valid_from is not None:
        return f" (valid: from {valid_from:%Y-%m-%d})"
    if valid_to is not None:
        return f" (valid: until {valid_to:%Y-%m-%d})"
    return ""


def _clean_episodes(
    episodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove internal ranking fields from episode dicts.

    Keeps: ``id``, ``role``, ``content``, ``created_at``.
    Removes: ``score``, ``rrf_score``, ``reranker_score``.

    Args:
        episodes: Raw episode results from the RRF merge.

    Returns:
        Cleaned episode dicts safe for JSON serialisation.
    """
    allowed_keys = {"id", "role", "content", "created_at", "blobs"}
    return [
        {k: v for k, v in ep.items() if k in allowed_keys}
        for ep in episodes
    ]


def _clean_facts(
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove internal ranking fields from fact dicts.

    Keeps: ``id``, ``content``, ``subject``, ``predicate``, ``object``,
    ``confidence``, ``created_at``, ``valid_from``, ``valid_to``,
    ``invalid_at``.
    Removes: ``score``, ``rrf_score``, ``reranker_score``.

    Args:
        facts: Raw fact results from the RRF merge.

    Returns:
        Cleaned fact dicts safe for JSON serialisation.
    """
    allowed_keys = {
        "id", "content", "subject", "predicate",
        "object", "confidence", "created_at",
        "valid_from", "valid_to", "invalid_at",
    }
    return [
        {k: v for k, v in fact.items() if k in allowed_keys}
        for fact in facts
    ]


def _clean_entities(
    entities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove internal fields from entity dicts.

    Keeps: ``id``, ``name``, ``type``, ``summary``.
    Removes: ``distance``, ``score``.

    Args:
        entities: Raw entity results from graph BFS.

    Returns:
        Cleaned entity dicts safe for JSON serialisation.
    """
    allowed_keys = {"id", "name", "type", "summary"}
    return [
        {k: v for k, v in ent.items() if k in allowed_keys}
        for ent in entities
    ]


def _clean_communities(
    communities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove internal fields from community dicts.

    Keeps: ``id``, ``name``, ``summary``.

    Args:
        communities: Raw community summary results.

    Returns:
        Cleaned community dicts safe for JSON serialisation.
    """
    allowed_keys = {"id", "name", "summary"}
    return [
        {k: v for k, v in com.items() if k in allowed_keys}
        for com in communities
    ]
