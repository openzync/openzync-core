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
from datetime import datetime, timezone

import structlog
from sqlalchemy import text

from workers.tasks.base import ENRICHMENT_ENTITIES, with_retry

# TechLead note: Import prompt_renderer at module level — it is a local
# Jinja2 utility with no heavy dependencies, so eager import is safe
# and avoids re-import overhead on every task invocation.
from services.worker.prompt_renderer import render_prompt

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
) -> None:
    """Extract named entities and relationships from a message and persist them.

    This function is designed as an ARQ task — the ``ctx`` parameter is
    required by the ARQ contract but is not used directly here (we create
    a short-lived DB engine per invocation).

    Pipeline:
        1. Fetch organization's entity type ontology from
           ``extraction_schemas (type='entity_type')``.
        2. If ``session_id`` is provided, fetch known entities from previous
           turns of this session for delta extraction (v3 prompt).
        3. Render the extract prompt:
           - ``extract_entities_v3.jinja2`` when known entities exist (delta).
           - ``extract_entities_v1.jinja2`` for the first extraction.
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

    Raises:
        Exception: Re-raises the last LLM or DB error after retry exhaustion
            (``on_exhaustion="raise"`` default behaviour).
    """
    # Lazy imports to keep the module importable without the full async
    # stack at definition time — ARQ workers run in a separate process.
    from core.config import settings
    from core.db import get_async_session, init_db_engine
    from core.llm import resolve_backend
    from models.episode import Episode
    from repositories.entity_repository import EntityRepository
    from repositories.fact_repository import FactRepository
    from sqlalchemy import select

    logger.info(
        "entity_extraction.started",
        episode_id=episode_id,
        org_id=org_id,
        session_id=session_id,
        content_length=len(content),
    )

    # ── 1. Fetch org entity types (ontology) ──────────────────────────────────
    entity_types = await _fetch_entity_types(org_id)
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
            await ctx_engine.dispose()
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
    prompt_template = "extract_entities_v3" if known_entities else "extract_entities_v1"
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
                        "You are an entity extraction system. "
                        "Output ONLY valid JSON."
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

    persisted_count = 0

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
            "i", "me", "my", "mine", "myself", "we", "us", "our", "ours",
            "ourselves",
            # Second‑person
            "you", "your", "yours", "yourself", "yourselves",
            # Third‑person
            "he", "him", "his", "himself", "she", "her", "hers", "herself",
            "it", "its", "itself", "they", "them", "their", "theirs",
            "themselves",
            # Ambiguous / filler
            "this", "that", "these", "those", "someone", "somebody",
            "everyone", "everybody", "nobody", "anyone", "anybody",
            # Common misspellings (observed: "shhe")
            "shhe", "hhe", "thei", "theyr", "thereselves",
            # Questions / catch‑all that leak through extraction
            "what", "who", "whom", "whose", "which",
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
        # Create a temporary DB engine + session for the entity repository
        from sqlalchemy.ext.asyncio import create_async_engine as _create_engine

        _engine = _create_engine(
            str(settings.DATABASE_URL),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=2,
        )
        _session_factory = get_async_session(_engine)

        name_to_node: dict[str, dict] = {}

        try:
            async with _session_factory() as _db:
                entity_repo = EntityRepository(db=_db)

                for entity in entities:
                    entity_name = entity.get("name", "")
                    entity_type = entity.get("type", "Custom")
                    mentions: list[str] = entity.get("mentions", [])
                    summary = (
                        f"{entity_name} ({entity_type}) — "
                        f"mentioned as: {', '.join(set(mentions))}"
                        if mentions
                        else f"{entity_name} ({entity_type})"
                    )

                    node = await entity_repo.upsert_entity(
                        org_id=uuid.UUID(org_id),
                        name=entity_name,
                        entity_type=entity_type,
                        summary=summary,
                    )
                    if node is not None:
                        name_to_node[entity_name] = node

                # ── 7. Persist relationships to graph ─────────────────────────
                for rel in relationships:
                    subject = rel.get("subject", "")
                    predicate = rel.get("predicate", "")
                    obj = rel.get("object", "")

                    if not subject or not predicate or not obj:
                        continue

                    if subject in name_to_node and obj in name_to_node:
                        await entity_repo.upsert_relationship(
                            subject=subject, predicate=predicate, obj=obj,
                            org_id=uuid.UUID(org_id),
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
            await _engine.dispose()

        # ── 9. Persist relationships as facts in PostgreSQL ───────────────────
        if relationships:
            entity_type_map: dict[str, str] = {
                e["name"]: e.get("type", "Custom") for e in entities
            }
            # Build name → UUID map from upserted entity nodes
            name_to_uuid: dict[str, uuid.UUID | None] = {
                name: uuid.UUID(node["id"]) if node.get("id") else None
                for name, node in name_to_node.items()
            }
            engine = init_db_engine(str(settings.DATABASE_URL), pool_size=5, max_overflow=2)
            session_factory = get_async_session(engine)
            try:
                async with session_factory() as db:
                    episode_uuid = uuid.UUID(episode_id)
                    org_uuid = uuid.UUID(org_id)
                    user_uuid = uuid.UUID(user_id)
                    now = datetime.now(timezone.utc)

                    for rel in relationships:
                        subject = rel.get("subject", "")
                        predicate = rel.get("predicate", "")
                        obj = rel.get("object", "")
                        if not subject or not predicate or not obj:
                            continue
                        await db.execute(
                            text("""
                                INSERT INTO facts
                                    (id, user_id, organization_id, content,
                                     subject, predicate, "object",
                                     subject_type, object_type,
                                     subject_entity_id, object_entity_id,
                                     confidence, source_episode_id,
                                     valid_from, created_at, updated_at)
                                VALUES
                                    (gen_random_uuid(), :user_id, :org_id, :content,
                                     :subject, :predicate, :object,
                                     :subject_type, :object_type,
                                     :subj_entity_id, :obj_entity_id,
                                     1.0, :episode_id,
                                     :valid_from, :valid_from, :valid_from)
                            """),
                            {
                                "user_id": user_uuid, "org_id": org_uuid,
                                "content": f"{subject} {predicate} {obj}",
                                "subject": subject, "predicate": predicate,
                                "object": obj,
                                "subject_type": entity_type_map.get(subject, "literal"),
                                "object_type": entity_type_map.get(obj, "literal"),
                                "subj_entity_id": name_to_uuid.get(subject),
                                "obj_entity_id": name_to_uuid.get(obj),
                                "episode_id": episode_uuid, "valid_from": now,
                            },
                        )
                    await db.commit()
                    persisted_count = len(relationships)
            finally:
                await engine.dispose()

        logger.info(
            "entity_extraction.persisted",
            episode_id=episode_id,
            entity_count=len(name_to_node),
            relationship_count=persisted_count,
        )

    # ── 10. Always set enrichment_status bit 0 ────────────────────────────────
    await _set_enrichment_bit(episode_id, ENRICHMENT_ENTITIES)

    if persisted_count:
        logger.info("entity_extraction.completed", episode_id=episode_id,
                     entities=len(entities if data else []),
                     facts=persisted_count)
    else:
        logger.info("entity_extraction.done", episode_id=episode_id)


# ── Private helpers ───────────────────────────────────────────────────────────


async def _fetch_entity_types(org_id: str) -> list[str]:
    """Fetch entity type ontology from the organization's extraction schemas.

    Queries ``extraction_schemas`` where ``type='entity_type'`` and
    ``is_active=true``.  The ``json_schema`` field is expected to contain
    ``{"types": ["Type1", "Type2", ...]}``.

    Falls back to the default type set if no schemas are configured.

    Returns:
        A list of allowed entity type names.

    Raises:
        Exception: Re-raises DB errors so the caller's ``@with_retry``
            decorator can handle transient failures.
    """
    from core.config import settings
    from core.db import get_async_session, init_db_engine

    _engine = init_db_engine(
        str(settings.DATABASE_URL), pool_size=2, max_overflow=1
    )
    _session_factory = get_async_session(_engine)
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
                return ["Person", "Organization", "Product", "Location", "Date", "Custom"]

            types: list[str] = []
            for row in schemas:
                schema: dict = row[0]
                if isinstance(schema, dict) and "types" in schema and isinstance(schema["types"], list):
                    types.extend(schema["types"])

            return types or ["Person", "Organization", "Product", "Location", "Date", "Custom"]
    finally:
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
    json_start = content.find('{')
    if json_start < 0:
        json_start = content.find('[')
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


async def _set_enrichment_bit(episode_id: str, bit: int) -> None:
    """Set an enrichment_status bit for an episode.

    Always runs, even if no data was found — marks the task as complete
    so the pipeline knows it has been attempted.
    """
    from core.db import get_async_session, init_db_engine
    from core.config import settings as app_settings
    from sqlalchemy import text

    engine = init_db_engine(str(app_settings.DATABASE_URL), pool_size=2, max_overflow=1)
    session_factory = get_async_session(engine)
    try:
        async with session_factory() as db:
            await db.execute(
                text("UPDATE episodes SET enrichment_status = enrichment_status | :bit WHERE id = :id"),
                {"bit": bit, "id": uuid.UUID(episode_id)},
            )
            await db.commit()
    except Exception as exc:
        logger.warning("enrichment_bit_failed", extra={"episode_id": episode_id, "bit": bit, "error": str(exc)})
    finally:
        await engine.dispose()
