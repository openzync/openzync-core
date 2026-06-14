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

import json
import re
import uuid

import structlog
from sqlalchemy import text

from core.exceptions import ExternalServiceError

# note: Import prompt_renderer at module level — it is a local
# Jinja2 utility with no heavy dependencies, so eager import is safe
# and avoids re-import overhead on every task invocation.
from services.worker.prompt_renderer import render_prompt
from workers.tasks.base import ENRICHMENT_ENTITIES, with_retry

logger = structlog.get_logger()


# ── Public ARQ task (decorated with retry) ────────────────────────────────────


@with_retry(max_retries=3, base_delay_s=2.0)
async def extract_entities(
    ctx: object,
    episode_id: str,
    org_id: str,
    user_id: str,
    content: str,
    session_id: str | None = None,
    trace_id: str = "",
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
        user_id: UUID of the user who authored the message.
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
    from sqlalchemy import select

    from core.config import settings
    from core.db import get_async_session
    from core.llm import resolve_backend
    from models.episode import Episode
    from repositories.entity_repository import EntityRepository
    from repositories.fact_repository import FactRepository

    logger.info(
        "entity_extraction.started",
        episode_id=episode_id,
        org_id=org_id,
        session_id=session_id,
        content_length=len(content),
        trace_id=trace_id,
    )

    # Use the shared engine from worker context.
    engine = ctx.get("db_engine") if isinstance(ctx, dict) else None
    if engine is None:
        from core.db import init_db_engine

        engine = init_db_engine(
            str(settings.DATABASE_URL), pool_size=2, max_overflow=1
        )
        _own_engine = True
    else:
        _own_engine = False
    session_factory = ctx.get("db_session_factory") if isinstance(ctx, dict) else None
    if session_factory is None:
        session_factory = get_async_session(engine)

    # ── 1. Fetch org entity types (ontology) ──────────────────────────────────
    entity_types = await _fetch_entity_types(org_id, session_factory=session_factory)
    logger.debug(
        "entity_extraction.entity_types",
        episode_id=episode_id,
        org_id=org_id,
        entity_types=entity_types,
    )

    # ── 1b. Fetch known entities for delta extraction (if session_id) ─────────
    known_entities: list[dict] = []
    if session_id:
        try:
            async with session_factory() as db:
                repo = FactRepository(db)
                known_entities = await repo.get_entities_for_session(
                    session_id=uuid.UUID(session_id),
                    organization_id=uuid.UUID(org_id),
                )
            logger.debug(
                "entity_extraction.known_entities_fetched",
                episode_id=episode_id,
                known_entities=len(known_entities),
            )
        except Exception as exc:
            # ⚠️ Non-fatal: continue without context if DB is unavailable
            logger.warning(
                "entity_extraction.known_entities_failed",
                episode_id=episode_id,
                session_id=session_id,
                error=str(exc),
            )

    # ── 2. Render prompt ──────────────────────────────────────────────────────
    prompt_template = "extract_entities_v4" if known_entities else "extract_entities_v3"
    try:
        prompt = render_prompt(
            prompt_template,
            conversation=content,
            entity_types=entity_types,
            known_entities=known_entities,
        )
    except FileNotFoundError:
        logger.error(
            "entity_extraction.prompt_missing",
            episode_id=episode_id,
            template=f"{prompt_template}.jinja2",
        )
        return

    # ── 3. Call LLM ───────────────────────────────────────────────────────────
    try:
        llm = await resolve_backend()
        response = await llm.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are an entity extraction system. Output ONLY valid JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )
    except Exception as exc:
        logger.error(
            "entity_extraction.llm_failed",
            episode_id=episode_id,
            error=str(exc),
        )
        raise  # Let the @with_retry decorator handle transient failures

    # ── 4. Parse JSON response with recovery for malformed output ────────────
    data = _parse_entity_response(response.content)

    # Recovery attempt: if the first parse failed, retry with a stricter
    # system prompt and temperature 0.0 for determinism.
    if data is None:
        logger.warning(
            "entity_extraction.parse_recovery",
            episode_id=episode_id,
        )
        try:
            recovery_prompt = render_prompt(
                prompt_template,
                conversation=content,
                entity_types=entity_types,
                known_entities=known_entities,
            )
            response2 = await llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "CRITICAL: You MUST output valid JSON only. "
                            "No other text, no markdown fences, no explanation."
                        ),
                    },
                    {"role": "user", "content": recovery_prompt},
                ],
                temperature=0.0,
            )
            data = _parse_entity_response(response2.content)
        except Exception as exc:
            logger.error(
                "entity_extraction.recovery_failed",
                episode_id=episode_id,
                error=str(exc),
            )
            return

    if data is None:
        logger.warning("entity_extraction.no_result", episode_id=episode_id)
    else:
        entities: list[dict] = data.get("entities", [])
        relationships: list[dict] = data.get("relationships", [])

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
            if (
                subj.lower() in _PRONOUN_SKIP_NAMES
                or obj.lower() in _PRONOUN_SKIP_NAMES
            ):
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
        entity_failure_count: int = 0
        relationship_failure_count: int = 0
        relationship_skip_count: int = 0

        try:
            async with session_factory() as _db:
                entity_repo = EntityRepository(db=_db)

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
                        name=normalized_name,
                        entity_type=entity_type,
                        summary=summary,
                    )
                    if node is not None:
                        # Key by normalized name so relationship lookups work
                        name_to_node[normalized_name] = node
                        # Also key by original name as fallback for callers
                        # that might use the raw LLM output
                        if normalized_name != entity_name:
                            name_to_node[entity_name] = node
                    else:
                        entity_failure_count += 1
                        logger.warning(
                            "entity_extraction.entity_upsert_skipped",
                            episode_id=episode_id,
                            entity_name=normalized_name,
                            entity_type=entity_type,
                        )

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
                                name=name,
                                entity_type="Custom",
                                summary=(
                                    f"Auto-created from relationship: "
                                    f"{subject} {predicate} {obj}"
                                ),
                            )
                            if fallback_node is not None:
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

                # ── 8. Link entities to this episode in graph_episode_entities ───
                # This replaces the separate sync_to_graph ARQ task.  Linking
                # happens inline so it's always consistent with entity extraction.
                episode_uuid = uuid.UUID(episode_id)
                for entity_name, entity_node in name_to_node.items():
                    await _db.execute(
                        text(
                            """
                            INSERT INTO graph_episode_entities
                                (episode_id, entity_id, created_at)
                            VALUES (:episode_id, :entity_id, now())
                            ON CONFLICT (episode_id, entity_id) DO NOTHING
                            """
                        ),
                        {
                            "episode_id": episode_uuid,
                            "entity_id": uuid.UUID(entity_node["id"]),
                        },
                    )

                # ⚠️ Commit is required — SQLAlchemy AsyncSession does NOT
                # auto-commit when the context manager exits. Without this,
                # all entity/relationship writes are silently rolled back.
                await _db.commit()
        finally:
            if _own_engine:
                await engine.dispose()

        # ── 8b. Guard enrichment bit against persistence failures ────────────
        # If entities failed to persist, raise so @with_retry can retry.
        # Enrichment bit will NOT be set on this attempt (line 524 won't run).
        if entity_failure_count > 0:
            logger.error(
                "entity_extraction.entity_persistence_failures",
                episode_id=episode_id,
                entity_failure_count=entity_failure_count,
                entity_success_count=len(name_to_node),
                relationship_failure_count=relationship_failure_count,
                relationship_skip_count=relationship_skip_count,
            )
            raise ExternalServiceError(
                message=f"Failed to persist {entity_failure_count} entities to graph "
                f"(backend returned None). {len(name_to_node)} entities succeeded.",
                detail={
                    "episode_id": episode_id,
                    "entity_failure_count": entity_failure_count,
                    "entity_success_count": len(name_to_node),
                    "relationship_failure_count": relationship_failure_count,
                    "relationship_skip_count": relationship_skip_count,
                },
            )

        logger.info(
            "entity_extraction.persisted",
            episode_id=episode_id,
            entity_count=len(name_to_node),
            entity_failure_count=entity_failure_count,
            relationship_failure_count=relationship_failure_count,
            relationship_skip_count=relationship_skip_count,
        )

    # ── 9. Set enrichment_status bit 0 ───────────────────────────────────────
    # Only reached if entity persistence succeeded (no ExternalServiceError
    # raised by the guard at step 8b) — enrichment bit is NOT set when
    # entities were silently dropped, allowing @with_retry to re-attempt.
    await _set_enrichment_bit(
        episode_id,
        ENRICHMENT_ENTITIES,
        db_session_factory=session_factory,
    )

    entity_count = len(entities if data else [])
    if entity_count:
        logger.info(
            "entity_extraction.completed",
            episode_id=episode_id,
            entities=entity_count,
        )
    else:
        logger.info("entity_extraction.done", episode_id=episode_id)


