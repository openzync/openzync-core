"""Admin metrics sub-domain services — DB, Prometheus, and batch fan-out.

Thin composition root is :class:`AdminMetricsQueriesService` in
``services/admin_metrics_queries_service.py`` (import path preserved for
the router); the per-query implementations live here, one class per file.
"""
