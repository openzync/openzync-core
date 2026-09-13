"""Pydantic schemas for the admin metrics dashboard.

All response models provide aggregate data suitable for a real-time admin
frontend — combining DB counts and Prometheus-backed latency/error metrics.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LatencyPercentiles(BaseModel):
    """Latency distribution at key percentiles (in milliseconds)."""

    p50: float = Field(0.0, description="50th percentile latency in ms")
    p95: float = Field(0.0, description="95th percentile latency in ms")
    p99: float = Field(0.0, description="99th percentile latency in ms")


class TimeseriesPoint(BaseModel):
    """Single point in a time-series."""

    timestamp: str = Field(..., description="ISO timestamp")
    value: float = Field(..., description="Metric value")


class RetrievalTimeseries(BaseModel):
    """Retrieval rate time-series for the dashboard chart."""

    context_retrievals: list[TimeseriesPoint] = Field(default_factory=list)
    graph_retrievals: list[TimeseriesPoint] = Field(default_factory=list)


class ErrorTimeseriesPoint(BaseModel):
    """Daily error count for the error chart."""

    date: str = Field(..., description="Date in YYYY-MM-DD format")
    count_4xx: int = Field(0, description="4xx error count")
    count_5xx: int = Field(0, description="5xx error count")


class LatencyTimeseriesPoint(BaseModel):
    """Daily latency percentiles for the latency chart."""

    date: str = Field(..., description="Date in YYYY-MM-DD format")
    p50: float = Field(0.0, description="50th percentile in ms")
    p95: float = Field(0.0, description="95th percentile in ms")
    p99: float = Field(0.0, description="99th percentile in ms")


class QueueDepth(BaseModel):
    """ARQ worker queue depths."""

    high: int = Field(0, description="High-priority queue depth")
    low: int = Field(0, description="Low-priority queue depth")


class EpisodeStats(BaseModel):
    """Episode metrics — ingestion pipeline status."""

    added_total: int = Field(0, description="Total episodes ever created")
    added_24h: int = Field(0, description="Episodes created in last 24 hours")
    in_progress: int = Field(0, description="Episodes with incomplete enrichment")
    enrichment_pending: int = Field(0, description="Episodes with no enrichment started")
    fully_enriched: int = Field(0, description="Episodes with all enrichment bits set (status=63)")
    with_embeddings: int = Field(0, description="Episodes with embedding vector populated")
    fully_enriched_pct: float = Field(0.0, description="Percentage of episodes fully enriched")


class GraphStats(BaseModel):
    """Graph entity metrics."""

    entities_total: int = Field(0, description="Total graph entities created")
    entities_24h: int = Field(0, description="Entities created in last 24 hours")
    relationships_total: int = Field(0, description="Total graph relationships")


class MetricsSummaryResponse(BaseModel):
    """Aggregated metrics for the admin dashboard frontend.

    Combines DB counts with Prometheus-sourced latency and error metrics.
    The ``status`` field indicates whether Prometheus is reachable.
    """

    # ── Data counts (from DB) ──────────────────────────────────────────────
    episodes: EpisodeStats = Field(default_factory=EpisodeStats)
    graphs: GraphStats = Field(default_factory=GraphStats)
    users_total: int = Field(0, description="Total non-deleted users")

    # ── Performance (from Prometheus) ──────────────────────────────────────
    request_rate: dict[str, float] = Field(
        default_factory=lambda: {"2xx": 0.0, "4xx": 0.0, "5xx": 0.0},
        description="Requests per second by status class",
    )
    error_rate_pct: float = Field(0.0, description="Percentage of 5xx errors")
    overall_latency_ms: LatencyPercentiles = Field(default_factory=LatencyPercentiles)
    context_latency_ms: LatencyPercentiles = Field(default_factory=LatencyPercentiles)
    graph_search_latency_ms: LatencyPercentiles = Field(default_factory=LatencyPercentiles)
    queue_depth: QueueDepth | None = Field(None, description="Worker queue depth")
    total_requests: int = Field(0, description="Total HTTP requests ever")
    active_requests: int = Field(0, description="Currently in-flight requests")

    # ── Time-series (from Prometheus range queries) ─────────────────────────
    retrieval_timeseries: RetrievalTimeseries = Field(
        default_factory=RetrievalTimeseries,
        description="Retrieval rate over the last 24h",
    )
    error_timeseries: list[ErrorTimeseriesPoint] = Field(
        default_factory=list,
        description="Hourly 4xx/5xx error counts over the last 24h",
    )
    context_latency_timeseries: list[LatencyTimeseriesPoint] = Field(
        default_factory=list,
        description="Context latency percentiles over the last 24h",
    )
    graph_latency_timeseries: list[LatencyTimeseriesPoint] = Field(
        default_factory=list,
        description="Graph search latency percentiles over the last 24h",
    )

    # ── Health ─────────────────────────────────────────────────────────────
    status: str = Field("ok", description="ok or degraded")
    message: str | None = Field(None, description="Detail if degraded")
