"""ARQ worker for community summarisation — scheduled nightly.

Pipeline:
1. Find orgs with graph data via the ORM
2. Resolve per-org graph backend (surfaces SurrealDB or FalkorDB if configured)
3. Per project within each org:
   a. Fetch all entities + relationships via the graph backend
   b. Build a NetworkX graph
   c. Run Label Propagation community detection
   d. For each community with >= 2 entities, generate an LLM summary
   e. Store community entities + MEMBER_OF edges via the graph backend

All raw SQL has been removed — every graph operation goes through the
``GraphBackend`` ABC.  See Wave 3c of the migration.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from models.organization import Organization
from models.project import Project
from workers.backend import resolve_graph_backend

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.graph_backend.interface import GraphBackend

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

COMMUNITY_MIN_ENTITY_COUNT = 5
"""Minimum entities required for community detection to run for a project."""

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
        Dict with ``status``, ``orgs_processed``, ``orgs_failed``,
        ``communities_created``.
    """
    from core.config import settings
    from core.db import get_async_session

    # Use the shared engine from worker context.
    engine = ctx.get("db_engine") if isinstance(ctx, dict) else None
    if engine is None:
        from core.db import init_db_engine

        engine = init_db_engine(
            str(settings.DATABASE_URL),
            pool_size=5,
            max_overflow=2,
        )
        _own_engine = True
    else:
        _own_engine = False
    session_factory = ctx.get("db_session_factory") if isinstance(ctx, dict) else None
    if session_factory is None:
        session_factory = get_async_session(engine)

    try:
        async with session_factory() as db:
            # Determine which orgs to process
            if org_id:
                org_ids = [UUID(org_id)]
            else:
                # Discover eligible orgs via the Organization table — each
                # org's resolved backend answers authoritatively for entity
                # counts via get_all_entities().  Direct GraphEntity ORM
                # queries are incorrect for non-Postgres backends.
                result = await db.execute(select(Organization.id))
                all_org_ids = [r[0] for r in result.all()]

                org_ids = []
                for oid in all_org_ids:
                    try:
                        backend = await resolve_graph_backend(ctx, oid, db)
                        if backend is None:
                            continue
                        # Entity-count filtering happens inside _process_org
                        # via backend.get_all_entities() — no need to
                        # duplicate it here.
                        org_ids.append(oid)
                    except Exception:
                        logger.error(
                            "community.org_resolution_failed",
                            extra={"org_id": str(oid)},
                            exc_info=True,
                        )
                        continue

            if not org_ids:
                return {"status": "skipped", "reason": "No eligible orgs found"}

            org_errors: list[str] = []
            total_communities = 0
            for current_org_id in org_ids:
                try:
                    communities = await _process_org(ctx, db, current_org_id)
                    total_communities += communities
                except Exception as exc:
                    logger.error(
                        "community.process_org_failed",
                        extra={"org_id": str(current_org_id), "error": str(exc)},
                    )
                    org_errors.append(str(current_org_id))

            if org_errors and len(org_errors) == len(org_ids):
                raise RuntimeError(
                    f"All {len(org_ids)} orgs failed: {', '.join(org_errors)}"
                )

            return {
                "status": "completed" if not org_errors else "partial",
                "orgs_processed": len(org_ids),
                "orgs_failed": len(org_errors),
                "communities_created": total_communities,
            }

    finally:
        if _own_engine:
            await engine.dispose()


async def _process_org(ctx: dict, db: AsyncSession, org_id: UUID) -> int:
    """Run community detection and summarisation for a single org.

    Resolves the per-org graph backend, discovers projects with graph
    data, and processes each project independently.

    Args:
        ctx: ARQ worker context (passed to ``resolve_graph_backend``).
        db: Database session.
        org_id: Organization UUID.

    Returns:
        Number of communities created across all projects.
    """
    from packages.community.algorithms import (
        build_entity_graph,
        detect_communities_label_propagation,
    )

    backend = await resolve_graph_backend(ctx, org_id, db)
    if backend is None:
        logger.warning(
            "community.graph_disabled",
            extra={"org_id": str(org_id)},
        )
        return 0

    # Discover projects for this org that have graph entities
    result = await db.execute(
        select(Project.id).where(
            Project.organization_id == org_id,
            Project.is_archived.is_(False),
        )
    )
    project_ids = [r[0] for r in result.all()]

    if not project_ids:
        logger.info(
            "community.no_projects",
            extra={"org_id": str(org_id)},
        )
        return 0

    total_created = 0
    for project_id in project_ids:
        try:
            # 1. Fetch all entities (non-community) via backend
            entities = await backend.get_all_entities(org_id, project_id)

            if len(entities) < COMMUNITY_MIN_ENTITY_COUNT:
                logger.info(
                    "community.too_few_entities",
                    extra={
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                        "count": len(entities),
                    },
                )
                continue

            # 2. Fetch relationships via backend
            relationships = await backend.get_all_relationships(
                org_id, project_id
            )

            # 3. Build graph and detect communities
            graph = build_entity_graph(entities, relationships)
            communities = detect_communities_label_propagation(graph)

            if not communities:
                logger.info(
                    "community.no_communities_found",
                    extra={
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                    },
                )
                continue

            # 4. Generate summaries and store
            for community_nodes in communities:
                try:
                    await _create_community(
                        ctx=ctx,
                        backend=backend,
                        db=db,
                        org_id=org_id,
                        project_id=project_id,
                        entity_ids=list(community_nodes),
                        all_entities=entities,
                        all_relationships=relationships,
                    )
                    total_created += 1
                except Exception as exc:
                    logger.error(
                        "community.create_failed",
                        extra={
                            "org_id": str(org_id),
                            "project_id": str(project_id),
                            "error": str(exc),
                        },
                    )
        except Exception as exc:
            logger.error(
                "community.project_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "error": str(exc),
                },
            )

    await db.commit()
    logger.info(
        "community.org_completed",
        extra={"org_id": str(org_id), "communities": total_created},
    )
    return total_created


