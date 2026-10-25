"""Regression: admin_metrics query facade preserves its contract (item 3).

- The router import path ``services.admin_metrics_queries_service`` still
  resolves with the same ``(db, prom_client)`` constructor.
- ``run_batch`` delegates to ``AdminMetricsBatchService`` with all args.
- Every ``services/admin_metrics/*.py`` file stays under the size ceiling
  (~350 lines, ~7 public methods per service-file discipline).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from services.admin_metrics_queries_service import AdminMetricsQueriesService

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

MAX_FILE_LINES = 350
MAX_PUBLIC_METHODS = 7


def test_facade_constructor_signature() -> None:
    """Facade ctor is still ``(db, prom_client)`` — router wiring unchanged."""
    params = list(inspect.signature(AdminMetricsQueriesService.__init__).parameters)
    assert params == ["self", "db", "prom_client"]


def test_facade_import_path_preserved() -> None:
    """The router import path resolves to the facade class."""
    import services.admin_metrics_queries_service as mod

    assert mod.AdminMetricsQueriesService is AdminMetricsQueriesService


@pytest.mark.asyncio
async def test_facade_run_batch_delegates() -> None:
    """``run_batch`` forwards every arg to ``AdminMetricsBatchService``."""
    facade = AdminMetricsQueriesService(db=AsyncMock(), prom_client=AsyncMock())
    from services.admin_metrics.batch_service import AdminMetricsBatchService

    assert isinstance(facade._batch, AdminMetricsBatchService)  # noqa: SLF001
    batch_mock = AsyncMock()
    batch_mock.run_batch.return_value = {"results": {}, "errors": [], "meta": {}}
    facade._batch = batch_mock  # noqa: SLF001

    request, org_config, start, end = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    project_id = UUID("00000000-0000-0000-0000-000000000003")
    result = await facade.run_batch(
        request, org_config, ORG_ID, start, end, 20, project_id, True
    )

    assert result == {"results": {}, "errors": [], "meta": {}}
    batch_mock.run_batch.assert_awaited_once_with(
        request, org_config, ORG_ID, start, end, 20, project_id, True
    )


@pytest.mark.parametrize(
    "filename",
    [
        "__init__.py",
        "shared.py",
        "db_queries_service.py",
        "prom_queries_service.py",
        "batch_service.py",
    ],
)
def test_admin_metrics_package_files_under_ceiling(filename: str) -> None:
    """Each package file stays under the service-file size discipline."""
    path = Path(__file__).resolve().parents[3] / "services" / "admin_metrics" / filename
    lines = path.read_text().splitlines()
    assert len(lines) <= MAX_FILE_LINES, f"{filename} has {len(lines)} lines"

    module_name = (
        "services.admin_metrics.batch_service"
        if filename == "batch_service.py"
        else None
    )
    if module_name is None:
        return
    import importlib

    mod = importlib.import_module(module_name)
    for attr in ("AdminMetricsBatchService",):
        cls = getattr(mod, attr)
        public = [
            m for m in dir(cls) if not m.startswith("_") and callable(getattr(cls, m))
        ]
        assert len(public) <= MAX_PUBLIC_METHODS, f"{attr}: {public}"
