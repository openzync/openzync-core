"""ARQ worker for community summarisation — scheduled nightly.

Pipeline:
1. Fetch all entities + relationships for an org from PostgreSQL
2. Build a NetworkX graph
3. Run Label Propagation community detection
4. For each community with >= 2 entities, generate an LLM summary
5. Store community entities + MEMBER_OF edges in graph_entities/relationships
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

COMMUNITY_MIN_ENTITY_COUNT = 5
"""Minimum entities required for community detection to run for an org."""

PROMPT_NAME = "summarise_community_v1"
"""Name of the Jinja2 prompt template for community summarisation."""


async def summarise_community(ctx: dict, org_id: str | None = None) -> dict:
    """ARQ worker: run community detection and summarisation.

    Can be called:
    - Without ``org_id`` (scheduled run): processes all eligible orgs.
    - With ``org_id`` (manual trigger): processes a single org.

    Args:
        ctx: ARQ worker context (includes ``redis``, ``db``).
        org_id: Optional org UUID to process (processes all if ``None``).

    Returns:
        Dict with ``status``, ``orgs_processed``, ``communities_created``.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from core.config import settings

    engine = create_async_engine(
        str(settings.DATABASE_URL),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=2,
    )

    from core.db import get_async_session

    session_factory = get_async_session(engine)

    try:
        async with session_factory() as db:
            # Determine which orgs to process
            if org_id:
                org_ids = [UUID(org_id)]
            else:
                # Find orgs with enough entities
                result = await db.execute(
                    text(
                        """
                        SELECT organization_id, count(*) as cnt
                        FROM graph_entities
                        WHERE entity_type != 'community'
                        GROUP BY organization_id
                        HAVING count(*) >= :min_count
                        """
                    ),
                    {"min_count": COMMUNITY_MIN_ENTITY_COUNT},
                )
                org_ids = [r[0] for r in result.all()]

            if not org_ids:
                return {"status": "skipped", "reason": "No eligible orgs found"}

            total_communities = 0
            for current_org_id in org_ids:
                try:
                    communities = await _process_org(db, current_org_id)
                    total_communities += communities
                except Exception as exc:
                    logger.error(
                        "community.process_org_failed",
                        extra={"org_id": str(current_org_id), "error": str(exc)},
                    )

            return {
                "status": "completed",
                "orgs_processed": len(org_ids),
                "communities_created": total_communities,
            }

    finally:
        await engine.dispose()


async def _process_org(db: AsyncSession, org_id: UUID) -> int:
    """Run community detection and summarisation for a single org.

    Args:
        db: Database session.
        org_id: Organization UUID.

    Returns:
        Number of communities created.
    """
    from sqlalchemy import text

    from packages.community.algorithms import (
        build_entity_graph,
        detect_communities_label_propagation,
    )

    # 1. Fetch all entities (non-community)
    result = await db.execute(
        text(
            """
            SELECT id, name, entity_type, summary
            FROM graph_entities
            WHERE organization_id = :org_id AND entity_type != 'community'
            """
        ),
        {"org_id": org_id},
    )
    entities = [
        {"id": str(r[0]), "name": r[1], "type": r[2], "summary": r[3] or ""}
        for r in result.all()
    ]

    if len(entities) < COMMUNITY_MIN_ENTITY_COUNT:
        logger.info(
            "community.too_few_entities",
            extra={"org_id": str(org_id), "count": len(entities)},
        )
        return 0

    # 2. Fetch relationships
    result = await db.execute(
        text(
            """
            SELECT source_id, target_id, relationship_type
            FROM graph_relationships
            WHERE organization_id = :org_id AND invalid_at IS NULL
            """
        ),
        {"org_id": org_id},
    )
    relationships = [
        {"source_id": str(r[0]), "target_id": str(r[1]), "relationship_type": r[2]}
        for r in result.all()
    ]

    # 3. Build graph and detect communities
    graph = build_entity_graph(entities, relationships)
    communities = detect_communities_label_propagation(graph)

    if not communities:
        logger.info("community.no_communities_found", extra={"org_id": str(org_id)})
        return 0

    # 4. Generate summaries and store
    created = 0
    for community_nodes in communities:
        try:
            await _create_community(
                db=db,
                org_id=org_id,
                entity_ids=list(community_nodes),
                all_entities=entities,
                all_relationships=relationships,
            )
            created += 1
        except Exception as exc:
            logger.error(
                "community.create_failed",
                extra={"org_id": str(org_id), "error": str(exc)},
            )

    await db.commit()
    logger.info(
        "community.org_completed",
        extra={"org_id": str(org_id), "communities": created},
    )
    return created


