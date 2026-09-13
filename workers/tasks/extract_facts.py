"""Fact extraction helpers for the combined enrichment worker.

Exports ``process_facts_output`` (plus quality/filter/entity-resolution
helpers), which the combined ``enrich_episode`` worker calls to persist
LLM-extracted fact triples.  The standalone ``extract_facts`` ARQ task was
retired in favour of ``enrich_episode``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog

from core.exceptions import GraphBackendUnavailableError
from workers.tasks.base import ENRICHMENT_FACTS

if TYPE_CHECKING:
    from arq.connections import ArqRedis
    from sqlalchemy.ext.asyncio import AsyncSession

    from models.fact import Fact
    from packages.graph_backend.interface import GraphBackend
    from repositories.entity_repository import EntityRepository
    from repositories.episode_repository import EpisodeRepository
    from repositories.fact_repository import FactRepository
    from schemas.llm_outputs import FactExtractionOutput

logger = structlog.get_logger()

# ── Quality-heuristic constants ───────────────────────────────────────────────
_CONFIDENCE_THRESHOLD: float = 0.3


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
            logger.warning(
                "fact_extraction.incomplete_triple_skipped",
                extra={"subject": subject, "predicate": predicate, "object": obj},
            )
            continue

        # Reject born-dead validity windows (valid_from >= valid_to).  The
        # repository raises ValidationError on these, which would wedge the
        # worker: with_retry re-emits the same LLM-produced window forever
        # and the episode never enriches.  A window that has already ended
        # can never be active, so drop the fact like any other low-quality
        # output.  The repository guard stays — it protects the API path.
        valid_from = fact.get("valid_from")
        valid_to = fact.get("valid_to")
        if valid_from is not None and valid_to is not None and valid_from >= valid_to:
            logger.warning(
                "extract_facts.dropped_born_dead",
                extra={
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    "valid_from": valid_from.isoformat(),
                    "valid_to": valid_to.isoformat(),
                },
            )
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
                # Temporal validity window — only when the LLM stated an
                # explicit window/event date (see FactOutput.valid_from/valid_to).
                "valid_from": fact.get("valid_from"),
                "valid_to": fact.get("valid_to"),
                # 1-based output-slot provenance (set by process_facts_output)
                # so created rows can be mapped back to "N{index}" references
                # for LLM-driven invalidations.
                "_slot": fact.get("_slot"),
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
            keys, typically from ``GraphBackend.get_entities_for_session``.

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
       like ``"Nikita"`` ↔ ``"nikita"`` or ``"ExampleOrg"`` ↔ ``"the link ai"``.

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

        if key in existing_pairs and candidate_preds & existing_pairs[key]:
                logger.debug(
                    "fact_dedup.duplicate_skipped",
                    subject=nf["subject"],
                    predicate=nf["predicate"],
                    object=nf["object"],
                )
                continue

        deduped.append(nf)

    return deduped


# ── Public helper (exported for Wave 1d combined-worker refactor) ─────────────


async def process_facts_output(
    db: AsyncSession,
    graph_backend: GraphBackend | None,
    entity_repo: EntityRepository | None,
    fact_repo: FactRepository,
    episode_repo: EpisodeRepository | None,
    org_id: str,
    episode_id: str,
    project_id: str,
    session_id: str,
    user_id: str,
    trace_id: str,
    parsed: FactExtractionOutput,
    known_entities: list[dict],
    existing_facts: list[dict],
    arq_redis: ArqRedis | None = None,
    return_slot_map: bool = False,
) -> list[str] | tuple[list[Fact], dict[int, Fact | None]]:
    """Process and persist fact extraction output from LLM.

    Filters by confidence, resolves entity references, deduplicates
    against existing facts, persists new facts, applies enrichment bits,
    and optionally chains embed_fact jobs.

    Does NOT manage transactions — caller is responsible for commit/rollback
    of *db*.

    Args:
        db: Active database session (caller manages commit/rollback).
        graph_backend: Resolved graph backend for relationship upsert.
            Can be None; graph operations are skipped in that case.
        entity_repo: Entity repository for live entity lookup fallback
            and graph relationship upsert.  Can be None; graph operations
            are skipped in that case.
        fact_repo: Fact repository for batch_create_or_skip.
        episode_repo: Optional episode repository.  If provided, sets
            ``ENRICHMENT_FACTS`` bit on the episode inside the transaction.
        org_id: Organization UUID string.
        episode_id: Episode UUID string.
        project_id: Project UUID string.
        session_id: Session UUID string.
        user_id: User UUID string (owner of the extracted facts).
        trace_id: Trace/job ID string for log correlation.
        parsed: The validated ``FactExtractionOutput`` from the LLM.
        known_entities: List of known entity dicts for entity resolution.
        existing_facts: List of existing fact dicts for deduplication.
        arq_redis: Optional ARQ Redis client.  If provided, chains an
            ``embed_fact`` job per persisted fact (non-blocking — failures
            are logged as warnings and do not propagate).
        return_slot_map: When ``True``, returns
            ``(created_rows, slot_map)`` where ``slot_map`` maps each
            1-based output slot of ``parsed.facts`` to the created
            ``Fact`` row (or ``None`` when that slot was filtered,
            deduplicated, or skipped).  Used by ``enrich_episode`` to
            resolve ``"N{index}"`` successor references for LLM-driven
            invalidations.

    Returns:
        List of persisted fact UUID strings (empty if nothing was
        persisted).  When ``return_slot_map=True``, a tuple of the created
        ``Fact`` rows and the slot map instead.

    Raises:
        Various DB errors on persistence failure.
    """
    facts: list[dict] = [
        dict(f.model_dump(), _slot=i) for i, f in enumerate(parsed.facts, start=1)
    ]
    if not facts:
        return ([], {}) if return_slot_map else []

    valid_facts = _filter_facts(facts)
    if not valid_facts:
        return ([], {}) if return_slot_map else []

    resolved_facts = _resolve_fact_entities(valid_facts, known_entities)
    resolved_facts = _deduplicate_facts(resolved_facts, existing_facts)
    if not resolved_facts:
        return ([], {}) if return_slot_map else []

    # ── Batch-persist all new facts via supersession ───────────────────────
    # Conflicting active facts (same SPO identity) are superseded in the
    # same transaction; identical-content facts are skipped so ARQ retries
    # are idempotent.  Enrichment bit handling below is unchanged —
    # supersession is a side effect, not episode state.
    from services.cache_service import CacheService
    from services.fact_invalidation_service import (
        PURGE_ONLY_CACHE_TTL,
        FactInvalidationService,
    )
    from services.graph_edge_sync_service import GraphEdgeSyncService

    invalidation = FactInvalidationService(
        db=db,
        fact_repo=fact_repo,
        cache_service=(
            CacheService(arq_redis, default_ttl=PURGE_ONLY_CACHE_TTL)
            if arq_redis is not None
            else None
        ),
        graph_sync=(
            GraphEdgeSyncService(backends=[graph_backend])
            if graph_backend is not None
            else None
        ),
    )
    result = await invalidation.ingest_with_supersession(
        org_id=uuid.UUID(org_id),
        project_id=uuid.UUID(project_id),
        user_id=uuid.UUID(user_id),
        facts=resolved_facts,
        source_episode_id=uuid.UUID(episode_id),
        insert_mode="batch_create_or_skip",
    )
    new_facts = result.created

    # Build a lookup from content string → input fact dict to match returned
    # Fact ORM objects back to their original input for entity resolution,
    # graph upserts, and the invalidation slot map.  Keyed on the SAME
    # content fallback the supersession service computes in
    # ``_prepare_entry`` (``fact.get("content") or "s p o"``) — an SPO-only
    # key would silently miss facts with explicit LLM content, losing the
    # slot linkage and degrading a successor invalidation to a retraction
    # (D1 case-1 edge expiry despite a successor existing).
    content_to_fact: dict[str, dict] = {
        f.get("content") or f"{f['subject']} {f['predicate']} {f['object']}": f
        for f in resolved_facts
    }

    persisted_ids: list[str] = []

    duplicates_count = len(resolved_facts) - len(new_facts)
    if duplicates_count:
        logger.info(
            "fact_extraction.duplicates_skipped",
            episode_id=episode_id,
            count=duplicates_count,
            superseded_count=result.superseded_count,
        )

    # ── Post-insert per-fact processing ─────────────────────────────────────
    # Entity resolution fallback + graph relationship materialization for
    # newly created facts only.
    for fact_obj in new_facts:
        persisted_ids.append(str(fact_obj.id))

        input_fact = content_to_fact.get(fact_obj.content)
        if input_fact is None:
            continue  # guard against logic errors

        subj_id: uuid.UUID | None = input_fact.get("subject_entity_id")
        obj_id: uuid.UUID | None = input_fact.get("object_entity_id")

        # ── Live entity lookup fallback ──────────────────────────────────
        # extract_entities always completes before this worker runs (it
        # chains after via enqueue), so entities are guaranteed to be in
        # the DB.  If the graph backend is unavailable, log the error and
        # continue — fact persistence to PostgreSQL is the primary concern.
        if subj_id is None and entity_repo is not None:
            try:
                subj_node = await entity_repo.get_entity_by_name(
                    org_id=uuid.UUID(org_id),
                    project_id=uuid.UUID(project_id),
                    name=input_fact["subject"],
                )
            except GraphBackendUnavailableError:
                subj_node = None
                logger.error(
                    "fact_extraction.graph_backend_unavailable",
                    episode_id=episode_id,
                    operation="get_entity_by_name",
                    role="subject",
                    entity_name=input_fact["subject"],
                    exc_info=True,
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

        if obj_id is None and entity_repo is not None:
            try:
                obj_node = await entity_repo.get_entity_by_name(
                    org_id=uuid.UUID(org_id),
                    project_id=uuid.UUID(project_id),
                    name=input_fact["object"],
                )
            except GraphBackendUnavailableError:
                obj_node = None
                logger.error(
                    "fact_extraction.graph_backend_unavailable",
                    episode_id=episode_id,
                    operation="get_entity_by_name",
                    role="object",
                    entity_name=input_fact["object"],
                    exc_info=True,
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

        # ── Graph relationship upsert ────────────────────────────────────
        # When both entity IDs are resolved, materialize the relationship
        # in the graph for traversal queries.
        if subj_id is not None and obj_id is not None and entity_repo is not None:
            try:
                await entity_repo.upsert_relationship(
                    subject=input_fact["subject"],
                    predicate=input_fact["predicate"],
                    obj=input_fact["object"],
                    org_id=uuid.UUID(org_id),
                    project_id=uuid.UUID(project_id),
                )
            except GraphBackendUnavailableError:
                # Non-fatal: fact is already persisted in PostgreSQL,
                # graph relationship is secondary.
                logger.error(
                    "fact_extraction.graph_backend_unavailable",
                    episode_id=episode_id,
                    operation="upsert_relationship",
                    subject=input_fact["subject"],
                    predicate=input_fact["predicate"],
                    object=input_fact["object"],
                    exc_info=True,
                )

    # ── Enrichment bit ──────────────────────────────────────────────────────
    # Set after fact persistence, inside the same transaction —
    # rollback-safe.  Only applied when an episode_repo is provided.
    if episode_repo is not None:
        await episode_repo.apply_enrichment_bits(
            uuid.UUID(episode_id), ENRICHMENT_FACTS
        )

    await db.flush()

    # ── Chain embed_fact per persisted fact ─────────────────────────────────
    # Runs after flush but before the caller commits.  Failures here are
    # non-blocking — the embed_fact task re-pulls content from the DB on
    # its own retry cycle.
    if arq_redis is not None and persisted_ids:
        try:
            from services.worker.worker_settings import get_queue_name
            from services.worker.worker_settings import settings as w_settings

            for fact_obj in new_facts:
                await arq_redis.enqueue_job(
                    "embed_fact",
                    fact_id=str(fact_obj.id),
                    org_id=org_id,
                    content=fact_obj.content,
                    trace_id=trace_id,
                    _queue_name=get_queue_name(w_settings.ENV, "high"),
                )
                logger.info(
                    "fact_extraction.embed_enqueued",
                    episode_id=episode_id,
                    fact_id=str(fact_obj.id),
                )
        except Exception:
            logger.warning(
                "fact_extraction.embed_enqueue_failed",
                episode_id=episode_id,
                org_id=org_id,
                count=len(persisted_ids),
                exc_info=True,
            )
            # Non-blocking — fact extraction already committed via flush.

    # ── Output-slot map for LLM invalidation successors ──────────────────
    # Each 1-based output slot of ``parsed.facts`` maps to the created
    # Fact row (or ``None`` when the slot was filtered, deduplicated, or
    # skipped by the conflict scan) so the caller can resolve
    # ``"N{index}"`` successor references for LLM-driven invalidations.
    slot_map: dict[int, Fact | None] = {}
    for fact_obj in new_facts:
        input_fact = content_to_fact.get(fact_obj.content)
        slot = input_fact.get("_slot") if input_fact is not None else None
        if slot is not None:
            slot_map[slot] = fact_obj
    slot_map = {i: slot_map.get(i) for i in range(1, len(parsed.facts) + 1)}

    if return_slot_map:
        return new_facts, slot_map
    return persisted_ids


