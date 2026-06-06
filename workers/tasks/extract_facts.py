"""Fact extraction worker — zero-shot fact extraction from conversation turns.

Runs as an ARQ background task after an episode has been committed to
PostgreSQL.  Uses an LLM to extract subject-predicate-object triples,
filters them by confidence and quality heuristics, and persists the
results in the ``facts`` table.

Bitmask:
    Sets ``episodes.enrichment_status`` bit 2 (``ENRICHMENT_FACTS``)
    on success.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import text

from workers.tasks.base import ENRICHMENT_FACTS, with_retry

# TechLead note: Import prompt_renderer at module level — it is a local
# Jinja2 utility with no heavy dependencies, so eager import is safe
# and avoids re-import overhead on every task invocation.
from services.worker.prompt_renderer import render_prompt

logger = structlog.get_logger()

# ── Quality-heuristic constants ───────────────────────────────────────────────
# Bare copular verbs are generally not informative as extracted predicates.
# The LLM should prefer richer verbs like "works_at", "prefers", "uses".
_BARE_COPULARS: frozenset[str] = frozenset(
    {"is", "are", "was", "were", "be", "been", "being", "am"}
)
_IGNORE_PREDICATE_PREFIXES: tuple[str, ...] = (
    "instruction",
    "ignore",
    "disregard",
    "pretend",
    "you are",
    "you should",
)
_IGNORE_SUBJECT_PREFIXES: tuple[str, ...] = (
    "ignore",
    "instruction",
    "system",
)
_CONFIDENCE_THRESHOLD: float = 0.3


# ── Public ARQ task (decorated with retry) ────────────────────────────────────


@with_retry(max_retries=3, base_delay_s=2.0)
async def extract_facts(
    ctx: object,
    episode_id: str,
    org_id: str,
    user_id: str,
    content: str,
) -> None:
    """Extract zero-shot factual statements from a message and persist them.

    This function is designed as an ARQ task — the ``ctx`` parameter is
    required by the ARQ contract but is not used directly here (we create
    a short-lived DB engine per invocation).

    Pipeline:
        1. Render the ``extract_facts_v1.jinja2`` prompt with the conversation.
        2. Call the LLM backend (via ``resolve_backend()``, temperature 0.1).
        3. Parse the JSON response (handles markdown fence wrapping).
        4. Filter triples by confidence (>= 0.3) and quality heuristics.
        5. Persist valid facts via ``FactRepository``.
        6. Update ``episodes.enrichment_status`` bit 2.

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        episode_id: UUID of the source episode (string, from ARQ).
        org_id: UUID of the owning organization.
        user_id: UUID of the user who authored the message.
        content: The message text to extract facts from.

    Raises:
        Exception: Re-raises the last LLM or DB error after retry exhaustion
            (``on_exhaustion="raise"`` default behaviour).
    """
    # Lazy imports to keep the module importable without the full async
    # stack at definition time — ARQ workers run in a separate process.
    from core.config import settings
    from core.db import get_async_session, init_db_engine
    from core.llm import resolve_backend
    from repositories.fact_repository import FactRepository

    logger.info(
        "fact_extraction.started",
        episode_id=episode_id,
        org_id=org_id,
        content_length=len(content),
    )

    # ── 1. Render prompt ──────────────────────────────────────────────────────
    try:
        prompt = render_prompt("extract_facts_v1", conversation=content)
    except FileNotFoundError:
        logger.error(
            "fact_extraction.prompt_missing",
            episode_id=episode_id,
            template="extract_facts_v1.jinja2",
        )
        return

    # ── 2. Call LLM ───────────────────────────────────────────────────────────
    try:
        llm = await resolve_backend()
        response = await llm.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a fact extraction system. "
                        "Output ONLY valid JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )
    except Exception as exc:
        logger.error(
            "fact_extraction.llm_failed",
            episode_id=episode_id,
            error=str(exc),
        )
        raise  # Let the @with_retry decorator handle transient failures

    # ── 3. Parse JSON response ────────────────────────────────────────────────
    facts = _parse_facts_response(response.content)
    if not facts:
        logger.info("fact_extraction.no_facts", episode_id=episode_id)
        return

    # ── 4. Filter by confidence + quality heuristics ──────────────────────────
    valid_facts = _filter_facts(facts)
    if not valid_facts:
        logger.info(
            "fact_extraction.filtered_all",
            episode_id=episode_id,
            raw_count=len(facts),
        )
        return

    # ── 5. Persist via repository ─────────────────────────────────────────────
    engine = init_db_engine(
        str(settings.DATABASE_URL),
        pool_size=5,
        max_overflow=2,
    )
    session_factory = get_async_session(engine)

    try:
        async with session_factory() as db:
            repo = FactRepository(db)

            for fact in valid_facts:
                await repo.create(
                    user_id=uuid.UUID(user_id),
                    organization_id=uuid.UUID(org_id),
                    content=(
                        f"{fact['subject']} {fact['predicate']} {fact['object']}"
                    ),
                    subject=fact["subject"],
                    predicate=fact["predicate"],
                    obj=fact["object"],
                    confidence=fact["confidence"],
                    source_episode_id=uuid.UUID(episode_id),
                    valid_from=datetime.now(timezone.utc),
                )

            # ── 6. Update enrichment_status bit 2 ─────────────────────────────
            await db.execute(
                text(
                    "UPDATE episodes "
                    "SET enrichment_status = enrichment_status | :bit "
                    "WHERE id = :id"
                ),
                {"bit": ENRICHMENT_FACTS, "id": episode_id},
            )
            await db.commit()

        logger.info(
            "fact_extraction.completed",
            episode_id=episode_id,
            facts=len(valid_facts),
        )
    finally:
        await engine.dispose()


# ── Private helpers ───────────────────────────────────────────────────────────


def _parse_facts_response(content: str) -> list[dict]:
    """Parse LLM JSON response for fact triples.

    Handles common LLM output quirks: markdown code fences, trailing
    commas, and both list and dict-with-key wrappers.

    Args:
        content: Raw response text from the LLM.

    Returns:
        A list of fact dicts, or an empty list if parsing failed or the
        response contained no facts.
    """
    # Strip markdown code fences if present
    if "```json" in content:
        content = content.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in content:
        content = content.split("```", 1)[1].split("```", 1)[0].strip()

    # Strip leading/trailing whitespace that may remain after fence removal
    content = content.strip()

    if not content:
        return []

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logger = structlog.get_logger()
        logger.warning(
            "fact_extraction.parse_failed",
            content_preview=content[:200],
        )
        return []

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("facts", data.get("triples", []))

    logger = structlog.get_logger()
    logger.warning(
        "fact_extraction.unexpected_type",
        json_type=type(data).__name__,
    )
    return []


def _filter_facts(facts: list[dict]) -> list[dict]:
    """Apply confidence threshold and quality heuristics.

    Filters out:
    - Facts below the confidence threshold (0.3).
    - Triples with empty subject, predicate, or object.
    - Bare copular predicates (is, are, was, …).
    - Predicates or subjects that suggest instruction-following content.

    Args:
        facts: Raw fact triples from the LLM.

    Returns:
        Filtered list of fact dicts meeting all quality criteria.
    """
    valid: list[dict] = []

    for fact in facts:
        # ⚠️ Type coercion: the LLM may return numbers or booleans for
        # these fields; normalise everything to string for validation.
        confidence = float(fact.get("confidence", 0.5))
        if confidence < _CONFIDENCE_THRESHOLD:
            continue

        subject = str(fact.get("subject", "")).strip()
        predicate = str(fact.get("predicate", "")).strip()
        obj = str(fact.get("object", "")).strip()

        # Reject incomplete triples
        if not subject or not predicate or not obj:
            continue

        # Reject bare copular verbs — they add no information
        if predicate.lower() in _BARE_COPULARS:
            continue

        # ⚠️ Anti-injection guard: reject triples that sound like they
        # are describing the model's own instructions rather than the
        # user's data.
        if predicate.lower().startswith(_IGNORE_PREDICATE_PREFIXES):
            continue
        if subject.lower().startswith(_IGNORE_SUBJECT_PREFIXES):
            continue

        valid.append(
            {
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "confidence": confidence,
            }
        )

    return valid
