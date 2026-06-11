"""Link entities to episode via graph_episode_entities join table.

Runs after entity extraction is complete (or alongside it).  Reads the
extracted entities from the graph backend and links them to this episode
in the ``graph_episode_entities`` join table.

Previously this worker created a Graphiti ``EpisodicNode`` for each
episode.  That pattern is replaced by storing entity–episode links in
PostgreSQL, eliminating the need for a separate graph database.
"""

from __future__ import annotations

import structlog
from sqlalchemy import text

from workers.tasks.base import ENRICHMENT_SYNC_GRAPH, with_retry

logger = structlog.get_logger()


@with_retry(max_retries=3, base_delay_s=2.0)
async def sync_to_graph(
    ctx: object,
    episode_id: str,
    org_id: str,
    user_id: str,
    content: str,
    role: str,
) -> None:
    """Link entities extracted from this episode via graph_episode_entities.

    PostgreSQL is the authoritative store — if this task fails, the episode
    data is not lost and can be retried.

    Flow:
    1. Bootstrap a temporary DB engine
    2. Get the episode row
    3. Search for entities in graph_entities by name/content match
    4. Link matching entities via INSERT INTO graph_episode_entities
    5. Set enrichment_status bit 3

    Args:
        ctx: ARQ worker context (unused, required by ARQ contract).
        episode_id: UUID of the episode to sync.
        org_id: UUID of the owning organization.
        user_id: UUID of the user who authored the episode.
        content: Episode message text (used for entity name matching).
        role: Message role (user/assistant/system/tool).

    Raises:
        RuntimeError: If Graphiti is required but not installed or
            initialised.
    """
    from datetime import datetime, timezone
    from uuid import UUID

    from sqlalchemy import select, update
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import settings
    from core.db import get_async_session
    from models.episode import Episode

    engine = create_async_engine(
        str(settings.DATABASE_URL),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=2,
    )
    session_factory = get_async_session(engine)
    now = datetime.now(timezone.utc)

    try:
        async with session_factory() as db:
            # ── 1. Get the episode ──────────────────────────────────────────
            result = await db.execute(select(Episode).where(Episode.id == episode_id))
            episode = result.scalar_one_or_none()
            if episode is None:
                logger.warning(
                    "sync_to_graph.episode_not_found",
                    episode_id=episode_id,
                )
                return

            # ── 2. Search for matching entities ─────────────────────────────
            # Extract potential entity names from content (simple keyword split)
            # and search the graph_entities table for matches.
            words = set(
                w.strip().rstrip(".,!?:;")
                for w in content.split()
                if len(w.strip()) > 2 and w.strip()[0].isupper()
            )

            words_matched: int = 0
            entities_found_per_word: list[int] = []
            linked = 0
            for word in words:
                if not word:
                    continue
                # Search for entities whose name matches (fuzzy via pg_trgm).
                # Skip merged/deprecated entities so episodes are only linked
                # to active entities.
                entity_result = await db.execute(
                    text(
                        """
                        SELECT id FROM graph_entities
                        WHERE organization_id = :org_id
                          AND is_merged = false
                          AND (name ILIKE :word
                               OR similarity(name, :word) > 0.3)
                        LIMIT 5
                        """
                    ),
                    {"org_id": UUID(org_id), "word": f"%{word}%"},
                )
                entity_rows = entity_result.all()
                if entity_rows:
                    words_matched += 1
                    entities_found_per_word.append(len(entity_rows))
                for entity_row in entity_rows:
                    entity_id = str(entity_row[0])
                    # Link entity to episode via graph_episode_entities
                    await db.execute(
                        text(
                            """
                            INSERT INTO graph_episode_entities
                                (episode_id, entity_id, created_at)
                            VALUES (:episode_id, :entity_id, :created_at)
                            ON CONFLICT (episode_id, entity_id) DO NOTHING
                            """
                        ),
                        {
                            "episode_id": UUID(episode_id),
                            "entity_id": UUID(entity_id),
                            "created_at": now,
                        },
                    )
                    linked += 1

            # ── 3. Update enrichment_status bit 3 ───────────────────────────
            await db.execute(
                update(Episode)
                .where(Episode.id == episode_id)
                .values(
                    enrichment_status=Episode.enrichment_status.op("|")(
                        ENRICHMENT_SYNC_GRAPH
                    ),
                )
            )
            await db.commit()

            logger.info(
                "sync_to_graph.completed",
                episode_id=episode_id,
                entities_linked=linked,
                words_analyzed=len(words),
                words_matched=words_matched,
                avg_entities_per_match=(
                    round(
                        sum(entities_found_per_word) / len(entities_found_per_word), 2
                    )
                    if entities_found_per_word
                    else 0
                ),
            )

    finally:
        await engine.dispose()
