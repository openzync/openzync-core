"""Unit tests for metrics_service — Prometheus query orchestration."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import httpx

from core.exceptions import MetricsUnavailableError
from services.metrics_service import MetricsService


class TestMetricsService:
    """Tests for MetricsService — get_summary, _fetch_value, _build_response."""

    def _make_service(self) -> MetricsService:
        return MetricsService(prometheus_url="http://prometheus:9090")

    async def test_summary_success(self) -> None:
        """All PromQL queries succeed and produce a complete response."""
        service = self._make_service()

        with patch.object(service, "_fetch_value", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = 42.0

            with patch("services.metrics_service.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_client.get.return_value = mock_resp

                summary = await service.get_summary()

        assert summary.status == "ok"
        assert summary.overall_latency_ms.p50 == 42.0
        assert summary.overall_latency_ms.p95 == 42.0
        assert summary.overall_latency_ms.p99 == 42.0
        assert summary.total_requests == 42
        assert summary.active_requests == 42
        assert summary.queue_depth is not None
        assert summary.queue_depth.high == 42
        assert summary.queue_depth.low == 42
        assert summary.error_rate_pct == 42.0

    async def test_prometheus_query_failure_raises_error(self) -> None:
        """If any PromQL query fails, MetricsUnavailableError is raised."""
        service = self._make_service()

        with patch.object(service, "_fetch_value", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = ValueError("connection timeout")

            with patch("services.metrics_service.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_client.get.return_value = mock_resp

                with pytest.raises(MetricsUnavailableError, match="Prometheus query failed"):
                    await service.get_summary()

    async def test_prometheus_unreachable_raises_error(self) -> None:
        """Readiness check failure raises MetricsUnavailableError."""
        service = self._make_service()

        with patch.object(service, "_fetch_value", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = 42.0

            with patch("services.metrics_service.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_client.get.side_effect = httpx.ConnectError("prometheus down")

                with pytest.raises(MetricsUnavailableError, match="Prometheus is unreachable"):
                    await service.get_summary()

    async def test_readiness_check_non_200(self) -> None:
        """Non-200 readiness response raises MetricsUnavailableError."""
        service = self._make_service()

        with patch.object(service, "_fetch_value", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = 42.0

            with patch("services.metrics_service.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                mock_resp = MagicMock()
                mock_resp.status_code = 503
                mock_client.get.return_value = mock_resp

                with pytest.raises(MetricsUnavailableError, match="readiness check returned 503"):
                    await service.get_summary()

    async def test_fetch_value_parses_prometheus_response(self) -> None:
        """_fetch_value correctly parses a Prometheus vector result."""
        service = self._make_service()

        with patch("services.metrics_service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {
                "status": "success",
                "data": {"result": [{"value": [1712345678, "123.456"]}]},
            }
            mock_client.get.return_value = mock_resp

            val = await service._fetch_value("up")
            assert val == 123.456

    async def test_fetch_value_empty_result_returns_zero(self) -> None:
        """Empty Prometheus result array returns 0.0."""
        service = self._make_service()

        with patch("services.metrics_service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {
                "status": "success",
                "data": {"result": []},
            }
            mock_client.get.return_value = mock_resp

            val = await service._fetch_value("up")
            assert val == 0.0

    async def test_fetch_value_api_error_status(self) -> None:
        """Non-success status from Prometheus API raises MetricsUnavailableError."""
        service = self._make_service()

        with patch("services.metrics_service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {
                "status": "error",
                "error": "timeout",
            }
            mock_client.get.return_value = mock_resp

            with pytest.raises(MetricsUnavailableError, match="Prometheus API error"):
                await service._fetch_value("up")
