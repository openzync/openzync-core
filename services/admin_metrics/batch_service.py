"""Admin metrics batch fan-out — runs all 12 predefined queries concurrently.

DB legs each run on a sibling session from
``request.app.state.db_session_factory`` (an ``AsyncSession`` is not
concurrency-safe, so sharing one across ``asyncio.gather`` corrupts
results). Prometheus legs share the concurrency-safe ``httpx`` client.
Per-query failures stay HTTP 200 in the ``errors`` envelope;
``CancelledError`` is control flow and always re-raised.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

import httpx
import structlog
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from core.graph_backend import resolve_graph_backend_fail_soft
from core.rls import release_tenant_context, set_tenant_context
from services.admin_metrics.db_queries_service import DbMetricsQueriesService
from services.admin_metrics.prom_queries_service import PromMetricsQueriesService

if TYPE_CHECKING:
    from packages.graph_backend.interface import GraphBackend
    from schemas.organization_config import OrgConfigBase

logger = structlog.get_logger(__name__)

_DB_TIMEOUT = 5.0
_PROM_TIMEOUT = 8.0
_OVERALL_TIMEOUT = 15.0

PROM_QUERY_NAMES = frozenset(
    {"error_rate_by_day", "latency_percentiles", "context_retrieval_rate"}
)


class AdminMetricsBatchService:
    """Fans out the 12 predefined queries with a partial-OK envelope."""

    def __init__(self, db: AsyncSession, prom_client: httpx.AsyncClient) -> None:
        self._db = db
        self._prom = PromMetricsQueriesService(prom_client)

    @staticmethod
    async def _guarded(name: str, coro: Awaitable[dict]) -> dict:
        """Await a leg with its per-domain budget (5s DB / 8s Prometheus)."""
        timeout = _PROM_TIMEOUT if name in PROM_QUERY_NAMES else _DB_TIMEOUT
        return await asyncio.wait_for(coro, timeout=timeout)

    @staticmethod
    def _classify_batch_error(query_name: str, exc: BaseException) -> dict:
        """Map a batch leg failure to the ``errors`` envelope entry."""
        if isinstance(exc, HTTPException):
            code = "bad_gateway" if exc.status_code == 502 else "handler_error"
            message = str(exc.detail)
        elif isinstance(exc, httpx.HTTPError):
            # raise_for_status() in _prom_instant/_prom_range surfaces transport
            # and status errors as httpx.HTTPError — Prometheus is down, not a
            # handler bug, so report bad_gateway like the 502 HTTPException path.
            code = "bad_gateway"
            message = str(exc) or repr(exc)
        elif isinstance(exc, TimeoutError):
            code = "timeout"
            message = f"Query '{query_name}' timed out"
        else:
            code = "handler_error"
            message = str(exc) or repr(exc)
        logger.warning(
            "admin_metrics.batch_query_failed",
            query=query_name,
            code=code,
            error=message,
        )
        return {"query": query_name, "code": code, "message": message}

    async def _invoke_concurrent(
        self,
        factory: async_sessionmaker[AsyncSession],
        org_id: UUID,
        name: str,
        call: Callable[[DbMetricsQueriesService], Awaitable[dict]],
    ) -> dict:
        """Run one DB leg on a sibling tenant-bound session.

        Args:
            factory: ``app.state.db_session_factory`` — one session per leg.
            org_id: Tenant bound on the sibling session (RLS).
            name: Query name (timeout budget + log context).
            call: Takes the leg-scoped service, returns the leg result.
        """
        async with factory() as leg_db:
            await set_tenant_context(leg_db, org_id)
            try:
                return await self._guarded(name, call(DbMetricsQueriesService(leg_db)))
            except Exception:
                await leg_db.rollback()
                raise
            finally:
                await release_tenant_context(leg_db)

    async def _run_legs(
        self,
        request: Request,
        org_id: UUID,
        backend: GraphBackend | None,
        db_calls: dict[str, Callable[[DbMetricsQueriesService], Awaitable[dict]]],
        prom_calls: dict[str, Callable[[], Awaitable[dict]]],
        names: list[str],
    ) -> list:
        """Run every leg; returns outcomes aligned with ``names``.

        With a session factory each DB leg gets a sibling session and all
        12 legs gather. Without one (no factory on app state) the request
        session serves DB legs sequentially — an ``AsyncSession`` shared
        across ``gather`` is a race, not a speedup — and only the
        Prometheus legs gather.
        """
        factory = getattr(request.app.state, "db_session_factory", None)
        if factory is None:
            # ⚠️ Sequential DB path: the shared session is not
            # concurrency-safe. Per-leg CancelledError re-raises (control
            # flow); true failures become per-query outcomes below.
            logger.warning("admin_metrics.batch_no_session_factory_sequential_db")
            by_name: dict[str, BaseException | dict] = {}
            for name in names:
                if name not in db_calls:
                    continue
                try:
                    leg = DbMetricsQueriesService(self._db)
                    by_name[name] = await self._guarded(name, db_calls[name](leg))
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    by_name[name] = exc
            prom_names = [n for n in names if n in prom_calls]
            prom_outcomes = await asyncio.gather(
                *(self._guarded(n, prom_calls[n]()) for n in prom_names),
                return_exceptions=True,
            )
            by_name.update(dict(zip(prom_names, prom_outcomes, strict=True)))
            return [by_name[n] for n in names]
        return await asyncio.gather(
            *(
                self._guarded(name, prom_calls[name]())
                if name in prom_calls
                else self._invoke_concurrent(factory, org_id, name, db_calls[name])
                for name in names
            ),
            return_exceptions=True,
        )

    async def run_batch(
        self,
        request: Request,
        org_config: OrgConfigBase,
        org_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
        include_archived: bool,
    ) -> dict:
        """Run every predefined query concurrently; partial failures stay HTTP 200.

        Returns:
            ``{"results": {name: query-dict}, "errors": [...], "meta": {...}}``
            where each ``results`` value has ``query/org_scoped/columns/rows/
            total/parameters`` and each ``errors`` entry has
            ``query/code/message``.
        """
        started = time.perf_counter()
        backend = await resolve_graph_backend_fail_soft(
            request, org_config, org_id, self._db, "admin_metrics"
        )

        db_calls: dict[str, Callable[[DbMetricsQueriesService], Awaitable[dict]]] = {
            "episodes_per_day": lambda svc: svc.episodes_per_day(
                org_id, start, end, limit, project_id
            ),
            "messages_per_day": lambda svc: svc.messages_per_day(
                org_id, start, end, limit, project_id
            ),
            "activity_per_day": lambda svc: svc.activity_per_day(
                org_id, start, end, limit, project_id
            ),
            "entities_per_day": lambda svc: svc.entities_per_day(
                org_id, start, end, limit, project_id, backend
            ),
            "facts_per_day": lambda svc: svc.facts_per_day(
                org_id, start, end, limit, project_id
            ),
            "enrichment_progress": lambda svc: svc.enrichment_progress(
                org_id, start, end, limit, project_id
            ),
            "top_projects_by_episodes": lambda svc: svc.top_projects_by_episodes(
                org_id, start, end, limit, project_id, include_archived
            ),
            "top_users_by_messages": lambda svc: svc.top_users_by_messages(
                org_id, start, end, limit, project_id
            ),
            "queue_depth_over_time": lambda svc: svc.queue_depth_over_time(
                org_id, start, end, limit, project_id
            ),
        }
        prom_calls: dict[str, Callable[[], Awaitable[dict]]] = {
            "error_rate_by_day": lambda: self._prom.error_rate_by_day(
                org_id, start, end, limit, project_id
            ),
            "latency_percentiles": lambda: self._prom.latency_percentiles(
                org_id, start, end, limit, project_id
            ),
            "context_retrieval_rate": lambda: self._prom.context_retrieval_rate(
                org_id, start, end, limit, project_id
            ),
        }
        names = [
            "episodes_per_day",
            "messages_per_day",
            "activity_per_day",
            "entities_per_day",
            "facts_per_day",
            "enrichment_progress",
            "top_projects_by_episodes",
            "top_users_by_messages",
            "error_rate_by_day",
            "latency_percentiles",
            "queue_depth_over_time",
            "context_retrieval_rate",
        ]

        try:
            # Concurrent ceiling is ~8s (slowest Prom query), so the 15s overall
            # budget below rarely fires — it bounds pathological scheduling, not
            # the common case.
            outcomes = await asyncio.wait_for(
                self._run_legs(request, org_id, backend, db_calls, prom_calls, names),
                timeout=_OVERALL_TIMEOUT,
            )
        except TimeoutError as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            return {
                "results": {},
                "errors": [self._classify_batch_error(n, exc) for n in names],
                "meta": {
                    "limit": limit,
                    "duration_ms": duration_ms,
                    "partial": True,
                },
            }

        results: dict[str, dict] = {}
        errors: list[dict] = []
        for name, outcome in zip(names, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                # CancelledError is control flow, not a per-query failure.
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                errors.append(self._classify_batch_error(name, outcome))
            else:
                results[name] = outcome

        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            "results": results,
            "errors": errors,
            "meta": {
                "limit": limit,
                "duration_ms": duration_ms,
                "partial": bool(errors),
            },
        }
