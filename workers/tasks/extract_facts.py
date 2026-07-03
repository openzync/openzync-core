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

import re
import uuid
from datetime import datetime, timezone

import structlog

from services.worker.prompt_renderer import build_enrichment_prompt, render_prompt
from core.exceptions import EpisodeNotFoundError
from workers.tasks.base import ENRICHMENT_FACTS, with_retry

logger = structlog.get_logger()

# ── Quality-heuristic constants ───────────────────────────────────────────────
_CONFIDENCE_THRESHOLD: float = 0.3


# ── Public ARQ task (decorated with retry) ────────────────────────────────────


@with_retry(max_retries=3, base_delay_s=2.0)
async def extract_facts(
    ctx: object,
    episode_id: str,
    org_id: str,
    project_id: str,
    content: str,
    session_id: str | None = None,
    trace_id: str = "",
    metadata: dict | None = None,
) -> None:
    """Extract zero-shot factual statements from a message and persist them.

    This function is designed as an ARQ task — the ``ctx`` parameter provides
    a shared DB engine from the worker process (``ctx["db_engine"]``).
    When ``ctx`` is absent (direct invocation), a short-lived engine is
    created as a fallback.

    Pipeline:
        0. Fetch known entities + recent history from session (if session_id).
        1. Render the ``extract_facts_v4.jinja2`` prompt with conversation,
           known entities, existing facts, and recent history (or
           ``extract_facts_v3.jinja2`` for first extraction when no facts exist yet).
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
        project_id: UUID of the project for project scoping.
        content: The message text to extract facts from.
        session_id: UUID of the session (passed from MemoryService).
            Used to fetch previously extracted entities and recent
            conversation turns for pronoun resolution.
        trace_id: Request trace ID for end-to-end correlation across ARQ tasks.

    Raises:
        Exception: Re-raises the last LLM or DB error after retry exhaustion
            (``on_exhaustion="raise"`` default behaviour).
    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    # Lazy imports to keep the module importable without the full async
    # stack at definition time — ARQ workers run in a separate process.
    from core.config import settings
    from core.db import get_async_session
    from core.llm import resolve_backend
    from core.org_config import get_org_config
    from repositories.episode_repository import EpisodeRepository
    from repositories.fact_repository import FactRepository
    from schemas.llm_outputs import FactExtractionOutput

    logger.info(
        "fact_extraction.started",
        episode_id=episode_id,
        org_id=org_id,
        project_id=project_id,
        session_id=session_id,
        content_length=len(content),
        trace_id=trace_id,
    )

    # Use the shared engine from worker context.
    engine = ctx.get("db_engine") if isinstance(ctx, dict) else None
    if engine is None:
        from core.db import init_db_engine

        engine = init_db_engine(str(settings.DATABASE_URL), pool_size=2, max_overflow=1)
        _own_engine = True
    else:
        _own_engine = False
    session_factory = ctx.get("db_session_factory") if isinstance(ctx, dict) else None
    if session_factory is None:
        session_factory = get_async_session(engine)

    # ── 0. Resolve user_id from episode record ──────────────────────────────
    # user_id is stored on the episode at creation time (from the API key's
    # created_by via the auth middleware).  The worker resolves it from the
    # episode rather than receiving it as an ARQ parameter.
    from sqlalchemy import select
    from models.episode import Episode

    async with session_factory() as resolve_db:
        result = await resolve_db.execute(
            select(Episode.user_id).where(Episode.id == episode_id)
        )
        user_id_row = result.scalar_one_or_none()
    if user_id_row is None:
        logger.warning(
            "fact_extraction.episode_not_found",
            episode_id=episode_id,
        )
        raise EpisodeNotFoundError(
            message=f"Episode {episode_id} not found for fact extraction.",
            detail={"episode_id": episode_id},
        )
    user_id: str = str(user_id_row)

    # ── 1. Render prompt (system instructions) with auto-injected context ──
    system_prompt, prompt_context = await render_prompt(
        "fact_extraction",
        org_id=org_id,
        episode_id=episode_id,
        session_id=session_id,
        user_id=user_id,
        db_session_factory=session_factory,
        return_context=True,
        metadata=metadata or {},
    )

    known_entities: list[dict] = prompt_context.get("known_entities", [])
    existing_facts: list[dict] = prompt_context.get("existing_facts", [])

    # ── 1b. Build full prompt with context sections ────────────────────────
    prompt = build_enrichment_prompt(system_prompt, prompt_context)

    # ── 1b. Fetch per-organization config ─────────────────────────────────
    llm_config_dict: dict | None = None
    try:
        async with session_factory() as db:
            org_cfg = await get_org_config(uuid.UUID(org_id), db, redis=None)
            llm_config_dict = org_cfg.to_llm_config_dict()
    except Exception:
        logger.warning(
            "fact_extraction.org_config_fetch_failed",
            org_id=org_id,
            exc_info=True,
        )

    # ── 2-3. Call LLM with structured-output validation ───────────────────────
    try:
        llm = await resolve_backend(org_config=llm_config_dict)
        response = await llm.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a fact extraction system."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_model=FactExtractionOutput,
            temperature=0.1,
        )
    except Exception as exc:
        logger.error(
            "fact_extraction.llm_failed",
            episode_id=episode_id,
            error=str(exc),
        )
        raise  # Let the @with_retry decorator handle transient failures

    parsed = response.validated_data  # FactExtractionOutput instance
    facts: list[dict] = [f.model_dump() for f in parsed.facts]

    # ── 4. Filter, resolve entities, persist, and set enrichment bit ─────────
    # Uses the shared engine from worker ctx (set earlier in this function).
    persisted = 0
    try:
        async with session_factory() as db:
            # Persist facts (if any) AND set enrichment bit in a single
            # transaction so that a persistence failure does NOT leave a
            # falsely-completed enrichment marker.
            #
            # note: The enrichment bit is set AFTER fact persistence
            # inside the same transaction.  If the fact inserts fail the
            # transaction rolls back and the bit is NOT set, allowing the
            # retry mechanism to re-attempt the work.
            if facts:
                valid_facts = _filter_facts(facts)
                if valid_facts:
                    # Resolve subject/object pronouns against known entities
                    resolved_facts = _resolve_fact_entities(valid_facts, known_entities)

                    # ── Deduplicate against existing facts ──────────────────
                    # Filters out facts that already exist in the session
                    # (same normalized triple) to prevent re-extraction from
                    # assistant echo messages or overlapping extractions.
                    resolved_facts = _deduplicate_facts(
                        resolved_facts,
                        existing_facts,
                    )

                    if resolved_facts:
                        repo = FactRepository(db)
                        # Also init entity repo for graph relationship upserts
                        from repositories.entity_repository import (
                            EntityRepository as _EntityRepo,
                        )

                        entity_repo = _EntityRepo(db=db)

                        # ══════════════════════════════════════════════════════
                        # Batch-create all unique facts in a single query
                        # instead of N individual round-trips.
                        # ══════════════════════════════════════════════════════
                        new_facts = await repo.batch_create_or_skip(
                            facts=resolved_facts,
                            user_id=uuid.UUID(user_id),
                            organization_id=uuid.UUID(org_id),
                            project_id=uuid.UUID(project_id),
                            source_episode_id=uuid.UUID(episode_id),
                        )

                        # Build a lookup from content string → input fact dict
                        # to match returned Fact ORM objects back to their
                        # original input for entity resolution and graph upserts.
                        content_to_fact: dict[str, dict] = {
                            f"{f['subject']} {f['predicate']} {f['object']}": f
                            for f in resolved_facts
                        }

                        persisted = len(new_facts)

                        duplicates_count = len(resolved_facts) - len(new_facts)
                        if duplicates_count:
                            logger.info(
                                "fact_extraction.duplicates_skipped",
                                episode_id=episode_id,
                                count=duplicates_count,
                            )

                        # ── Post-insert per-fact processing ──────────────────
                        # Entity resolution fallback + graph relationship
                        # materialization for newly created facts only.
                        for fact_obj in new_facts:
                            input_fact = content_to_fact.get(fact_obj.content)
                            if input_fact is None:
                                continue  # guard against logic errors

                            # ── Also persist to graph_relationships ──────────
                            # When both entity IDs are resolved, materialize
                            # the relationship in the graph for traversal queries.
                            subj_id = input_fact.get("subject_entity_id")
                            obj_id = input_fact.get("object_entity_id")

                            # ── Live entity lookup fallback ────────────────
                            # extract_entities always completes before this
                            # worker runs (it chains after via enqueue), so
                            # entities are guaranteed to be in the DB.
                            if subj_id is None:
                                subj_node = await entity_repo.get_entity_by_name(
                                    org_id=uuid.UUID(org_id),
                                    project_id=uuid.UUID(project_id),
                                    name=input_fact["subject"],
                                )
                                if subj_node is not None:
                                    subj_id = uuid.UUID(subj_node["id"])
                                    input_fact["subject_entity_id"] = subj_id
                                    logger.info(
                                        "fact_extraction.live_entity_resolved",
                                        episode_id=episode_id,
                                        entity_name=input_fact["subject"],
                                        role="subject",
                                    )

                            if obj_id is None:
                                obj_node = await entity_repo.get_entity_by_name(
                                    org_id=uuid.UUID(org_id),
                                    project_id=uuid.UUID(project_id),
                                    name=input_fact["object"],
                                )
                                if obj_node is not None:
                                    obj_id = uuid.UUID(obj_node["id"])
                                    input_fact["object_entity_id"] = obj_id
                                    logger.info(
                                        "fact_extraction.live_entity_resolved",
                                        episode_id=episode_id,
                                        entity_name=input_fact["object"],
                                        role="object",
                                    )

                            if subj_id is not None and obj_id is not None:
                                try:
                                    await entity_repo.upsert_relationship(
                                        subject=input_fact["subject"],
                                        predicate=input_fact["predicate"],
                                        obj=input_fact["object"],
                                        org_id=uuid.UUID(org_id),
                                        project_id=uuid.UUID(project_id),
                                    )
                                except Exception:
                                    # Non-fatal: fact is already persisted,
                                    # graph relationship is secondary
                                    logger.warning(
                                        "fact_extraction.graph_rel_failed",
                                        episode_id=episode_id,
                                        subject=input_fact["subject"],
                                        predicate=input_fact["predicate"],
                                        object=input_fact["object"],
                                        exc_info=True,
                                    )

            # Set enrichment bit after fact persistence, inside the same
            # transaction — rollback-safe.
            episode_repo = EpisodeRepository(db)
            await episode_repo.apply_enrichment_bits(
                uuid.UUID(episode_id), ENRICHMENT_FACTS
            )
            await db.commit()
    finally:
        if _own_engine:
            await engine.dispose()

    if persisted:
        logger.info(
            "fact_extraction.completed",
            episode_id=episode_id,
            project_id=project_id,
            facts=persisted,
        )
    else:
        logger.info(
            "fact_extraction.no_facts",
            episode_id=episode_id,
            project_id=project_id,
        )


# ── Private helpers ───────────────────────────────────────────────────────────


def _filter_facts(facts: list[dict]) -> list[dict]:
    """Apply confidence threshold and reject incomplete triples.

    Filters out facts below the confidence threshold and triples with empty
    subject, predicate, or object.  All predicate-level filtering is delegated
    to the prompt layer — the LLM should produce quality facts directly.

    Args:
        facts: Raw fact triples from the LLM.

    Returns:
        Filtered list of fact dicts meeting minimum quality criteria.
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

        valid.append(
            {
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "confidence": confidence,
                # Preserve LLM's entity/literal judgment if available (v4 prompt);
                # default to "literal" for v2/v3 prompts that don't output this field.
                "subject_type": fact.get("subject_type", "literal"),
                "object_type": fact.get("object_type", "literal"),
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
    the list of known entities from the session.  Entity matching runs
    regardless of the LLM's ``subject_type``/``object_type`` label to
    ensure backward compatibility with v2/v3 prompts that don't output
    these fields.

    When a match is found:
    - The subject/object text is replaced with the canonical entity name.
    - ``subject_type`` / ``object_type`` is set to ``"entity"``.
    - ``subject_entity_id`` / ``object_entity_id`` is set to the entity UUID.

    When no match is found the original values are preserved.  If the
    LLM (v4+) already set a type, it is kept; otherwise defaults to
    ``"literal"`` with ``None`` entity IDs.

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

        # Resolve subject — always attempt matching regardless of LLM label
        subj_result = _match_entity(fact["subject"], known_entities)
        if subj_result:
            new_fact["subject"] = subj_result["name"]
            new_fact["subject_type"] = "entity"
            new_fact["subject_entity_id"] = subj_result["id"]

        # Resolve object — always attempt matching regardless of LLM label
        obj_result = _match_entity(fact["object"], known_entities)
        if obj_result:
            new_fact["object"] = obj_result["name"]
            new_fact["object_type"] = "entity"
            new_fact["object_entity_id"] = obj_result["id"]

        resolved.append(new_fact)

    return resolved


_FIRST_PERSON_PRONOUNS: set[str] = {
    "i",
    "me",
    "my",
    "mine",
    "myself",
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
