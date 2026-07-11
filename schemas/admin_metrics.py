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

    # ── Health ─────────────────────────────────────────────────────────────
    status: str = Field("ok", description="ok or degraded")
    message: str | None = Field(None, description="Detail if degraded")