# ── Private helpers ───────────────────────────────────────────────────────────


async def _fetch_entity_types(
    org_id: str,
    session_factory: Any = None,
) -> list[str]:
    """Fetch entity type ontology from the organization's extraction schemas.

    Queries ``extraction_schemas`` where ``type='entity_type'`` and
    ``is_active=true``.  The ``json_schema`` field is expected to contain
    ``{"types": ["Type1", "Type2", ...]}``.

    Falls back to the default type set if no schemas are configured.

    Args:
        org_id: Organization UUID string.
        session_factory: Optional shared session factory from the worker
            ctx.  When provided, avoids creating a short-lived DB engine.

    Returns:
        A list of allowed entity type names.

    Raises:
        Exception: Re-raises DB errors so the caller's ``@with_retry``
            decorator can handle transient failures.
    """
    if session_factory is None:
        from core.config import settings as _settings
        from core.db import get_async_session, init_db_engine

        _engine = init_db_engine(
            str(_settings.DATABASE_URL), pool_size=2, max_overflow=1
        )
        _session_factory = get_async_session(_engine)
        _own_engine = True
    else:
        _engine = None
        _session_factory = session_factory
        _own_engine = False
    try:
        async with _session_factory() as _db:
            result = await _db.execute(
                text("""
                    SELECT json_schema FROM extraction_schemas
                    WHERE organization_id = :org_id
                      AND type = 'entity_type'
                      AND is_active = true
                """),
                {"org_id": uuid.UUID(org_id)},
            )
            schemas = result.all()
            if not schemas:
                return [
                    "Person",
                    "Organization",
                    "Product",
                    "Location",
                    "Date",
                    "Custom",
                ]

            types: list[str] = []
            for row in schemas:
                schema: dict = row[0]
                if (
                    isinstance(schema, dict)
                    and "types" in schema
                    and isinstance(schema["types"], list)
                ):
                    types.extend(schema["types"])

            return types or [
                "Person",
                "Organization",
                "Product",
                "Location",
                "Date",
                "Custom",
            ]
    finally:
        if _own_engine:
            await _engine.dispose()


