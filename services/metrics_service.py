"""Metrics service — queries Prometheus for admin dashboard metrics.

Thin wrapper around the Prometheus HTTP API that runs multiple PromQL
queries concurrently and returns a frontend-friendly JSON shape.

If Prometheus is unreachable or any query fails, ``MetricsUnavailableError``
is raised — the admin dashboard will display an error state rather than
silently showing zeroed-out metrics.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from core.exceptions import MetricsUnavailableError
from schemas.admin_metrics import (
    LatencyPercentiles,
    MetricsSummaryResponse,
    QueueDepth,
)

logger = logging.getLogger(__name__)

# ── PromQL query definitions ──────────────────────────────────────────────────
# Each query is a (name, PromQL) pair.  Names must match keys in the
# response builder below.

LATENCY_QUERIES: list[tuple[str, str]] = [
    (
        "overall_p50",
        'histogram_quantile(0.50, sum(rate(openzync_http_request_duration_seconds_bucket[5m])) by (le)) * 1000',
    ),
    (
        "overall_p95",
        'histogram_quantile(0.95, sum(rate(openzync_http_request_duration_seconds_bucket[5m])) by (le)) * 1000',
    ),
    (
        "overall_p99",
        'histogram_quantile(0.99, sum(rate(openzync_http_request_duration_seconds_bucket[5m])) by (le)) * 1000',
    ),
    (
        "context_p50",
        'histogram_quantile(0.50, sum(rate(openzync_context_latency_seconds_bucket[5m])) by (le)) * 1000',
    ),
    (
        "context_p95",
        'histogram_quantile(0.95, sum(rate(openzync_context_latency_seconds_bucket[5m])) by (le)) * 1000',
    ),
    (
        "context_p99",
        'histogram_quantile(0.99, sum(rate(openzync_context_latency_seconds_bucket[5m])) by (le)) * 1000',
    ),
    (
        "graph_search_p50",
        'histogram_quantile(0.50, sum(rate(openzync_graph_search_latency_seconds_bucket[5m])) by (le)) * 1000',
    ),
    (
        "graph_search_p95",
        'histogram_quantile(0.95, sum(rate(openzync_graph_search_latency_seconds_bucket[5m])) by (le)) * 1000',
    ),
    (
        "graph_search_p99",
        'histogram_quantile(0.99, sum(rate(openzync_graph_search_latency_seconds_bucket[5m])) by (le)) * 1000',
    ),
]

RATE_QUERIES: list[tuple[str, str]] = [
    ("rate_2xx", 'sum(rate(openzync_http_requests_total{status="2xx"}[5m]))'),
    ("rate_4xx", 'sum(rate(openzync_http_requests_total{status="4xx"}[5m]))'),
    ("rate_5xx", 'sum(rate(openzync_http_requests_total{status="5xx"}[5m]))'),
    (
        "error_rate_pct",
        '(sum(rate(openzync_http_requests_total{status="5xx"}[5m])) / (sum(rate(openzync_http_requests_total[5m])) or vector(1))) * 100',
    ),
]

COUNTER_QUERIES: list[tuple[str, str]] = [
    ("total_requests", "sum(openzync_http_requests_total)"),
    ("active_requests", "sum(openzync_http_requests_in_progress)"),
]

QUEUE_QUERIES: list[tuple[str, str]] = [
    ("queue_high", 'openzync_worker_queue_depth{queue_name="high"}'),
    ("queue_low", 'openzync_worker_queue_depth{queue_name="low"}'),
]

ALL_QUERIES = LATENCY_QUERIES + RATE_QUERIES + COUNTER_QUERIES + QUEUE_QUERIES


class MetricsService:
    """Aggregate metrics from Prometheus for the admin dashboard."""

    def __init__(self, prometheus_url: str) -> None:
        self._base_url = prometheus_url.rstrip("/")

    async def get_summary(self) -> MetricsSummaryResponse:
        """Run all PromQL queries and assemble the response.

        Returns:
            A fully populated ``MetricsSummaryResponse``.

        Raises:
            MetricsUnavailableError: If Prometheus is unreachable or any
                query fails.
        """
        results: dict[str, float] = {}

        async def _query(name: str, promql: str) -> tuple[str, float]:
            val = await self._fetch_value(promql)
            return name, val

        tasks = [_query(name, promql) for name, promql in ALL_QUERIES]
        completed = await asyncio.gather(*tasks, return_exceptions=True)

        for item in completed:
            if isinstance(item, Exception):
                logger.error("metrics.prometheus_query_failed", exc_info=True)
                raise MetricsUnavailableError(
                    "Prometheus query failed."
                ) from item
            name, val = item
            results[name] = val

        # Verify Prometheus is reachable
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                resp = await client.get(f"{self._base_url}/-/ready")
                if resp.status_code != 200:
                    raise MetricsUnavailableError(
                        f"Prometheus readiness check returned {resp.status_code}."
                    )
        except httpx.RequestError as exc:
            logger.error("metrics.prometheus_unreachable", exc_info=True)
            raise MetricsUnavailableError(
                "Prometheus is unreachable."
            ) from exc

        return self._build_response(results)

    async def _fetch_value(self, promql: str) -> float:
        """Execute a PromQL instant query and return the scalar value."""
        async with httpx.AsyncClient(timeout=2) as client:
            resp = await client.get(
                f"{self._base_url}/api/v1/query",
                params={"query": promql},
            )
            resp.raise_for_status()
            data = resp.json()

        if data["status"] != "success":
            logger.error(
                "metrics.prometheus_api_error",
                extra={"error": data.get("error", "")},
            )
            raise MetricsUnavailableError(
                f"Prometheus API error: {data.get('error', '')}"
            )

        results = data["data"]["result"]
        if not results:
            return 0.0

        # Scalar or vector result
        try:
            return float(results[0]["value"][1])
        except (KeyError, IndexError, ValueError) as exc:
            logger.error("metrics.unexpected_response_format", exc_info=True)
            raise MetricsUnavailableError(
                "Unexpected Prometheus response format."
            ) from exc

    def _build_response(
        self, results: dict[str, float]
    ) -> MetricsSummaryResponse:
        """Map raw PromQL results into the response model."""

        # Queue depth — may not exist (worker not running)
        qd = None
        if "queue_high" in results or "queue_low" in results:
            qd = QueueDepth(
                high=int(results.get("queue_high", 0)),
                low=int(results.get("queue_low", 0)),
            )

        return MetricsSummaryResponse(
            request_rate={
                "2xx": round(results.get("rate_2xx", 0.0), 3),
                "4xx": round(results.get("rate_4xx", 0.0), 3),
                "5xx": round(results.get("rate_5xx", 0.0), 3),
            },
            error_rate_pct=round(results.get("error_rate_pct", 0.0), 2),
            overall_latency_ms=LatencyPercentiles(
                p50=round(results.get("overall_p50", 0.0), 1),
                p95=round(results.get("overall_p95", 0.0), 1),
                p99=round(results.get("overall_p99", 0.0), 1),
            ),
            context_latency_ms=LatencyPercentiles(
                p50=round(results.get("context_p50", 0.0), 1),
                p95=round(results.get("context_p95", 0.0), 1),
                p99=round(results.get("context_p99", 0.0), 1),
            ),
            graph_search_latency_ms=LatencyPercentiles(
                p50=round(results.get("graph_search_p50", 0.0), 1),
                p95=round(results.get("graph_search_p95", 0.0), 1),
                p99=round(results.get("graph_search_p99", 0.0), 1),
            ),
            total_requests=int(results.get("total_requests", 0)),
            active_requests=int(results.get("active_requests", 0)),
            queue_depth=qd,
            status="ok",
        )
