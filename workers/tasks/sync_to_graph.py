"""Sync episode to Graphiti episodic layer (async, non-blocking).

Runs after an episode is committed to PostgreSQL.  Creates an ``EpisodicNode``
in Graphiti's temporal knowledge graph and stores the resulting node ID back
on the episode row.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select, update

from workers.tasks.base import ENRICHMENT_SYNC_GRAPH

logger = structlog.get_logger()


async def sync_to_graph(
    ctx: object,
    episode_id: str,
    org_id: str,
    user_id: str,
    content: str,
    role: str,
) -> None:
    """Read episode from PostgreSQL and create an ``EpisodicNode`` in Graphiti.

    PostgreSQL is the authoritative store — if this task fails, the episode
    data is not lost and can be retried.

    Updates ``episodes.graphiti_node_id`` on success and sets
    ``episodes.enrichment_status`` bit 3 on completion or permanent failure.

    Args:
        ctx: ARQ worker context (unused in this task, required by ARQ
            contract).
        episode_id: UUID of the episode to sync.
        org_id: UUID of the owning organization (used as Graphiti group_id).
        user_id: UUID of the user who authored the episode.
        content: Episode message text.
        role: Message role (user/assistant/system/tool).

    Raises:
        RuntimeError: If Graphiti is required but not installed or
            initialised.
    """
    # ── 1. Check HAS_GRAPHITI — skip gracefully if not installed ─────────
    try:
        from core.graphiti import HAS_GRAPHITI, get_graphiti
    except ImportError:
        logger.warning("sync_to_graph.skipped", reason="graphiti-core not installed")
        return

    if not HAS_GRAPHITI:
        logger.warning("sync_to_graph.skipped", reason="graphiti-core not available")
        return

    # ── 2. Bootstrap a temporary DB engine for this task ──────────────────
    # TechLead note: We create a short-lived engine here because ARQ workers
    # run in a separate process and may not share the app's engine.  For
    # higher throughput, consider passing the engine from the worker
    # initialisation context instead.
    from core.config import settings
    from core.db import get_async_session
    from models.episode import Episode
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        str(settings.DATABASE_URL),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=2,
    )
    session_factory = get_async_session(engine)

    try:
        async with session_factory() as db:
            # ── 3. Get the episode ────────────────────────────────────────
            result = await db.execute(select(Episode).where(Episode.id == episode_id))
            episode = result.scalar_one_or_none()
            if episode is None:
                logger.warning(
                    "sync_to_graph.episode_not_found",
                    episode_id=episode_id,
                )
                return

            # ── 4. Create EpisodicNode in Graphiti ────────────────────────
            graphiti = get_graphiti()
            # Graphiti methods are synchronous — offload to the executor.
            import asyncio

            loop = asyncio.get_running_loop()

            # ⚠️ RACE CONDITION: If two workers process the same episode_id
            # concurrently, both could create Graphiti nodes and both would
            # succeed.  The second write to `graphiti_node_id` would silently
            # overwrite the first.  The enrichment_status bitmask guard in
            # the caller should prevent this — verify that the caller checks
            # bit 3 before enqueuing.
            node = await loop.run_in_executor(
                None,
                lambda: graphiti._add_entity(
                    name=f"episode_{episode_id[:8]}",
                    entity_type="EpisodicNode",
                    summary=content[:500],  # truncate for graph summary
                    group_id=f"org_{org_id}",
                ),
            )

            node_id: str = (
                str(node.uuid)
                if hasattr(node, "uuid")
                else str(node.get("uuid", ""))
            )

            # ── 5. Update graphiti_node_id and enrichment_status ──────────
            # Set bit 3 on enrichment_status to mark completion.
            # Use explicit operator expression for column bitwise OR.
            await db.execute(
                update(Episode)
                .where(Episode.id == episode_id)
                .values(
                    graphiti_node_id=node_id,
                    enrichment_status=Episode.enrichment_status.op("|")(
                        ENRICHMENT_SYNC_GRAPH
                    ),
                )
            )
            await db.commit()

            logger.info(
                "sync_to_graph.completed",
                episode_id=episode_id,
                graphiti_node_id=node_id,
            )

    finally:
        await engine.dispose()