def _parse_entity_response(content: str) -> dict | None:
    """Parse LLM JSON response for entities and relationships.

    Handles common LLM output quirks: markdown code fences, trailing
    commas, and extra text before/after the JSON object.

    Args:
        content: Raw response text from the LLM.

    Returns:
        A dict with ``entities`` and ``relationships`` keys, or ``None``
        if parsing failed or the structure is invalid.
    """
    # Strip markdown code fences if present — handles both ```json and ```
    if "```json" in content:
        content = content.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in content:
        content = content.split("```", 1)[1].split("```", 1)[0].strip()

    # Strip leading/trailing whitespace that may remain after fence removal
    content = content.strip()

    # Strip deepseek-r1 thinking blocks: find first JSON object or array
    json_start = content.find("{")
    if json_start < 0:
        json_start = content.find("[")
    if json_start >= 0:
        content = content[json_start:].strip()

    if not content:
        return None

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logger.warning(
            "entity_extraction.parse_failed",
            content_preview=content[:300],
        )
        return None

    if not isinstance(data, dict):
        logger.warning(
            "entity_extraction.unexpected_type",
            json_type=type(data).__name__,
        )
        return None

    # Normalise: ensure both arrays exist so callers don't need to check
    if "entities" not in data:
        data["entities"] = []
    if "relationships" not in data:
        data["relationships"] = []

    return data


async def _set_enrichment_bit(
    episode_id: str,
    bit: int,
    db_session_factory: Any = None,
) -> None:
    """Set an enrichment_status bit for an episode.

    Always runs, even if no data was found — marks the task as complete
    so the pipeline knows it has been attempted.

    Args:
        episode_id: UUID of the episode to update.
        bit: Bitmask value to OR into enrichment_status.
        db_session_factory: Optional shared session factory from the worker
            ctx.  When provided, avoids creating a short-lived DB engine.
    """
    from sqlalchemy import text

    if db_session_factory is None:
        from core.config import settings as app_settings
        from core.db import get_async_session, init_db_engine

        engine = init_db_engine(
            str(app_settings.DATABASE_URL), pool_size=2, max_overflow=1
        )
        session_factory = get_async_session(engine)
        _own_engine = True
    else:
        engine = None
        session_factory = db_session_factory
        _own_engine = False

    try:
        async with session_factory() as db:
            await db.execute(
                text(
                    "UPDATE episodes SET enrichment_status = enrichment_status | :bit WHERE id = :id"
                ),
                {"bit": bit, "id": uuid.UUID(episode_id)},
            )
            await db.commit()
    except Exception as exc:
        logger.warning(
            "enrichment_bit_failed",
            extra={"episode_id": episode_id, "bit": bit, "error": str(exc)},
        )
    finally:
        if _own_engine:
            await engine.dispose()
