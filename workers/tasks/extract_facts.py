"""Fact extraction worker — zero-shot fact extraction from conversation turns.

Runs as an ARQ background task after an episode has been committed to
PostgreSQL.  Uses an LLM to extract subject-predicate-object triples,
filters them by confidence and quality heuristics, resolves pronouns
against previously extracted entities, and persists the results in the
``facts`` table.

Key improvement over v1: receives ``session_id`` to fetch:
- Previously extracted entities for pronoun resolution.
- Recent conversation turns for coreference context.

Bitmask:
    Sets ``episodes.enrichment_status`` bit 2 (``ENRICHMENT_FACTS``)
    on success.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import text

# TechLead note: Import prompt_renderer at module level — it is a local
# Jinja2 utility with no heavy dependencies, so eager import is safe
# and avoids re-import overhead on every task invocation.
from services.worker.prompt_renderer import render_prompt
from workers.tasks.base import ENRICHMENT_FACTS, with_retry

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

# ── Meta-fact / uninformative fact filters ──────────────────────────────────
# These predicates describe the conversation itself rather than the user's
# domain knowledge.  Facts with these predicates add no value and should be
# filtered out.
_META_PREDICATES: frozenset[str] = frozenset({
    "refers_to", "stands_for", "means", "describes", "explains",
    "is_about", "talks_about", "mentions", "refers", "implies",
    "indicates", "signifies", "represents", "corresponds_to",
    "is_short_for", "is_abbreviation_for", "is_known_as",
    "also_known_as", "aka",
})
# Self-referential patterns: subject == object with certain predicates
# indicates a fact the LLM extracted about itself rather than domain knowledge.
_SELF_REFERENTIAL_PREDICATES: frozenset[str] = frozenset({
    "refers_to", "is", "are", "identifies_as",
})
# Uninformative predicates that convey no actionable information
_UNINFORMATIVE_PREDICATES: frozenset[str] = frozenset({
    "name", "called", "named", "has_name",
})

# ── Session context constants ─────────────────────────────────────────────────
_RECENT_EPISODE_WINDOW: int = 10
"""Number of previous conversation turns to include as context."""


# ── Public ARQ task (decorated with retry) ────────────────────────────────────


@with_retry(max_retries=3, base_delay_s=2.0)
async def extract_facts(
    ctx: object,
    episode_id: str,
    org_id: str,
    user_id: str,
    content: str,
    session_id: str | None = None,
) -> None:
    """Extract zero-shot factual statements from a message and persist them.

    This function is designed as an ARQ task — the ``ctx`` parameter is
    required by the ARQ contract but is not used directly here (we create
    a short-lived DB engine per invocation).

    Pipeline:
        0. Fetch known entities + recent history from session (if session_id).
        1. Render the ``extract_facts_v2.jinja2`` prompt with conversation,
           known entities, and recent history.
        2. Call the LLM backend (via ``resolve_backend()``, temperature 0.1).
        3. Parse the JSON response (handles markdown fence wrapping).
        4. Filter triples by confidence (>= 0.3) and quality heuristics.
        5. Resolve subject/object to entity IDs from known entities.
        6. Persist valid facts via ``FactRepository``.
        7. Update ``episodes.enrichment_status`` bit 2.

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        episode_id: UUID of the source episode (string, from ARQ).
        org_id: UUID of the owning organization.
        user_id: UUID of the user who authored the message.
        content: The message text to extract facts from.
        session_id: UUID of the session (passed from MemoryService).
            Used to fetch previously extracted entities and recent
            conversation turns for pronoun resolution.

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
    from models.episode import Episode
    from sqlalchemy import select

    logger.info(
        "fact_extraction.started",
        episode_id=episode_id,
        org_id=org_id,
        session_id=session_id,
        content_length=len(content),
    )

    # ── 0. Fetch session context (known entities + recent history + facts) ────
    known_entities: list[dict] = []
    recent_history: list[dict] = []
    existing_facts: list[dict] = []
    if session_id:
        try:
            ctx_engine = init_db_engine(
                str(settings.DATABASE_URL), pool_size=2, max_overflow=1
            )
            ctx_session_factory = get_async_session(ctx_engine)
            async with ctx_session_factory() as db:
                repo = FactRepository(db)
                known_entities = await repo.get_entities_for_session(
                    session_id=uuid.UUID(session_id),
                    organization_id=uuid.UUID(org_id),
                )

                # Fetch existing facts from this session for delta extraction
                existing_facts, _ = await repo.list_by_session(
                    organization_id=uuid.UUID(org_id),
                    session_id=uuid.UUID(session_id),
                    limit=200,
                )

                # Fetch recent conversation history (before current episode)
                result = await db.execute(
                    select(Episode)
                    .where(
                        Episode.session_id == uuid.UUID(session_id),
                        Episode.id != uuid.UUID(episode_id),
                        Episode.is_deleted == False,
                    )
                    .order_by(Episode.created_at.desc())
                    .limit(_RECENT_EPISODE_WINDOW)
                )
                recent_eps = list(result.scalars().all())
                # Reverse to get chronological order
                recent_history = [
                    {"role": ep.role, "content": ep.content}
                    for ep in reversed(recent_eps)
                ]

                logger.debug(
                    "fact_extraction.session_context_fetched",
                    episode_id=episode_id,
                    known_entities=len(known_entities),
                    existing_facts=len(existing_facts),
                    recent_episodes=len(recent_history),
                )
        except Exception as exc:
            # ⚠️ Non-fatal: continue without context if DB is unavailable
            logger.warning(
                "fact_extraction.session_context_failed",
                episode_id=episode_id,
                session_id=session_id,
                error=str(exc),
            )
        finally:
            await ctx_engine.dispose()

    # ── 1. Render prompt (v3 for delta with existing facts, v2 as fallback) ────
    prompt_template = "extract_facts_v3" if existing_facts else "extract_facts_v2"
    try:
        prompt = render_prompt(
            prompt_template,
            conversation=content,
            known_entities=known_entities,
            recent_history=recent_history,
            existing_facts=existing_facts,
        )
    except FileNotFoundError:
        logger.error(
            "fact_extraction.prompt_missing",
            episode_id=episode_id,
            template=f"{prompt_template}.jinja2",
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
                        "You are a fact extraction system. Output ONLY valid JSON."
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

    # ── 4. Filter, resolve entities, persist, and set enrichment bit ─────────
    persisted = 0
    engine = init_db_engine(str(settings.DATABASE_URL), pool_size=5, max_overflow=2)
    session_factory = get_async_session(engine)
    try:
        async with session_factory() as db:
            # Persist facts (if any) AND set enrichment bit in a single
            # transaction so that a persistence failure does NOT leave a
            # falsely-completed enrichment marker.
            #
            # TechLead note: The enrichment bit is set AFTER fact persistence
            # inside the same transaction.  If the fact inserts fail the
            # transaction rolls back and the bit is NOT set, allowing the
            # retry mechanism to re-attempt the work.
            if facts:
                valid_facts = _filter_facts(facts)
                if valid_facts:
                    # Resolve subject/object pronouns against known entities
                    resolved_facts = _resolve_fact_entities(
                        valid_facts, known_entities
                    )

                    # ── Deduplicate against existing facts ──────────────────
                    # Filters out facts that already exist in the session
                    # (same normalized triple) to prevent re-extraction from
                    # assistant echo messages or overlapping extractions.
                    resolved_facts = _deduplicate_facts(
                        resolved_facts, existing_facts,
                    )

                    if resolved_facts:
                        repo = FactRepository(db)
                        # Also init entity repo for graph relationship upserts
                        from repositories.entity_repository import EntityRepository as _EntityRepo
                        entity_repo = _EntityRepo(db=db)

                        for fact in resolved_facts:
                            await repo.create(
                                user_id=uuid.UUID(user_id),
                                organization_id=uuid.UUID(org_id),
                                content=f"{fact['subject']} {fact['predicate']} {fact['object']}",
                                subject=fact["subject"],
                                predicate=fact["predicate"],
                                obj=fact["object"],
                                subject_type=fact.get("subject_type", "literal"),
                                object_type=fact.get("object_type", "literal"),
                                confidence=fact["confidence"],
                                source_episode_id=uuid.UUID(episode_id),
                                valid_from=datetime.now(timezone.utc),
                                subject_entity_id=fact.get("subject_entity_id"),
                                object_entity_id=fact.get("object_entity_id"),
                            )

                            # ── Also persist to graph_relationships ──────────
                            # When both entity IDs are resolved, materialize
                            # the relationship in the graph for traversal queries.
                            subj_id = fact.get("subject_entity_id")
                            obj_id = fact.get("object_entity_id")
                            if subj_id is not None and obj_id is not None:
                                try:
                                    await entity_repo.upsert_relationship(
                                        subject=fact["subject"],
                                        predicate=fact["predicate"],
                                        obj=fact["object"],
                                        org_id=uuid.UUID(org_id),
                                    )
                                except Exception:
                                    # Non-fatal: fact is already persisted,
                                    # graph relationship is secondary
                                    logger.warning(
                                        "fact_extraction.graph_rel_failed",
                                        episode_id=episode_id,
                                        subject=fact["subject"],
                                        predicate=fact["predicate"],
                                        object=fact["object"],
                                    )
                    persisted = len(resolved_facts)

            # Set enrichment bit after fact persistence, inside the same
            # transaction — rollback-safe.
            await db.execute(
                text("UPDATE episodes SET enrichment_status = enrichment_status | :bit WHERE id = :id"),
                {"bit": ENRICHMENT_FACTS, "id": episode_id},
            )
            await db.commit()
    finally:
        await engine.dispose()

    if persisted:
        logger.info("fact_extraction.completed", episode_id=episode_id, facts=persisted)
    else:
        logger.info("fact_extraction.no_facts", episode_id=episode_id)


async def _set_enrichment_bit(episode_id: str, bit: int) -> None:
    """Set an enrichment_status bit for an episode.

    Always runs, even if no data was found — marks the task as complete
    so the pipeline knows it has been attempted.
    """
    from sqlalchemy import text

    from core.config import settings as app_settings
    from core.db import get_async_session, init_db_engine

    engine = init_db_engine(str(app_settings.DATABASE_URL), pool_size=2, max_overflow=1)
    session_factory = get_async_session(engine)
    try:
        async with session_factory() as db:
            await db.execute(
                text(
                    "UPDATE episodes SET enrichment_status = enrichment_status | :bit WHERE id = :id"
                ),
                {"bit": bit, "id": episode_id},
            )
            await db.commit()
    except Exception as exc:
        logger.warning(
            "enrichment_bit_failed", episode_id=episode_id, bit=bit, error=str(exc)
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

    # Strip deepseek-r1 thinking blocks: find first JSON object or array.
    # Only strip text that appears BEFORE the first [ or {, not the bracket itself.
    first_array = content.find('[')
    first_object = content.find('{')
    if first_array >= 0 and (first_object < 0 or first_array < first_object):
        json_start = first_array
    elif first_object >= 0:
        json_start = first_object
    else:
        json_start = -1
    if json_start > 0:  # only strip if there's text before the bracket
        content = content[json_start:].strip()
    elif json_start == -1:
        return []

    # Proactive handling: LLMs often return comma-separated JSON objects
    # without an array wrapper:
    #   {"subject":"Bob",...},
    #   {"subject":"Bob",...}
    # Detect this pattern and wrap in an array before the first parse attempt.
    stripped = content.strip()
    if stripped.startswith("{") and "},{" in stripped:
        try:
            data = json.loads(f"[{stripped}]")
        except json.JSONDecodeError:
            pass  # fall through to regular parse below
        else:
            logger.debug(
                "fact_extraction.array_wrapped", content_preview=stripped[:100]
            )
            return (
                data
                if isinstance(data, list)
                else data.get("facts", data.get("triples", []))
            )

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # Fallback: maybe the content is a single dict with a "facts" key
        if stripped.startswith("{"):
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                pass
        logger.warning(
            "fact_extraction.parse_failed",
            content_preview=content[:200],
        )
        return []

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("facts", data.get("triples", []))

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
    - **Meta-conversation facts**: predicates that describe the conversation
      itself rather than domain knowledge (refers_to, stands_for, means, …).
    - **Self-referential facts**: subject == object with identity predicates
      (e.g. ``"nikita refers_to nikita"``).
    - **Uninformative predicates**: trivial facts that add no value
      (e.g. ``"Rohan name Rohan"``).

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

        # ── Meta-fact filter — skip conversation-about-conversation ──────────
        pred_lower = predicate.lower()
        subj_lower = subject.lower()
        obj_lower = obj.lower()

        # Meta-predicates describe the conversation itself
        if pred_lower in _META_PREDICATES:
            logger.debug(
                "fact_filter.meta_predicate_skipped",
                subject=subject, predicate=predicate, object=obj,
            )
            continue

        # Self-referential: subject == object with identity predicate
        if subj_lower == obj_lower and pred_lower in _SELF_REFERENTIAL_PREDICATES:
            logger.debug(
                "fact_filter.self_referential_skipped",
                subject=subject, predicate=predicate, object=obj,
            )
            continue

        # Uninformative: trivial name declarations
        if pred_lower in _UNINFORMATIVE_PREDICATES:
            logger.debug(
                "fact_filter.uninformative_skipped",
                subject=subject, predicate=predicate, object=obj,
            )
            continue

        valid.append(
            {
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "confidence": confidence,
                "subject_type": "literal",
                "object_type": "literal",
                "subject_entity_id": None,
                "object_entity_id": None,
            }
        )

    return valid


def _resolve_fact_entities(
    facts: list[dict],
    known_entities: list[dict],
) -> list[dict]:
    """Resolve subject/object to canonical entity names and IDs.

    For each fact, attempts to match the subject and object strings against
    the list of known entities from the session.  When a match is found:
    - The subject/object text is replaced with the canonical entity name.
    - ``subject_type`` / ``object_type`` is set to ``"entity"``.
    - ``subject_entity_id`` / ``object_entity_id`` is set to the entity UUID.

    When no match is found the original values are preserved with type
    ``"literal"`` and ``None`` entity IDs.

    Args:
        facts: Filtered fact triples from ``_filter_facts``.
        known_entities: List of dicts with ``id``, ``name``, ``entity_type``
            keys, typically from ``FactRepository.get_entities_for_session``.

    Returns:
        A new list of fact dicts with entity resolution applied.
    """
    if not known_entities:
        return facts

    resolved: list[dict] = []
    for fact in facts:
        new_fact = dict(fact)

        # Resolve subject
        subj_result = _match_entity(fact["subject"], known_entities)
        if subj_result:
            new_fact["subject"] = subj_result["name"]
            new_fact["subject_type"] = "entity"
            new_fact["subject_entity_id"] = subj_result["id"]

        # Resolve object
        obj_result = _match_entity(fact["object"], known_entities)
        if obj_result:
            new_fact["object"] = obj_result["name"]
            new_fact["object_type"] = "entity"
            new_fact["object_entity_id"] = obj_result["id"]

        resolved.append(new_fact)

    return resolved


_FIRST_PERSON_PRONOUNS: set[str] = {
    "i", "me", "my", "mine", "myself",
}


def _match_entity(
    name: str,
    known_entities: list[dict],
) -> dict | None:
    """Match a subject/object string against known entities.

    Matching strategy (in order):
    1. First-person pronoun resolution — if the candidate is ``"I"``,
       ``"me"``, ``"my"``, ``"mine"``, or ``"myself"``, resolve to the
       first ``Person`` entity encountered in the known entities list.
    2. Exact, case-insensitive match.
    3. The known entity name is a substring of the candidate (e.g.
       "Rohan" matches "Rohan's expertise").
    4. The candidate is a substring of the known entity name (e.g.
       "OpenAI" matches "OpenAI") — only if the candidate is 3+
       characters to avoid false positives with short words.
    5. **Aggressive normalization fallback**: both strings are lowercased,
       stripped, punctuation removed, and whitespace collapsed before
       comparison.  Catches residual case/whitespace/punctuation mismatches
       like ``"Nikita"`` ↔ ``"nikita"`` or ``"theLinkAI"`` ↔ ``"the link ai"``.

    Only the first match is returned.  Entities are ordered
    alphabetically by name for deterministic matching.

    Args:
        name: The subject or object string from the extracted fact.
        known_entities: List of known entity dicts with ``name`` and
            ``entity_type`` keys.

    Returns:
        The matching entity dict, or ``None`` if no match was found.
    """
    name_lower = name.lower().strip()

    # Step 1: First-person pronoun → first Person entity
    if name_lower in _FIRST_PERSON_PRONOUNS:
        for ent in known_entities:
            if ent.get("entity_type", "").lower() == "person":
                return ent
        # Fall through to exact/substring matching below in case
        # no Person entity is known yet.

    for ent in known_entities:
        ent_name_lower = ent["name"].lower().strip()

        # Exact match (also catches resolved first-person above)
        if name_lower == ent_name_lower:
            return ent

        # Entity name is a substring of the candidate (e.g. "Rohan" in "Rohan's")
        if ent_name_lower and ent_name_lower in name_lower:
            return ent

        # Candidate is a substring of the entity name (e.g. "I" → "Iron Man" → no)
        # Only match if the candidate is 3+ characters to avoid false positives
        # with short words like "I", "AI", "IT"
        if len(name_lower) >= 3 and name_lower in ent_name_lower:
            return ent

    # Step 5: Aggressive normalization — strip punctuation, collapse whitespace.
    # Catches cases where the candidate and entity differ only in casing,
    # punctuation, or spacing (e.g. "Nikita" vs "nikita", "FIEM College" vs
    # "fiem college").
    import re as _re

    def normalize(s: str) -> str:
        return _re.sub(r"[^a-z0-9\s]", "", s.lower()).strip()

    name_normalized = normalize(name_lower)
    if len(name_normalized) >= 2:  # skip very short after normalization
        for ent in known_entities:
            ent_normalized = normalize(ent["name"])
            if name_normalized == ent_normalized:
                return ent

    return None


# ── Predicate synonym map for forgiving dedup ────────────────────────────────
# Maps predicates that are semantically equivalent — used by ``_deduplicate_facts``
# to catch duplicates that differ only in predicate naming.
_PREDICATE_SYNONYMS: dict[str, set[str]] = {
    "works_at": {"employed_at", "works_for", "employed_by", "joins"},
    "friend_of": {"friends_with", "shares_friend_with", "has_friend"},
    "colleague_of": {"coworker_of", "works_with", "teammate_of"},
    "studied_at": {"attended", "went_to", "graduated_from"},
    "likes": {"loves", "enjoys", "prefers"},
    "has_number_of_friends": {"has_friend_count", "friend_count", "num_friends"},
    "tech_lead_of": {"leads", "tech_lead_for", "leads_tech_for"},
    "graduated_from": {"completed", "finished", "graduated"},
}


def _deduplicate_facts(
    new_facts: list[dict],
    existing_facts: list[dict],
) -> list[dict]:
    """Deduplicate new facts against existing facts from the session.

    Two facts are considered duplicates if they have the same (subject, object)
    pair and either:
    - The same predicate (exact, case-insensitive), OR
    - The predicates are synonyms (per ``_PREDICATE_SYNONYMS``).

    This handles both exact duplicates (same triple, different episode) and
    near-duplicates (different predicate wording for the same meaning).

    Args:
        new_facts: Facts from the current extraction (after filtering + resolution).
        existing_facts: Facts already persisted for this session.

    Returns:
        Filtered list with duplicates removed.
    """
    if not existing_facts:
        return new_facts

    # Build a set of normalized (subject, object) pairs from existing facts,
    # along with the predicates used for each pair.
    existing_pairs: dict[tuple[str, str], set[str]] = {}
    for ef in existing_facts:
        key = (ef["subject"].lower().strip(), ef["object"].lower().strip())
        pred = ef["predicate"].lower().strip()
        if key not in existing_pairs:
            existing_pairs[key] = set()
        existing_pairs[key].add(pred)
        # Add synonym predicates so we can match against them
        if pred in _PREDICATE_SYNONYMS:
            existing_pairs[key].update(_PREDICATE_SYNONYMS[pred])
        # Also check if any other predicate maps TO this one
        for canonical, synonyms in _PREDICATE_SYNONYMS.items():
            if pred in synonyms:
                existing_pairs[key].add(canonical)

    deduped: list[dict] = []
    for nf in new_facts:
        key = (nf["subject"].lower().strip(), nf["object"].lower().strip())
        pred = nf["predicate"].lower().strip()

        # Expand to synonym set for matching
        candidate_preds: set[str] = {pred}
        if pred in _PREDICATE_SYNONYMS:
            candidate_preds.update(_PREDICATE_SYNONYMS[pred])
        for canonical, synonyms in _PREDICATE_SYNONYMS.items():
            if pred in synonyms:
                candidate_preds.add(canonical)

        if key in existing_pairs:
            # Check if any candidate predicate overlaps with existing
            if candidate_preds & existing_pairs[key]:
                logger.debug(
                    "fact_dedup.duplicate_skipped",
                    subject=nf["subject"],
                    predicate=nf["predicate"],
                    object=nf["object"],
                )
                continue

        deduped.append(nf)

    return deduped