async def _create_community(
    ctx: dict,
    backend: GraphBackend,
    db: AsyncSession,
    org_id: UUID,
    project_id: UUID,
    entity_ids: list[str],
    all_entities: list[dict],
    all_relationships: list[dict],
) -> None:
    """Create a community entity, generate summary, and link members.

    Uses the graph backend for all persistence — no raw SQL.

    Args:
        ctx: ARQ worker context (used for LLM backend resolution).
        backend: Initialised graph backend for this org.
        db: Database session (for org config resolution).
        org_id: Organization UUID.
        project_id: Project UUID.
        entity_ids: UUIDs of entities in this community.
        all_entities: All entities in the project (for building the prompt
            context).
        all_relationships: All relationships in the project (for building
            the prompt context).
    """
    from core.llm import resolve_backend as resolve_llm_backend
    from core.org_config import get_org_config

    # Build entity name map
    entity_map = {e["id"]: e for e in all_entities}
    member_names = [
        entity_map[eid]["name"] for eid in entity_ids if eid in entity_map
    ]
    community_name = (
        f"Community: {', '.join(member_names[:3])}"
        f"{'...' if len(member_names) > 3 else ''}"
    )

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
        bao_client = ctx.get("openbao_client") if isinstance(ctx, dict) else None
        if bao_client is not None:
            org_cfg = await get_org_config(
                org_id, redis=None, bao_client=bao_client
            )
        else:
            from core.config import BootstrapSettings
            from core.openbao import OpenBaoClient

            bootstrap = BootstrapSettings()
            async with OpenBaoClient(
                bootstrap.OPENBAO_ADDR,
                bootstrap.OPENBAO_ROLE_ID,
                bootstrap.OPENBAO_SECRET_ID,
                timeout=10.0,
            ) as _tmp_bao:
                org_cfg = await get_org_config(
                    org_id, redis=None, bao_client=_tmp_bao
                )
        llm = await resolve_llm_backend(org_config=org_cfg.to_llm_config_dict())
        response = await llm.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are an analyst. Output ONLY the summary text, no preamble."
                    ),
                },
                {"role": "user", "content": prompt},
            ]
        )
        summary = response.content.strip()
    except Exception as exc:
        logger.warning(
            "community.llm_summary_failed",
            extra={"error": str(exc)},
        )
        summary = (
            f"Community of {len(member_names)} entities: "
            f"{', '.join(member_names)}"
        )

    # Create community entity via backend
    entity = await backend.create_entity(
        org_id=org_id,
        project_id=project_id,
        name=community_name,
        entity_type="community",
        summary=summary,
    )
    community_id = UUID(entity["id"])

    # Create MEMBER_OF edges in bulk via backend
    member_edges = [
        {
            "source_id": UUID(eid),
            "target_id": community_id,
            "relationship_type": "member_of",
        }
        for eid in entity_ids
    ]
    if member_edges:
        await backend.create_relationship_bulk(
            org_id=org_id,
            project_id=project_id,
            relationships=member_edges,
        )

    logger.info(
        "community.created",
        extra={
            "org_id": str(org_id),
            "project_id": str(project_id),
            "community_id": str(community_id),
            "member_count": len(entity_ids),
            "summary_preview": summary[:80],
        },
    )


def _build_community_prompt(
    entities: list[dict],
    relationships: list[dict],
) -> str:
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
            "- {} ({}): {}".format(
                e.get("name", "?"), e.get("type", "?"),
                e.get("summary", "")[:100],
            )
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