async def _create_community(
    db: AsyncSession,
    org_id: UUID,
    entity_ids: list[str],
    all_entities: list[dict],
    all_relationships: list[dict],
) -> None:
    """Create a community entity, generate summary, and link members.

    Args:
        db: Database session.
        org_id: Organization UUID.
        entity_ids: UUIDs of entities in this community.
        all_entities: All entities for building the prompt context.
        all_relationships: All relationships for building the prompt context.
    """
    from sqlalchemy import text

    from core.llm import resolve_backend

    # Build entity name map
    entity_map = {e["id"]: e for e in all_entities}
    member_names = [entity_map[eid]["name"] for eid in entity_ids if eid in entity_map]
    community_name = f"Community: {', '.join(member_names[:3])}{'...' if len(member_names) > 3 else ''}"

    # Build context for LLM
    context_entities = [entity_map[eid] for eid in entity_ids if eid in entity_map]
    context_rels = [
        r
        for r in all_relationships
        if r["source_id"] in entity_ids and r["target_id"] in entity_ids
    ]

    # Generate summary via LLM
    try:
        prompt = _build_community_prompt(context_entities, context_rels)
        llm = await resolve_backend()
        response = await llm.chat(
            [
                {
                    "role": "system",
                    "content": "You are an analyst. Output ONLY the summary text, no preamble.",
                },
                {"role": "user", "content": prompt},
            ]
        )
        summary = response.content.strip()
    except Exception as exc:
        logger.warning("community.llm_summary_failed", extra={"error": str(exc)})
        summary = (
            f"Community of {len(member_names)} entities: {', '.join(member_names)}"
        )

    # Create community entity
    now = datetime.now(timezone.utc)
    result = await db.execute(
        text(
            """
            INSERT INTO graph_entities
                (organization_id, name, entity_type, summary, attributes, created_at)
            VALUES (:org_id, :name, 'community', :summary, :attributes, :created_at)
            RETURNING id
            """
        ),
        {
            "org_id": org_id,
            "name": community_name,
            "summary": summary,
            "attributes": json.dumps({"member_count": len(entity_ids)}),
            "created_at": now,
        },
    )
    community_id = result.scalar_one()

    # Create MEMBER_OF edges
    for eid in entity_ids:
        await db.execute(
            text(
                """
                INSERT INTO graph_relationships
                    (organization_id, source_id, target_id, relationship_type, created_at)
                VALUES (:org_id, :entity_id, :community_id, 'member_of', :created_at)
                """
            ),
            {
                "org_id": org_id,
                "entity_id": UUID(eid),
                "community_id": community_id,
                "created_at": now,
            },
        )

    logger.info(
        "community.created",
        extra={
            "org_id": str(org_id),
            "community_id": str(community_id),
            "member_count": len(entity_ids),
            "summary_preview": summary[:80],
        },
    )


def _build_community_prompt(entities: list[dict], relationships: list[dict]) -> str:
    """Build a prompt for community summarisation.

    Args:
        entities: List of entity dicts in the community.
        relationships: List of relationship dicts between community members.

    Returns:
        Prompt string for the LLM.
    """
    parts = [
        "Analyze these entities and their relationships. Write a 2-3 sentence summary "
        "describing what this group represents, who the key entities are, "
        "and what patterns exist between them.",
        "",
        "Entities:",
    ]
    for e in entities:
        parts.append(
            f"- {e.get('name', '?')} ({e.get('type', '?')}): {e.get('summary', '')[:100]}"
        )

    if relationships:
        parts.extend(["", "Relationships:"])
        for r in relationships:
            src_name = next(
                (e["name"] for e in entities if e["id"] == r["source_id"]),
                r["source_id"][:8],
            )
            tgt_name = next(
                (e["name"] for e in entities if e["id"] == r["target_id"]),
                r["target_id"][:8],
            )
            parts.append(f"- {src_name} --[{r['relationship_type']}]--> {tgt_name}")

    parts.extend(["", "Summary:"])
    return "\n".join(parts)
