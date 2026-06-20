"""Poll for enrichment completion across episodes in a session.

The OpenZep enrichment pipeline is asynchronous — messages are accepted
immediately (HTTP 202), then ARQ workers process entity extraction,
fact extraction, embedding, graph sync, classification, and structured
extraction in the background.  This waiter checks the bitmask and
blocks until all bits are set (or a timeout is reached).

Bitmask reference (from ``workers/tasks/base.py``):
    bit 0 (1)  — entity extraction
    bit 1 (2)  — embedding generation
    bit 2 (4)  — fact extraction
    bit 3 (8)  — graph sync
    bit 4 (16) — dialog classification
    bit 5 (32) — structured extraction
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.episode import Episode
from models.session import Session
from workers.tasks.base import (
    ENRICHMENT_ENTITIES,
    ENRICHMENT_EMBEDDING,
    ENRICHMENT_FACTS,
    ENRICHMENT_SYNC_GRAPH,
    ENRICHMENT_CLASSIFICATION,
    ENRICHMENT_STRUCTURED_EXTRACTION,
)

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

FULL_ENRICHMENT_MASK: int = (
    ENRICHMENT_ENTITIES
    | ENRICHMENT_EMBEDDING
    | ENRICHMENT_FACTS
    | ENRICHMENT_SYNC_GRAPH
    | ENRICHMENT_CLASSIFICATION
    | ENRICHMENT_STRUCTURED_EXTRACTION
)
"""All 6 enrichment bits set."""

POLL_INTERVAL_S: float = 1.0
"""Delay between consecutive enrichment status checks."""

DEFAULT_TIMEOUT_S: float = 300.0
"""Maximum time to wait for enrichment to complete (5 minutes)."""


# ── Public API ─────────────────────────────────────────────────────────────────


async def wait_for_session_enrichment(
    session_id: str,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    poll_interval_s: float = POLL_INTERVAL_S,
) -> dict[str, Any]:
    """Block until *all* episodes in a session are fully enriched.

    Args:
        session_id: UUID of the session to monitor.
        db_session_factory: An ``async_sessionmaker`` bound to the OpenZep DB.
        timeout_s: Maximum wall-clock time to wait.
        poll_interval_s: Delay between status polls.

    Returns:
        A dict with keys:
            - ``elapsed_s``: total wall-clock seconds waited.
            - ``episode_count``: number of episodes in the session.
            - ``fully_enriched``: ``True`` only if all episodes completed.

    Raises:
        TimeoutError: If enrichment hasn't finished within ``timeout_s``.
    """
    start = asyncio.get_event_loop().time()

    while True:
        elapsed = asyncio.get_event_loop().time() - start
        if elapsed > timeout_s:
            # Log which episodes are still missing bits
            incomplete = await _fetch_incomplete_episodes(session_id, db_session_factory)
            logger.warning(
                "enrichment.timeout",
                extra={
                    "session_id": session_id,
                    "elapsed_s": round(elapsed, 1),
                    "incomplete_count": len(incomplete),
                    "incomplete": [str(e.id) for e in incomplete[:10]],
                },
            )
            raise TimeoutError(
                f"Enrichment not complete after {timeout_s}s for session {session_id}. "
                f"{len(incomplete)} episodes still incomplete."
            )

        total, enriched = await _count_enriched(session_id, db_session_factory)
        if total == 0:
            logger.debug("enrichment.no_episodes", extra={"session_id": session_id})
            return {
                "elapsed_s": round(elapsed, 1),
                "episode_count": 0,
                "fully_enriched": True,
            }

        if total == enriched:
            logger.info(
                "enrichment.complete",
                extra={
                    "session_id": session_id,
                    "elapsed_s": round(elapsed, 1),
                    "episode_count": total,
                },
            )
            return {
                "elapsed_s": round(elapsed, 1),
                "episode_count": total,
                "fully_enriched": True,
            }

        logger.debug(
            "enrichment.waiting",
            extra={
                "session_id": session_id,
                "enriched": enriched,
                "total": total,
                "elapsed_s": round(elapsed, 1),
            },
        )
        await asyncio.sleep(poll_interval_s)


async def wait_for_all_sessions(
    session_ids: list[str],
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    poll_interval_s: float = POLL_INTERVAL_S,
    concurrency: int = 10,
) -> list[dict[str, Any]]:
    """Wait for enrichment across *many* sessions concurrently.

    Args:
        session_ids: UUIDs of sessions to monitor.
        db_session_factory: An ``async_sessionmaker`` bound to the OpenZep DB.
        timeout_s: Per-session timeout.
        poll_interval_s: Delay between status polls per session.
        concurrency: Maximum number of concurrent polling tasks.

    Returns:
        List of result dicts (one per session), in the same order as
        ``session_ids``.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _wait_one(sid: str) -> dict[str, Any]:
        async with sem:
            try:
                return await wait_for_session_enrichment(
                    sid,
                    db_session_factory,
                    timeout_s=timeout_s,
                    poll_interval_s=poll_interval_s,
                )
            except TimeoutError:
                return {
                    "session_id": sid,
                    "elapsed_s": timeout_s,
                    "episode_count": -1,
                    "fully_enriched": False,
                    "error": "timeout",
                }

    tasks = [_wait_one(sid) for sid in session_ids]
    return await asyncio.gather(*tasks)


# ── Internal helpers ───────────────────────────────────────────────────────────


async def _count_enriched(
    session_external_id: str,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int]:
    """Return ``(total_episodes, fully_enriched_count)`` for a session.

    The session is identified by its *external* ID (caller-defined string);
    this function joins through the ``sessions`` table to resolve the
    internal UUID foreign key on ``episodes``.

    Uses raw SQL for the bitmask check because SQLAlchemy's ORM expression
    for bitwise ``&`` + comparison inside ``func.cast`` produces incorrect
    Python-evaluated results rather than SQL expressions.
    """
    async with db_session_factory() as session:
        result = await session.execute(
            text(
                """
                SELECT
                    COUNT(e.id) AS total,
                    COALESCE(SUM(
                        CASE
                            WHEN (e.enrichment_status & :full_mask) = :full_mask THEN 1
                            ELSE 0
                        END
                    ), 0) AS enriched
                FROM episodes e
                JOIN sessions s ON e.session_id = s.id
                WHERE s.external_id = :session_ext_id
                """
            ),
            {
                "full_mask": FULL_ENRICHMENT_MASK,
                "session_ext_id": session_external_id,
            },
        )
        row = result.one()
        return int(row.total), int(row.enriched)


async def _fetch_incomplete_episodes(
    session_external_id: str,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> list[Episode]:
    """Return episodes whose enrichment mask is incomplete.

    Resolves the session via ``Session.external_id`` join.
    Uses raw SQL for the bitmask check.
    """
    async with db_session_factory() as session:
        result = await session.execute(
            text(
                """
                SELECT e.*
                FROM episodes e
                JOIN sessions s ON e.session_id = s.id
                WHERE s.external_id = :session_ext_id
                  AND (e.enrichment_status & :full_mask) != :full_mask
                """
            ),
            {
                "full_mask": FULL_ENRICHMENT_MASK,
                "session_ext_id": session_external_id,
            },
        )
        rows = result.mappings().all()
        return [Episode(**dict(row)) for row in rows]
