"""Unit tests for UserConfigService system-managed field guard.

Observed contract: graph connection details moved to env
(``OZ_FALKORDB_URL`` / ``OZ_SURREALDB_*``), so
``get_system_managed_fields()`` is always ``[]`` and every patch key
merges through. ``PATCH /v1/me/config`` still maps a service-layer
``ValidationError`` to 422 problem+json.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.exceptions import ValidationError, register_exception_handlers
from dependencies.auth import get_dashboard_user
from dependencies.services import get_user_config_service
from routers.me import router
from services.user_config_service import UserConfigService

USER_ID = UUID("00000000-0000-0000-0000-000000000002")


def _make_service() -> tuple[UserConfigService, AsyncMock]:
    """Build a service with a mocked user repo (no I/O)."""
    mock_repo = AsyncMock()
    mock_repo.session = AsyncMock()
    service = UserConfigService(
        user_repo=mock_repo,
        redis=None,
        auth_repo=AsyncMock(),
        db=AsyncMock(),
    )
    return service, mock_repo


@pytest.mark.unit
class TestUpdateOverrideSystemManaged:
    """``update_override`` has no system-managed guard anymore.

    Graph connection details moved to env (``OZ_FALKORDB_URL`` /
    ``OZ_SURREALDB_*``) with no per-user override, so
    ``get_system_managed_fields()`` is always ``[]`` and every patch
    key — including the removed ``falkordb_url`` — merges through.
    """

    @pytest.mark.asyncio
    async def test_system_managed_fields_always_empty(self) -> None:
        """No fields are locked at the system level anymore."""
        service, _ = _make_service()
        assert service.get_system_managed_fields() == []

    @pytest.mark.asyncio
    async def test_falkordb_url_merges_through(self) -> None:
        """The removed ``falkordb_url`` key is no longer rejected."""
        service, mock_repo = _make_service()
        mock_repo.merge_config_override.return_value = {
            "falkordb_url": "redis://other:6379"
        }
        result = await service.update_override(
            USER_ID, {"falkordb_url": "redis://other:6379"}
        )
        assert result == {"falkordb_url": "redis://other:6379"}
        mock_repo.merge_config_override.assert_awaited_once_with(
            USER_ID, {"falkordb_url": "redis://other:6379"}
        )


@pytest.mark.unit
class TestUpdateMyConfigEndpoint:
    """``PATCH /v1/me/config`` maps the guard to 422 problem+json."""

    @staticmethod
    def _create_app(mock_service: AsyncMock) -> FastAPI:
        """Build a minimal app with the me router and problem handlers."""
        app = FastAPI()
        app.state.redis = AsyncMock()
        app.dependency_overrides[get_user_config_service] = lambda: mock_service
        app.dependency_overrides[get_dashboard_user] = lambda: str(USER_ID)
        app.include_router(router)
        register_exception_handlers(app)
        return app

    @pytest.mark.asyncio
    async def test_system_managed_422_problem_json(self) -> None:
        """Service ValidationError surfaces as 422 problem+json (not 400/500)."""
        mock_service = AsyncMock(spec=UserConfigService)
        mock_service.update_override.side_effect = ValidationError(
            "These fields are configured at the system level "
            "and cannot be modified: falkordb_url."
        )
        app = self._create_app(mock_service)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/v1/me/config",
                json={"config": {"falkordb_url": "redis://other:6379"}},
            )
        assert resp.status_code == 422
        body = resp.json()
        assert body["type"] == "https://errors.openzync.tech/validation_error"
        assert body["title"] == "Validation Error"
        assert body["status"] == 422
        assert "falkordb_url" in body["detail"]
