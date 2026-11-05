"""Leg dispatch — sibling-session plumbing plus per-leg entry points."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.exceptions import SearchLegFailedError

logger = logging.getLogger(__name__)


class LegDispatchMixin:
    """Concurrent-leg plumbing and the five retrieval-leg entry points."""

    _db: AsyncSession
    _org_id: UUID
    _session_factory: async_sessionmaker[AsyncSession] | None

    def _scoped(self, db: AsyncSession) -> LegDispatchMixin:
        """Return a retriever with identical config bound to another session.

        Used to run one retrieval leg on a sibling session — a single
        ``AsyncSession`` allows only one in-flight operation, so
        concurrent legs cannot share ``self._db``.

        Args:
            db: The sibling session the leg runs on.

        Returns:
            A new retriever sharing org, backends, reranker
            settings, org config, reranker, and Redis, but no session
            factory (no nesting — the parent fans out, legs never re-fan).
        """
        return type(self)(
            db,
            self._org_id,
            redis=self._redis,
            graph_backends=self._graph_backends,
            org_config=self._org_config,
            reranker=self._reranker,
            session_factory=None,
        )

    async def _run_leg(
        self,
        leg_name: str,
        project_id: UUID,
        run: Callable[[LegDispatchMixin], Awaitable[list[dict[str, Any]]]],
    ) -> list[dict[str, Any]]:
        """Run one retrieval leg, failing loud as ``SearchLegFailedError``.

        With a session factory the leg runs on a sibling session bound to
        this org's RLS tenant; a leg failure rolls back only that session.
        Without a factory the leg runs sequentially on the shared session.
        ``SearchLegFailedError`` from the leg itself passes through
        unwrapped; any other exception is logged with leg context and
        wrapped. ``CancelledError`` is never swallowed.

        Args:
            leg_name: Leg identifier for logs and the raised error.
            project_id: Scoped project UUID (log context).
            run: Takes the retriever bound to the leg's session and returns
                that leg's result list.

        Returns:
            The leg's result list.

        Raises:
            SearchLegFailedError: If the leg fails.
        """
        extra = {
            "leg": leg_name,
            "org_id": str(self._org_id),
            "project_id": str(project_id),
        }
        if self._session_factory is None:
            logger.warning("hybrid_retriever.sequential_leg", extra=extra)
            try:
                return await run(self)
            except SearchLegFailedError:
                raise
            except Exception as exc:
                logger.error(
                    f"hybrid_retriever.{leg_name}_failed", extra=extra, exc_info=True
                )
                await self._db.rollback()
                raise SearchLegFailedError(
                    leg_name=leg_name, original_error=str(exc)
                ) from exc
        async with self._session_factory() as leg_db:
            from services import hybrid_retriever as _hr  # via facade (patchable)
            await _hr.set_tenant_context(leg_db, self._org_id)
            try:
                return await run(self._scoped(leg_db))
            except SearchLegFailedError:
                await leg_db.rollback()
                raise
            except Exception as exc:
                logger.error(
                    f"hybrid_retriever.{leg_name}_failed", extra=extra, exc_info=True
                )
                await leg_db.rollback()
                raise SearchLegFailedError(
                    leg_name=leg_name, original_error=str(exc)
                ) from exc
            finally:
                await _hr.release_tenant_context(leg_db)

    async def _leg_episode_vector(
        self,
        embedding: list[float] | None,
        project_id: UUID,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Episode vector leg (no-op when the embedding failed)."""
        if embedding is None:
            return []
        return await self._run_leg(
            "episode_vector",
            project_id,
            lambda r: r._vector_search_episodes(
                embedding, project_id, limit
            ),
        )

    async def _leg_fact_vector(
        self,
        embedding: list[float] | None,
        project_id: UUID,
        limit: int,
        query_time: datetime | None,
    ) -> list[dict[str, Any]]:
        """Fact vector leg (no-op when the embedding failed)."""
        if embedding is None:
            return []
        as_of_kwargs: dict[str, datetime] = (
            {} if query_time is None else {"query_time": query_time}
        )
        return await self._run_leg(
            "fact_vector",
            project_id,
            lambda r: r._vector_search_facts(
                embedding, project_id, limit, **as_of_kwargs
            ),
        )

    async def _leg_episode_bm25(
        self, query: str, project_id: UUID, limit: int
    ) -> list[dict[str, Any]]:
        """Episode BM25 leg."""
        return await self._run_leg(
            "episode_bm25",
            project_id,
            lambda r: r._bm25_search_episodes(
                query, project_id, limit
            ),
        )

    async def _leg_fact_bm25(
        self,
        query: str,
        project_id: UUID,
        limit: int,
        query_time: datetime | None,
    ) -> list[dict[str, Any]]:
        """Fact BM25 leg."""
        as_of_kwargs: dict[str, datetime] = (
            {} if query_time is None else {"query_time": query_time}
        )
        return await self._run_leg(
            "fact_bm25",
            project_id,
            lambda r: r._bm25_search_facts(
                query, project_id, limit, **as_of_kwargs
            ),
        )

    async def _leg_graph_bfs(
        self, query: str, project_id: UUID, query_time: datetime | None
    ) -> list[dict[str, Any]]:
        """Graph-BFS leg."""
        return await self._run_leg(
            "graph_bfs",
            project_id,
            lambda r: r._graph_bfs_search(
                query, project_id, query_time=query_time
            ),
        )
