"""Prometheus metrics for the FastAPI application.

Exposes a set of standard RED (Rate, Errors, Duration) metrics suitable for
Grafana dashboarding.  Metrics are registered on a custom ``REGISTRY`` to
isolate application metrics from system/client metrics.

Usage:
    from middleware.metrics import METRICS_REGISTRY, http_requests_total

    # Increment in middleware or service code
    http_requests_total.labels(method="GET", path="/v1/users", status="200").inc()

To expose the metrics, mount ``routers/metrics.py`` at ``GET /metrics``.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, CollectorRegistry

# ── Isolated registry — does not include default process/GC metrics ──────────
METRICS_REGISTRY = CollectorRegistry(auto_describe=False)

# ── HTTP RED metrics ─────────────────────────────────────────────────────────
# Rate: requests per second, broken down by method + path + status

http_requests_total = Counter(
    "openzep_http_requests_total",
    "Total HTTP requests processed.",
    labelnames=["method", "path", "status"],
    registry=METRICS_REGISTRY,
)

# Errors: 5xx responses
http_errors_total = Counter(
    "openzep_http_errors_total",
    "Total HTTP 5xx errors.",
    labelnames=["method", "path"],
    registry=METRICS_REGISTRY,
)

# Duration: request latency histogram (p50/p95/p99 buckets)
http_request_duration_seconds = Histogram(
    "openzep_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    labelnames=["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=METRICS_REGISTRY,
)

# In-flight requests (useful for saturation detection)
http_requests_in_progress = Gauge(
    "openzep_http_requests_in_progress",
    "HTTP requests currently being handled.",
    labelnames=["method"],
    registry=METRICS_REGISTRY,
)

# Request body size
http_request_size_bytes = Histogram(
    "openzep_http_request_size_bytes",
    "HTTP request body size in bytes.",
    labelnames=["method"],
    buckets=(64, 256, 1024, 4096, 16384, 65536, 262144, 1048576),
    registry=METRICS_REGISTRY,
)

# ── Application-level metrics (set by service layer) ─────────────────────────

context_latency_seconds = Histogram(
    "openzep_context_latency_seconds",
    "Context assembly latency. ``type`` distinguishes cold (miss) vs warm (hit).",
    labelnames=["type"],  # "cold" | "warm"
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=METRICS_REGISTRY,
)

graph_search_latency_seconds = Histogram(
    "openzep_graph_search_latency_seconds",
    "Hybrid graph+vector+BM25 search latency in seconds.",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=METRICS_REGISTRY,
)

reranker_latency_seconds = Histogram(
    "openzep_reranker_latency_seconds",
    "Cross-encoder re-ranker inference latency in seconds.",
    labelnames=["backend"],  # "sentence_transformers" | "cohere"
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=METRICS_REGISTRY,
)

# ── ASGI middleware ──────────────────────────────────────────────────────────


class MetricsMiddleware:
    """ASGI middleware that records RED metrics for every request.

    Must be placed outermost (registered last) so it wraps everything
    including the 404 handler for unknown routes.
    """

    def __init__(self, app: callable) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: callable, send: callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "UNKNOWN")
        path_template: str = scope.get("path", "/unknown")

        # Track in-flight requests
        http_requests_in_progress.labels(method=method).inc()

        # Capture response status
        status_code: int = 200

        async def _send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 200)
            await send(message)

        import time

        start = time.monotonic()
        try:
            await self.app(scope, receive, _send_wrapper)
        except Exception:
            status_code = 500
            raise
        finally:
            duration = time.monotonic() - start
            http_requests_in_progress.labels(method=method).dec()

            status_group = f"{status_code // 100}xx"
            http_requests_total.labels(
                method=method, path=path_template, status=status_group
            ).inc()

            if status_code >= 500:
                http_errors_total.labels(method=method, path=path_template).inc()

            # Only record duration for non-WebSocket upgrades
            if scope.get("type") == "http":
                http_request_duration_seconds.labels(
                    method=method, path=path_template
                ).observe(duration)
