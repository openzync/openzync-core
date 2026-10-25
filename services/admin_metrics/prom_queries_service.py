"""Prometheus-backed admin metrics batch queries — no DB access.

One query per method over the shared ``httpx.AsyncClient`` singleton
(the ``httpx`` client is concurrency-safe, so these legs gather freely).
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from uuid import UUID

import httpx
from fastapi import HTTPException

from core.config import get_settings
from services.admin_metrics.shared import build_result


class PromMetricsQueriesService:
    """Runs the Prometheus-backed predefined metric queries."""

    def __init__(self, prom_client: httpx.AsyncClient) -> None:
        self._prom_client = prom_client

    async def _prom_instant(self, promql: str) -> float:
        """Run a PromQL instant query and return the scalar value."""
        base_url = get_settings().PROMETHEUS_URL.rstrip("/")
        resp = await self._prom_client.get(
            f"{base_url}/api/v1/query", params={"query": promql}, timeout=5.0
        )
        resp.raise_for_status()
        data = resp.json()
        if data["status"] != "success":
            raise HTTPException(
                status_code=502,
                detail=f"Prometheus error: {data.get('error', '')}",
            )
        results = data["data"]["result"]
        if not results:
            return 0.0
        value = float(results[0]["value"][1])
        # histogram_quantile yields "NaN" on sparse/fresh buckets — not JSON compliant.
        return value if math.isfinite(value) else 0.0

    @staticmethod
    def _prom_step(start: datetime, end: datetime) -> str:
        """Range step capped at ~180 points: ``max(1h, window/180)``."""
        window_hours = (end - start).total_seconds() / 3600
        hours = max(1, math.ceil(window_hours / 180))
        return f"{hours}h"

    async def _prom_range(
        self, promql: str, start: datetime, end: datetime
    ) -> list[list]:
        """Run a PromQL range query and return rows as [[timestamp, value]]."""
        base_url = get_settings().PROMETHEUS_URL.rstrip("/")
        resp = await self._prom_client.get(
            f"{base_url}/api/v1/query_range",
            params={
                "query": promql,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "step": self._prom_step(start, end),
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data["status"] != "success":
            raise HTTPException(
                status_code=502,
                detail=f"Prometheus error: {data.get('error', '')}",
            )
        results = data["data"]["result"]
        if not results:
            return []
        rows: list[list] = []
        for v in results[0].get("values", []):
            point = float(v[1])
            # Prometheus returns Unix-epoch floats; frontend parses with
            # new Date(...), which needs ISO-8601 strings.
            ts = datetime.fromtimestamp(float(v[0]), tz=UTC).isoformat()
            rows.append([ts, point if math.isfinite(point) else 0.0])
        return rows

    async def error_rate_by_day(
        self,
        org_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
    ) -> dict:
        """Daily 5xx rate for the org (Prometheus has no project scope)."""
        # Prometheus has no project scope — project_id is explicitly ignored.
        _ = (project_id, limit)
        promql = (
            'sum(increase(openzync_http_requests_total{status="5xx",'
            f'org_id="{org_id}"}}[1d]))'
        )
        rows = await self._prom_range(promql, start, end)
        params = {"from": start.isoformat(), "to": end.isoformat()}
        return build_result(
            "error_rate_by_day", True, ["timestamp", "value"], rows, params
        )

    async def latency_percentiles(
        self,
        org_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
    ) -> dict:
        """p50/p95/p99 latency snapshot (window and project ignored)."""
        # Instant snapshot — the [start, end) window is explicitly ignored, and
        # Prometheus has no project scope so project_id is ignored too.
        _ = (start, end, project_id, limit)
        # Order preserved: overall → context → graph, p50 → p95 → p99 —
        # rows follow this order.
        queries = {
            f"{prefix}_p{q}": (
                f"histogram_quantile({v}, sum(rate({metric}"
                f'{{org_id="{org_id}"}}[5m])) by (le)) * 1000'
            )
            for prefix, metric in (
                ("overall", "openzync_http_request_duration_seconds_bucket"),
                ("context", "openzync_context_latency_seconds_bucket"),
                ("graph", "openzync_graph_search_latency_seconds_bucket"),
            )
            for q, v in (("p50", "0.50"), ("p95", "0.95"), ("p99", "0.99"))
        }
        # httpx.AsyncClient is concurrency-safe — one gather, one RTT.
        values = await asyncio.gather(
            *(self._prom_instant(promql) for promql in queries.values())
        )
        rows = [
            [name, round(val, 1)]
            for name, val in zip(queries.keys(), values, strict=True)
        ]
        return build_result(
            "latency_percentiles", True, ["metric", "value_ms"], rows, {}
        )

    async def context_retrieval_rate(
        self,
        org_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        project_id: UUID | None,
    ) -> dict:
        """Context retrieval rate time-series (no project scope in Prom)."""
        # Prometheus has no project scope — project_id is explicitly ignored.
        _ = (project_id, limit)
        promql = (
            "sum(rate(openzync_context_latency_seconds_count{"
            f'org_id="{org_id}"}}[5m]))'
        )
        rows = await self._prom_range(promql, start, end)
        params = {"from": start.isoformat(), "to": end.isoformat()}
        return build_result(
            "context_retrieval_rate", True, ["timestamp", "rate"], rows, params
        )
