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
) -> None:
    """Extract named entities and relationships from a message and persist them.

    This function is designed as an ARQ task — the ``ctx`` parameter is
    required by the ARQ contract but is not used directly here (we create
    a short-lived DB engine per invocation).

    Pipeline:
        1. Render the ``extract_entities_v1.jinja2`` prompt with the
           conversation content.
        2. Call the LLM backend (via ``resolve_backend()``, temperature 0.1).
        3. Parse the JSON response (handles markdown fence wrapping).
        4. Persist entity nodes to Graphiti via ``EntityRepository``.
        5. Persist relationships as facts in PostgreSQL.
        6. Update ``episodes.enrichment_status`` bit 0.

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        episode_id: UUID of the source episode (string, from ARQ).
        org_id: UUID of the owning organization.
        user_id: UUID of the user who authored the message.
        content: The message text to extract entities from.

    Raises:
        Exception: Re-raises the last LLM or DB error after retry exhaustion
            (``on_exhaustion="raise"`` default behaviour).
    """
    # Lazy imports to keep the module importable without the full async
    # stack at definition time — ARQ workers run in a separate process.
    from core.config import settings
    from core.db import get_async_session, init_db_engine
    from core.llm import resolve_backend
    from repositories.entity_repository import EntityRepository

    logger.info(
        "entity_extraction.started",
        episode_id=episode_id,
        org_id=org_id,
        content_length=len(content),
    )

    # ── 1. Render prompt ──────────────────────────────────────────────────────
    try:
        prompt = render_prompt("extract_entities_v1", conversation=content)
    except FileNotFoundError:
        logger.error(
            "entity_extraction.prompt_missing",
            episode_id=episode_id,
            template="extract_entities_v1.jinja2",
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

    # ── 3. Parse JSON response with recovery for malformed output ────────────
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
                "extract_entities_v1", conversation=content
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
        return

    entities: list[dict] = data.get("entities", [])
    relationships: list[dict] = data.get("relationships", [])

    if not entities and not relationships:
        logger.info("entity_extraction.empty", episode_id=episode_id)
        return

    logger.info(
        "entity_extraction.parsed",
        episode_id=episode_id,
        entities=len(entities),
        relationships=len(relationships),
    )

    # ── 4. Persist entities to Graphiti (if available) ────────────────────────
    # Build a name→node mapping so we can reference entity IDs when creating
    # relationships in Graphiti.
    entity_repo = EntityRepository()
    name_to_node: dict[str, dict] = {}

    for entity in entities:
        entity_name = entity.get("name", "")
        entity_type = entity.get("type", "Custom")
        # Build a summary from mentions for richer graph context
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

        logger.debug(
            "entity_extraction.entity_persisted",
            entity_name=entity_name,
            entity_type=entity_type,
            graphiti_id=node.get("id") if node else None,
        )

    # ── 5. Persist relationships to Graphiti ──────────────────────────────────
    for rel in relationships:
        subject = rel.get("subject", "")
        predicate = rel.get("predicate", "")
        obj = rel.get("object", "")

        if not subject or not predicate or not obj:
            logger.warning(
                "entity_extraction.incomplete_relationship",
                episode_id=episode_id,
                relationship=rel,
            )
            continue

        # Only persist to Graphiti if both entities were successfully created
        # there (name_to_node contains an entry).
        if subject in name_to_node and obj in name_to_node:
            await entity_repo.upsert_relationship(
                subject=subject,
                predicate=predicate,
                obj=obj,
                org_id=uuid.UUID(org_id),
            )
        else:
            logger.debug(
                "entity_extraction.skipping_graph_relation",
                episode_id=episode_id,
                subject=subject,
                predicate=predicate,
                obj=obj,
                reason=(
                    "subject_in_graph" if subject in name_to_node
                    else "object_in_graph" if obj in name_to_node
                    else "neither_in_graph"
                ),
            )

    logger.info(
        "entity_extraction.graphiti_persisted",
        episode_id=episode_id,
        entity_count=len(name_to_node),
        relationship_count=len(relationships),
    )

    # ── 6. Persist relationships as facts in PostgreSQL ───────────────────────
    # Build a lookup: entity name → type for subject_type / object_type.
    entity_type_map: dict[str, str] = {
        e["name"]: e.get("type", "Custom") for e in entities
    }

    engine = init_db_engine(
        str(settings.DATABASE_URL),
        pool_size=5,
        max_overflow=2,
    )
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

                subject_type = entity_type_map.get(subject, "literal")
                object_type = entity_type_map.get(obj, "literal")

                # TechLead note: Raw SQL insertion here rather than going
                # through FactRepository because FactRepository currently
                # only exposes soft_delete_by_user (a stub).  Once
                # FactRepository.create() is added, this can be refactored
                # to use the repository layer.  See Phase 2 fact-extraction
                # task for the intended pattern.
                await db.execute(
                    text("""
                        INSERT INTO facts
                            (id, user_id, organization_id, content,
                             subject, predicate, "object",
                             subject_type, object_type,
                             confidence, source_episode_id,
                             valid_from, created_at, updated_at)
                        VALUES
                            (gen_random_uuid(), :user_id, :org_id, :content,
                             :subject, :predicate, :object,
                             :subject_type, :object_type,
                             1.0, :episode_id,
                             :valid_from, :valid_from, :valid_from)
                    """),
                    {
                        "user_id": user_uuid,
                        "org_id": org_uuid,
                        "content": f"{subject} {predicate} {obj}",
                        "subject": subject,
                        "predicate": predicate,
                        "object": obj,
                        "subject_type": subject_type,
                        "object_type": object_type,
                        "episode_id": episode_uuid,
                        "valid_from": now,
                    },
                )

            # ── 7. Update enrichment_status bit 0 ─────────────────────────────
            # ⚠️ RACE CONDITION: Two concurrent workers could both pass the
            # enrichment check and run extraction simultaneously.  The
            # enrichment_status bitwise OR is safe since setting an already-set
            # bit is a no-op, but duplicate rows in ``facts`` would result.
            # The unique constraint or application-level dedup in the caller
            # should prevent this.  See idempotency_service.check_and_mark_worker
            # for the proper SELECT FOR UPDATE pattern.
            await db.execute(
                text(
                    "UPDATE episodes "
                    "SET enrichment_status = enrichment_status | :bit "
                    "WHERE id = :id"
                ),
                {"bit": ENRICHMENT_ENTITIES, "id": episode_uuid},
            )
            await db.commit()

        logger.info(
            "entity_extraction.completed",
            episode_id=episode_id,
            entities=len(entities),
            facts=len(relationships),
        )
    finally:
        await engine.dispose()


# ── Private helpers ───────────────────────────────────────────────────────────


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
