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
import re
from datetime import UTC, datetime, timedelta

import httpx

from core.exceptions import MetricsUnavailableError
from schemas.admin_metrics import (
    ErrorTimeseriesPoint,
    LatencyPercentiles,
    LatencyTimeseriesPoint,
    MetricsSummaryResponse,
    QueueDepth,
    RetrievalTimeseries,
    TimeseriesPoint,
)

logger = logging.getLogger(__name__)

# ── PromQL query definitions ──────────────────────────────────────────────────
# Each query is a (name, PromQL) pair.  Names must match keys in the
# response builder below.

LATENCY_QUERIES: list[tuple[str, str]] = [
    (
        "overall_p50",
        "histogram_quantile(0.50, sum(rate(openzync_http_request_duration_seconds_bucket[5m])) by (le)) * 1000",
    ),
    (
        "overall_p95",
        "histogram_quantile(0.95, sum(rate(openzync_http_request_duration_seconds_bucket[5m])) by (le)) * 1000",
    ),
    (
        "overall_p99",
        "histogram_quantile(0.99, sum(rate(openzync_http_request_duration_seconds_bucket[5m])) by (le)) * 1000",
    ),
    (
        "context_p50",
        "histogram_quantile(0.50, sum(rate(openzync_context_latency_seconds_bucket[5m])) by (le)) * 1000",
    ),
    (
        "context_p95",
        "histogram_quantile(0.95, sum(rate(openzync_context_latency_seconds_bucket[5m])) by (le)) * 1000",
    ),
    (
        "context_p99",
        "histogram_quantile(0.99, sum(rate(openzync_context_latency_seconds_bucket[5m])) by (le)) * 1000",
    ),
    (
        "graph_search_p50",
        "histogram_quantile(0.50, sum(rate(openzync_graph_search_latency_seconds_bucket[5m])) by (le)) * 1000",
    ),
    (
        "graph_search_p95",
        "histogram_quantile(0.95, sum(rate(openzync_graph_search_latency_seconds_bucket[5m])) by (le)) * 1000",
    ),
    (
        "graph_search_p99",
        "histogram_quantile(0.99, sum(rate(openzync_graph_search_latency_seconds_bucket[5m])) by (le)) * 1000",
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

# ── Range query definitions (for time-series charts) ─────────────────────────
# These use [5m] rate windows and are queried over 24h with 1h step.

RETRIEVAL_RANGE_QUERIES: dict[str, str] = {
    "context": "sum(rate(openzync_context_latency_seconds_count[5m]))",
    "graph": "sum(rate(openzync_graph_search_latency_seconds_count[5m]))",
}

ERROR_RANGE_QUERIES: dict[str, str] = {
    "4xx": 'sum(increase(openzync_http_requests_total{status="4xx"}[1h]))',
    "5xx": 'sum(increase(openzync_http_requests_total{status="5xx"}[1h]))',
}

CONTEXT_LATENCY_RANGE_QUERIES: dict[str, str] = {
    "p50": "histogram_quantile(0.50, sum(rate(openzync_context_latency_seconds_bucket[5m])) by (le)) * 1000",
    "p95": "histogram_quantile(0.95, sum(rate(openzync_context_latency_seconds_bucket[5m])) by (le)) * 1000",
    "p99": "histogram_quantile(0.99, sum(rate(openzync_context_latency_seconds_bucket[5m])) by (le)) * 1000",
}

GRAPH_LATENCY_RANGE_QUERIES: dict[str, str] = {
    "p50": "histogram_quantile(0.50, sum(rate(openzync_graph_search_latency_seconds_bucket[5m])) by (le)) * 1000",
    "p95": "histogram_quantile(0.95, sum(rate(openzync_graph_search_latency_seconds_bucket[5m])) by (le)) * 1000",
    "p99": "histogram_quantile(0.99, sum(rate(openzync_graph_search_latency_seconds_bucket[5m])) by (le)) * 1000",
}

# ── Org-filter helpers ────────────────────────────────────────────────────────

# Queue depth is global infra — never org-filtered.
_ORG_FILTER_EXCLUDED = ("openzync_worker_queue_depth",)


def _is_org_scoped(org_id: str | None) -> bool:
    """Return True when PromQL should be scoped to an org."""
    return bool(org_id) and org_id != "anonymous"


def _inject_org_filter(promql: str, org_id: str) -> str:
    """Inject ``{org_id="..."}`` into every openzync_* selector.

    Selectors that already have a label set get ``,org_id="..."`` appended
    inside the braces. Bare metrics (``metric[`` or ``metric)``) get a new
    ``{org_id="..."}`` selector. Metrics in ``_ORG_FILTER_EXCLUDED`` are left
    untouched — queue depth is global infra, not per-org (cardinality + no
    org label on that metric).

    This keeps the canonical ``*_QUERIES`` constants global-readable and
    builds filtered copies on the fly — no duplication of query lists.
    """
    if any(excluded in promql for excluded in _ORG_FILTER_EXCLUDED):
        return promql

    org_label = f'org_id="{org_id}"'

    # 1) selectors already with braces: metric{labels} -> metric{labels,org_id="..."}
    def _with_braces(m: re.Match[str]) -> str:
        metric = m.group(1)
        inner = m.group(2).strip()
        if inner:
            return f"{metric}{{{inner},{org_label}}}"
        return f"{metric}{{{org_label}}}"

    promql = re.sub(r"(openzync_[a-z_]+)\{([^}]*)\}", _with_braces, promql)

    # 2) bare metrics (no braces) -> metric[ or metric) etc.
    # Use lookahead for [ or ) so we don't partial-match a filtered metric
    # (e.g. openzync_...{...} would otherwise backtrack and inject inside).
    promql = re.sub(
        r"(openzync_[a-z_]+)(?=\[|\)|,|\s|$)",
        lambda m: f"{m.group(1)}{{{org_label}}}",
        promql,
    )
    return promql


class MetricsService:
    """Aggregate metrics from Prometheus for the admin dashboard."""

    def __init__(self, prometheus_url: str) -> None:
        self._base_url = prometheus_url.rstrip("/")

    async def get_summary(self, org_id: str | None = None) -> MetricsSummaryResponse:
        """Run all PromQL queries and assemble the response.

        Args:
            org_id: Organization to scope Prometheus queries to. When ``None``
                or ``"anonymous"`` the original global queries are used
                (backward compat for health checks). The admin route always
                passes a real org_id.

        Returns:
            A fully populated ``MetricsSummaryResponse``.

        Raises:
            MetricsUnavailableError: If Prometheus is unreachable or any
                query fails.
        """
        results: dict[str, float] = {}

        # Build org-scoped instant queries (queue stays global — see _inject_org_filter).
        if _is_org_scoped(org_id):
            assert org_id is not None  # narrowed by _is_org_scoped
            effective_all = [
                (name, _inject_org_filter(promql, org_id))
                for name, promql in ALL_QUERIES
            ]
            effective_retrieval = {
                k: _inject_org_filter(v, org_id)
                for k, v in RETRIEVAL_RANGE_QUERIES.items()
            }
            effective_error = {
                k: _inject_org_filter(v, org_id) for k, v in ERROR_RANGE_QUERIES.items()
            }
            effective_ctx_lat = {
                k: _inject_org_filter(v, org_id)
                for k, v in CONTEXT_LATENCY_RANGE_QUERIES.items()
            }
            effective_graph_lat = {
                k: _inject_org_filter(v, org_id)
                for k, v in GRAPH_LATENCY_RANGE_QUERIES.items()
            }
        else:
            effective_all = ALL_QUERIES
            effective_retrieval = RETRIEVAL_RANGE_QUERIES
            effective_error = ERROR_RANGE_QUERIES
            effective_ctx_lat = CONTEXT_LATENCY_RANGE_QUERIES
            effective_graph_lat = GRAPH_LATENCY_RANGE_QUERIES

        async def _query(name: str, promql: str) -> tuple[str, float]:
            val = await self._fetch_value(promql)
            return name, val

        tasks = [_query(name, promql) for name, promql in effective_all]
        completed = await asyncio.gather(*tasks, return_exceptions=True)

        for item in completed:
            if isinstance(item, Exception):
                logger.error("metrics.prometheus_query_failed", exc_info=True)
                raise MetricsUnavailableError("Prometheus query failed.") from item
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
            raise MetricsUnavailableError("Prometheus is unreachable.") from exc

        # ── Range queries for time-series charts ────────────────────────────
        now = datetime.now(UTC)
        start_iso = (now - timedelta(hours=24)).isoformat()
        end_iso = now.isoformat()

        async def _range_task(name: str, promql: str) -> tuple[str, list[dict]]:
            vals = await self._fetch_range(promql, start_iso, end_iso)
            return name, vals

        range_tasks = (
            [_range_task(f"retrieval_{k}", v) for k, v in effective_retrieval.items()]
            + [_range_task(f"error_{k}", v) for k, v in effective_error.items()]
            + [_range_task(f"ctx_lat_{k}", v) for k, v in effective_ctx_lat.items()]
            + [_range_task(f"graph_lat_{k}", v) for k, v in effective_graph_lat.items()]
        )

        range_completed = await asyncio.gather(*range_tasks, return_exceptions=True)

        range_results: dict[str, list[dict]] = {}
        for item in range_completed:
            if isinstance(item, Exception):
                logger.warning("metrics.range_query_failed", exc_info=True)
                continue
            name, vals = item
            range_results[name] = vals

        # Build time-series response objects
        retrieval_ts = RetrievalTimeseries(
            context_retrievals=[
                TimeseriesPoint(timestamp=p["timestamp"], value=p["value"])
                for p in range_results.get("retrieval_context", [])
            ],
            graph_retrievals=[
                TimeseriesPoint(timestamp=p["timestamp"], value=p["value"])
                for p in range_results.get("retrieval_graph", [])
            ],
        )

        # Align error 4xx/5xx by timestamp
        error_4xx = {
            p["timestamp"]: p["value"] for p in range_results.get("error_4xx", [])
        }
        error_5xx = {
            p["timestamp"]: p["value"] for p in range_results.get("error_5xx", [])
        }
        all_error_ts = sorted(set(error_4xx) | set(error_5xx))
        error_ts = [
            ErrorTimeseriesPoint(
                date=ts,
                count_4xx=int(error_4xx.get(ts, 0)),
                count_5xx=int(error_5xx.get(ts, 0)),
            )
            for ts in all_error_ts
        ]

        # Align latency p50/p95/p99 by timestamp
        def _build_latency_ts(
            p50_key: str, p95_key: str, p99_key: str
        ) -> list[LatencyTimeseriesPoint]:
            p50 = {p["timestamp"]: p["value"] for p in range_results.get(p50_key, [])}
            p95 = {p["timestamp"]: p["value"] for p in range_results.get(p95_key, [])}
            p99 = {p["timestamp"]: p["value"] for p in range_results.get(p99_key, [])}
            all_ts = sorted(set(p50) | set(p95) | set(p99))
            return [
                LatencyTimeseriesPoint(
                    date=ts,
                    p50=round(p50.get(ts, 0.0), 1),
                    p95=round(p95.get(ts, 0.0), 1),
                    p99=round(p99.get(ts, 0.0), 1),
                )
                for ts in all_ts
            ]

        ctx_lat_ts = _build_latency_ts("ctx_lat_p50", "ctx_lat_p95", "ctx_lat_p99")
        graph_lat_ts = _build_latency_ts(
            "graph_lat_p50", "graph_lat_p95", "graph_lat_p99"
        )

        response = self._build_response(results)
        response.retrieval_timeseries = retrieval_ts
        response.error_timeseries = error_ts
        response.context_latency_timeseries = ctx_lat_ts
        response.graph_latency_timeseries = graph_lat_ts
        return response

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

    async def _fetch_range(
        self, promql: str, start: str, end: str, step: str = "1h"
    ) -> list[dict[str, str | float]]:
        """Execute a PromQL range query and return the result series.

        Args:
            promql: The PromQL query string.
            start: ISO start timestamp.
            end: ISO end timestamp.
            step: Resolution step (default 1h).

        Returns:
            List of ``{"timestamp": ..., "value": ...}`` dicts.
            Empty list on failure or no data.
        """
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"{self._base_url}/api/v1/query_range",
                    params={
                        "query": promql,
                        "start": start,
                        "end": end,
                        "step": step,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            logger.warning("metrics.range_query_failed", extra={"error": str(exc)})
            return []

        if data.get("status") != "success":
            logger.warning(
                "metrics.range_api_error", extra={"error": data.get("error", "")}
            )
            return []

        results = data["data"]["result"]
        if not results:
            return []

        # Return the first series' values: [[timestamp, value], ...]
        return [
            {"timestamp": str(v[0]), "value": float(v[1])}
            for v in results[0].get("values", [])
        ]

    def _build_response(self, results: dict[str, float]) -> MetricsSummaryResponse:
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
