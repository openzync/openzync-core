#!/usr/bin/env python3
"""Null stored embeddings after a same-dimension embedding-model swap.

WHY THIS EXISTS
---------------
``episodes.embedding`` and ``facts.embedding`` are both ``VECTOR(768)``.
``nomic-ai/nomic-embed-text-v1.5`` replaced ``snowflake-arctic-embed-m-v1.5``
at the *same* dimension, which makes the swap completely invisible to every
guard the system has:

- ``core.embeddings.validate_embedding_dim`` only checks ``len(vec) == 768``
  — old vectors are 768-wide, so they pass;
- the ``VECTOR(768)`` CHECK constraints — same width, so they pass;
- ``CAST(:embedding AS vector(768))`` — same width, so it cannot reshape;
- the HNSW indexes — they keep serving, just over a stale vector space.

Left alone, every stored vector stays a valid 768-float vector that is
semantically meaningless against the new model. Retrieval degrades to noise
with no error anywhere. Recovery is only possible *before* the new vectors
are written, which is why this script refuses to run without ``--yes``.

WHAT IT DOES (and deliberately does not do)
-------------------------------------------
It only resets state; it never embeds. The backfill is already implemented
and battle-tested in ``workers/tasks/reconcile_enrichment.py``, which runs
as a 5-minute ARQ cron:

- **Episodes** — this script clears ``embedding`` and the
  ``ENRICHMENT_EMBEDDING`` bit (bit 1). ``embed_episode`` skips any episode
  whose bit is already set, so clearing it is what makes the row eligible
  again. Other enrichment bits are left untouched: only the embedding leg
  needs redoing, and re-running the LLM legs would cost money. Reconcile
  then enqueues ``embed_episode`` for those rows.
- **Facts** — this script sets ``embedding = NULL, embedded_at = NULL``.
  Reconcile's fact pass selects on exactly that pair (``embedding IS NULL
  AND embedded_at IS NULL AND invalid_at IS NULL``), which makes every
  fact eligible for re-enqueue as ``embed_fact``. Retracted facts
  (``invalid_at IS NOT NULL``) are skipped by that query and are therefore
  left untouched here too.

Both passes are throttled: reconcile handles 100 rows per tick, so expect
a visible ramp rather than an instant re-embed.

⚠️ The fact re-enqueue path also requires the ARQ worker to be running, and
the episode pass skips episodes in projects that are archived.

Usage:
    python scripts/reset_embeddings_for_remodel.py --dry-run
    python scripts/reset_embeddings_for_remodel.py --yes
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.db import get_async_session, init_db_engine
from workers.tasks.base import ENRICHMENT_EMBEDDING

logger = logging.getLogger("reset_embeddings_for_remodel")


async def _count_rows(db: AsyncSession) -> tuple[int, int]:
    """Return the number of episode and fact rows this run would reset.

    Counts only rows that actually hold a vector — already-NULL rows need
    no work and should not inflate the operator's sense of the blast
    radius.

    Args:
        db: An open async SQLAlchemy session.

    Returns:
        ``(episodes, facts)`` row counts.
    """
    episodes = await db.execute(
        text("SELECT count(*) FROM episodes WHERE embedding IS NOT NULL")  # noqa: S608
    )
    facts = await db.execute(
        text("SELECT count(*) FROM facts WHERE embedding IS NOT NULL")  # noqa: S608
    )
    return episodes.scalar_one(), facts.scalar_one()


async def run(db_url: str, *, dry_run: bool) -> None:
    """Reset embeddings for a model swap, or report what it would reset.

    Args:
        db_url: Async SQLAlchemy DSN (``postgresql+asyncpg://…``).
        dry_run: When True, only report the row counts and write nothing.

    Raises:
        Exception: Any DB error propagates — a partial reset is visible and
            alertable rather than silently swallowed.
    """
    engine = init_db_engine(db_url, pool_size=2, max_overflow=1)
    try:
        async with get_async_session(engine)() as db:
            # Cross-org maintenance: the RLS policies read
            # NULLIF(current_setting('app.org_id', true), ''), so an unset
            # context matches no rows.  Same bypass the reconcile cron uses.
            await db.execute(text("SELECT set_config('app.bypass_rls', 'true', true)"))

            episodes, facts = await _count_rows(db)
            if dry_run:
                logger.info(
                    "reset_embeddings.dry_run episodes=%d facts=%d — no rows written",
                    episodes,
                    facts,
                )
                return

            # S608: `~{ENRICHMENT_EMBEDDING}` interpolates an int constant
            # (2), never user input. Clears bit 1 only — other enrichment
            # bits (entities, facts, classification) survive.
            await db.execute(
                text(
                    "UPDATE episodes SET embedding = NULL, "  # noqa: S608
                    f"enrichment_status = enrichment_status & ~{ENRICHMENT_EMBEDDING} "
                    "WHERE embedding IS NOT NULL"
                )
            )
            # embedded_at = NULL returns the row to reconcile's "never
            # attempted" state; leaving it set would mark it retired and
            # exclude it from repair forever.
            await db.execute(
                text(
                    "UPDATE facts SET embedding = NULL, embedded_at = NULL "
                    "WHERE embedding IS NOT NULL"
                )
            )
            await db.commit()

            logger.info(
                "reset_embeddings.completed episodes=%d facts=%d — "
                "reconcile_enrichment will re-enqueue these over the next "
                "ticks (100 rows/tick); confirm the ARQ worker is running",
                episodes,
                facts,
            )
    finally:
        await engine.dispose()


def main() -> None:
    """Parse args and run the reset."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report row counts without writing anything.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Required to actually write. Without it this is an implicit dry run, "
            "so an operator cannot reset every vector by accident."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=str(settings.DATABASE_URL),
        help="Async SQLAlchemy DSN (default: the configured DATABASE_URL).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # No --yes means no writes. A destructive bulk null with no confirmation
    # is exactly the kind of thing that gets run against production by
    # muscle memory.
    if not args.yes:
        if not args.dry_run:
            logger.warning(
                "reset_embeddings.no_confirm — pass --yes to write (dry run instead)"
            )
        asyncio.run(run(args.database_url, dry_run=True))
        return

    asyncio.run(run(args.database_url, dry_run=False))


if __name__ == "__main__":
    main()
