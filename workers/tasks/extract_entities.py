"""Entity extraction worker — ARQ task that extracts entities from messages.

Runs after an episode is committed to PostgreSQL.  Uses an LLM to extract
named entities (people, organisations, products, locations, etc.) and the
relationships between them from a single conversation turn.  Persists
entities to the Graphiti knowledge graph (if available) and relationships
to the ``facts`` table in PostgreSQL.

Bitmask:
    Sets ``episodes.enrichment_status`` bit 0 (``ENRICHMENT_ENTITIES``)
    on success.
"""

from __future__ import annotations

import re
import uuid

import structlog

from core.exceptions import EpisodeNotFoundError, GraphBackendUnavailableError

# note: Import prompt_renderer at module level — it is a local
# Jinja2 utility with no heavy dependencies, so eager import is safe
# and avoids re-import overhead on every task invocation.
from services.worker.prompt_renderer import build_enrichment_prompt, render_prompt
from workers.backend import resolve_graph_backend
from workers.tasks.base import ENRICHMENT_ENTITIES, with_retry

logger = structlog.get_logger()


# ── Public ARQ task (decorated with retry) ────────────────────────────────────


@with_retry(max_retries=3, base_delay_s=2.0)
async def extract_entities(
    ctx: object,
    episode_id: str,
    org_id: str,
    project_id: str,
    content: str,
    session_id: str | None = None,
    trace_id: str = "",
    metadata: dict | None = None,
) -> None:
    """Extract named entities and relationships from a message and persist them.

    This function is designed as an ARQ task — the ``ctx`` parameter provides
    a shared DB engine from the worker process (``ctx["db_engine"]``).
    When ``ctx`` is absent (direct invocation), a short-lived engine is
    created as a fallback.

    Pipeline:
        1. Fetch organization's entity type ontology from
           ``extraction_schemas (type='entity_type')``.
        2. If ``session_id`` is provided, fetch known entities from previous
           turns of this session for delta extraction (v3 prompt).
        3. Render the extract prompt:
            - ``extract_entities_v4.jinja2`` when known entities exist (delta).
            - ``extract_entities_v3.jinja2`` for the first extraction.
        4. Call the LLM backend (via ``resolve_backend()``, temperature 0.1).
        5. Parse the JSON response (handles markdown fence wrapping).
        6. Validate entity types against the allowed ontology (reassign
           invalid types to ``"Custom"``).
        7. Persist entity nodes to Graphiti via ``EntityRepository``.
        8. Persist relationships as facts in PostgreSQL.
        9. Update ``episodes.enrichment_status`` bit 0.

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        episode_id: UUID of the source episode (string, from ARQ).
        org_id: UUID of the owning organization.
        project_id: UUID of the project for project scoping.
        content: The message text to extract entities from.
        session_id: UUID of the session (passed from MemoryService).
            Used to fetch known entities for delta extraction.
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
    from repositories.entity_repository import EntityRepository
    from repositories.episode_repository import EpisodeRepository
    from schemas.llm_outputs import EntityExtractionOutput

    logger.info(
        "entity_extraction.started",
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

    backend: Any = None
    async with session_factory() as resolve_db:
        result = await resolve_db.execute(
            select(Episode.user_id).where(Episode.id == episode_id)
        )
        user_id_row = result.scalar_one_or_none()
        if user_id_row is None:
            logger.warning(
                "entity_extraction.episode_not_found",
                episode_id=episode_id,
            )
            raise EpisodeNotFoundError(
                message=f"Episode {episode_id} not found for entity extraction.",
                detail={"episode_id": episode_id},
            )
        user_id: str = str(user_id_row)

        # ── Resolve graph backend (same session, avoids extra connection) ──
        try:
            backend = await resolve_graph_backend(
                ctx if isinstance(ctx, dict) else {},
                uuid.UUID(org_id),
                resolve_db,
                fallback_to_postgres=True,
            )
        except Exception:
            logger.warning("entity_extraction.backend_resolve_failed", exc_info=True)

    # ── 1-2. Render prompt (system instructions) with auto-injected context ──
    system_prompt, prompt_context = await render_prompt(
        "entity_extraction",
        org_id=org_id,
        episode_id=episode_id,
        session_id=session_id,
        user_id=user_id,
        project_id=project_id,
        graph_backend=backend,
        db_session_factory=session_factory,
        return_context=True,
        metadata=metadata or {},
    )
    entity_types: list[str] = prompt_context.get("entity_types", [])

    # ── 1b. Build full prompt with context sections ────────────────────────
    prompt = build_enrichment_prompt(system_prompt, prompt_context)

    # ── 2b. Fetch per-organization config ─────────────────────────────────
    llm_config_dict: dict | None = None
    try:
        async with session_factory() as db:
            org_cfg = await get_org_config(uuid.UUID(org_id), db, redis=None)
            llm_config_dict = org_cfg.to_llm_config_dict()
    except Exception:
        logger.warning(
            "entity_extraction.org_config_fetch_failed",
            org_id=org_id,
            exc_info=True,
        )

    # ── 3-4. Call LLM with structured-output validation ──────────────────────
    try:
        llm = await resolve_backend(org_config=llm_config_dict)
        response = await llm.chat(
            [
                {
                    "role": "system",
                    "content": ("You are an entity extraction system."),
                },
                {"role": "user", "content": prompt},
            ],
            response_model=EntityExtractionOutput,
            temperature=0.1,
            max_tokens=4096,
        )
    except Exception as exc:
        logger.error(
            "entity_extraction.llm_failed",
            episode_id=episode_id,
            error=str(exc),
        )
        raise  # Let the @with_retry decorator handle transient failures

    parsed = response.validated_data  # EntityExtractionOutput instance
    entities: list[dict] = [e.model_dump() for e in parsed.entities]
    relationships: list[dict] = [r.model_dump() for r in parsed.relationships]

    # ── Pronoun filter — skip entities that are pronouns or common    ───
    #    misspellings.  Pronouns like "I", "me", "my" should never be
    #    persisted as graph entities — they are resolved to the speaker
    #    during fact extraction (see _match_entity in extract_facts.py).
    _PRONOUN_SKIP_NAMES: set[str] = {
        # First‑person
        "i",
        "me",
        "my",
        "mine",
        "myself",
        "we",
        "us",
        "our",
        "ours",
        "ourselves",
        # Second‑person
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
        # Third‑person
        "he",
        "him",
        "his",
        "himself",
        "she",
        "her",
        "hers",
        "herself",
        "it",
        "its",
        "itself",
        "they",
        "them",
        "their",
        "theirs",
        "themselves",
        # Ambiguous / filler
        "this",
        "that",
        "these",
        "those",
        "someone",
        "somebody",
        "everyone",
        "everybody",
        "nobody",
        "anyone",
        "anybody",
        # Common misspellings (observed: "shhe")
        "shhe",
        "hhe",
        "thei",
        "theyr",
        "thereselves",
        # Questions / catch‑all that leak through extraction
        "what",
        "who",
        "whom",
        "whose",
        "which",
    }

    filtered_entities: list[dict] = []
    for entity in entities:
        name = (entity.get("name") or "").strip()
        if not name:
            logger.warning(
                "entity_extraction.entity_without_name_skipped",
                episode_id=episode_id,
            )
            continue
        if name.lower() in _PRONOUN_SKIP_NAMES:
            logger.info(
                "entity_extraction.pronoun_skipped",
                episode_id=episode_id,
                name=name,
            )
            continue
        filtered_entities.append(entity)
    entities = filtered_entities

    # Clean relationships that reference skipped pronouns
    clean_relationships: list[dict] = []
    for rel in relationships:
        subj = (rel.get("subject") or "").strip()
        obj = (rel.get("object") or "").strip()
        if not subj or not obj:
            logger.warning(
                "entity_extraction.relationship_without_subject_or_object_skipped",
                episode_id=episode_id,
                subject=subj,
                predicate=rel.get("predicate"),
                object=obj,
            )
            continue
        if subj.lower() in _PRONOUN_SKIP_NAMES or obj.lower() in _PRONOUN_SKIP_NAMES:
            logger.info(
                "entity_extraction.relationship_pronoun_skipped",
                episode_id=episode_id,
                subject=subj,
                predicate=rel.get("predicate"),
                object=obj,
            )
            continue
        clean_relationships.append(rel)
    relationships = clean_relationships

    if not entities and not relationships:
        logger.info("entity_extraction.empty", episode_id=episode_id)

    logger.info(
        "entity_extraction.parsed",
        episode_id=episode_id,
        entities=len(entities),
        relationships=len(relationships),
    )

    # ── 5. Validate entity types against allowed ontology ────────────────
    allowed_types: set[str] = set(entity_types) | {"Custom"}
    for entity in entities:
        raw_type = entity.get("type")
        if not raw_type or raw_type not in allowed_types:
            logger.warning(
                "entity_extraction.invalid_type",
                episode_id=episode_id,
                name=entity.get("name"),
                original_type=raw_type,
                reassigned_to="Custom",
                allowed=sorted(allowed_types),
            )
            entity["type"] = "Custom"

    # ── 6. Persist entities to graph (if available) ─────────────────────
    # Uses the shared engine from worker ctx (set earlier in this function).

    name_to_node: dict[str, dict] = {}

    # ── Failure counters for enrichment-bit gating ──────────────────────
    # Note: entity failures now raise GraphBackendUnavailableError and trigger
    # retry via @with_retry. Only relationship failures (entity-not-found in
    # graph) are counted here since they are non-fatal edge cases.
    relationship_failure_count: int = 0
    relationship_skip_count: int = 0

    try:
        async with session_factory() as _db:
            # Resolve per-org graph backend for entity CRUD.
            backend = await resolve_graph_backend(ctx, uuid.UUID(org_id), _db)
            if backend is None:
                raise GraphBackendUnavailableError(
                    "Graph backend unavailable for entity extraction.",
                    detail={"org_id": org_id},
                )
            entity_repo = EntityRepository(db=_db, graph_backend=backend)

            for entity in entities:
                entity_name = entity.get("name", "")
                entity_type = entity.get("type", "Custom")
                mentions: list[str] = entity.get("mentions", [])

                # ── Normalize entity name casing ─────────────────────────
                # Use the first mention's casing if available (it preserves
                # the user's original casing), otherwise capitalize the
                # first letter of a lowercase name.
                normalized_name = entity_name
                if mentions:
                    first_mention = mentions[0].strip()
                    # Only override if the mention looks like a user-typed
                    # name (not a generic noun) — trust the LLM's name field
                    # for the canonical form but prefer title-case mentions.
                    if first_mention and len(first_mention) > 1:
                        normalized_name = first_mention
                elif entity_name and entity_name.islower():
                    normalized_name = entity_name.capitalize()

                summary = (
                    f"{normalized_name} ({entity_type}) — "
                    f"mentioned as: {', '.join(set(mentions))}"
                    if mentions
                    else f"{normalized_name} ({entity_type})"
                )

                node = await entity_repo.upsert_entity(
                    org_id=uuid.UUID(org_id),
                    project_id=uuid.UUID(project_id),
                    name=normalized_name,
                    entity_type=entity_type,
                    summary=summary,
                )
                # Key by normalized name so relationship lookups work
                name_to_node[normalized_name] = node
                # Also key by original name as fallback for callers
                # that might use the raw LLM output
                if normalized_name != entity_name:
                    name_to_node[entity_name] = node

            # ── 7. Persist relationships to graph ─────────────────────────
            for rel in relationships:
                subject = rel.get("subject", "")
                predicate = rel.get("predicate", "")
                obj = rel.get("object", "")

                if not subject or not predicate or not obj:
                    continue

                # ── On-the-fly entity recovery pass ─────────────────────
                # If the LLM included a name in a relationship but didn't
                # declare it in the entities array, auto-create it as a
                # "Custom" type entity so the graph edge is not lost.
                for name in (subject, obj):
                    if name not in name_to_node:
                        fallback_node = await entity_repo.upsert_entity(
                            org_id=uuid.UUID(org_id),
                            project_id=uuid.UUID(project_id),
                            name=name,
                            entity_type="Custom",
                            summary=(
                                f"Auto-created from relationship: "
                                f"{subject} {predicate} {obj}"
                            ),
                        )
                        name_to_node[name] = fallback_node
                        logger.info(
                            "entity_extraction.relationship_entity_recovered",
                            episode_id=episode_id,
                            entity_name=name,
                            relationship=f"{subject} {predicate} {obj}",
                        )

                if subject in name_to_node and obj in name_to_node:
                    result = await entity_repo.upsert_relationship(
                        subject=subject,
                        predicate=predicate,
                        obj=obj,
                        org_id=uuid.UUID(org_id),
                        project_id=uuid.UUID(project_id),
                    )
                    if result is None:
                        relationship_failure_count += 1
                else:
                    relationship_skip_count += 1
                    logger.warning(
                        "entity_extraction.relationship_skipped_missing_entity",
                        episode_id=episode_id,
                        subject=subject,
                        predicate=predicate,
                        object=obj,
                        subject_in_graph=subject in name_to_node,
                        object_in_graph=obj in name_to_node,
                    )

            # ── 8. Link entities to this episode via graph backend ─────────
            # This replaces the separate link_entities_to_episode ARQ task.  Linking
            # happens inline so it's always consistent with entity extraction.
            episode_uuid = uuid.UUID(episode_id)
            for entity_name, entity_node in name_to_node.items():
                await backend.link_entity_to_episode(
                    org_id=uuid.UUID(org_id),
                    project_id=uuid.UUID(project_id),
                    episode_id=episode_uuid,
                    entity_id=uuid.UUID(entity_node["id"]),
                )

            # ── 9. Set enrichment_status bit 0 inside the same transaction ──
            episode_repo = EpisodeRepository(_db)
            await episode_repo.apply_enrichment_bits(
                uuid.UUID(episode_id), ENRICHMENT_ENTITIES
            )

            # ⚠️ Commit is required — SQLAlchemy AsyncSession does NOT
            # auto-commit when the context manager exits. Without this,
            # all entity/relationship writes are silently rolled back.
            await _db.commit()
    finally:
        if _own_engine:
            await engine.dispose()

    logger.info(
        "entity_extraction.persisted",
        episode_id=episode_id,
        org_id=org_id,
        project_id=project_id,
        entity_count=len(name_to_node),
        relationship_failure_count=relationship_failure_count,
        relationship_skip_count=relationship_skip_count,
    )

    entity_count = len(entities)
    if entity_count:
        logger.info(
            "entity_extraction.completed",
            episode_id=episode_id,
            entities=entity_count,
        )
    else:
        logger.info("entity_extraction.done", episode_id=episode_id)

    # ── 10. Enqueue fact extraction (now that entities are committed) ──
    # extract_facts must run AFTER entities are in the DB so that entity
    # IDs can be resolved for graph_relationship edges.  Chaining via
    # enqueue eliminates the race condition.
    try:
        from services.worker.worker_settings import get_queue_name
        from services.worker.worker_settings import settings as w_settings

        arq_redis = ctx.get("redis") if isinstance(ctx, dict) else None
        if arq_redis is not None:
            await arq_redis.enqueue_job(
                "extract_facts",
                episode_id=episode_id,
                org_id=org_id,
                project_id=project_id,
                content=content,
                session_id=session_id,
                trace_id=trace_id,
                metadata=metadata,
                _queue_name=get_queue_name(w_settings.ENV, "high"),
            )
    except Exception:
        logger.warning(
            "entity_extraction.facts_enqueue_failed",
            episode_id=episode_id,
            exc_info=True,
        )
        raise  # Propagate so ARQ retry mechanism handles it
