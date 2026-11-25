"""Unit tests for UserConfigService system-managed field guard.

Observed contract: when ``OZ_FALKORDB_URL`` is set at the system level,
``PATCH /v1/me/config`` rejects ``falkordb_url`` with a ``ValidationError``
(surfaced as 422 problem+json), while unmanaged keys such as
``llm_temperature`` merge through.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
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


def _managed_settings() -> SimpleNamespace:
    """System settings with ``OZ_FALKORDB_URL`` set (falkordb managed)."""
    return SimpleNamespace(SURREALDB_URL=None, FALKORDB_URL="redis://falkordb:6379")


@pytest.mark.unit
class TestUpdateOverrideSystemManaged:
    """``update_override`` guard for system-managed fields."""

    @pytest.mark.asyncio
    async def test_rejects_system_managed_falkordb_url(self) -> None:
        """``falkordb_url`` patch raises when the system URL is set."""
        service, mock_repo = _make_service()
        with patch(
            "services.user_config_service.get_settings",
            return_value=_managed_settings(),
        ):
            with pytest.raises(ValidationError, match="falkordb_url"):
                await service.update_override(
                    USER_ID, {"falkordb_url": "redis://other:6379"}
                )
        mock_repo.merge_config_override.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allows_unmanaged_llm_temperature(self) -> None:
        """Unmanaged keys merge through even when the system URL is set."""
        service, mock_repo = _make_service()
        mock_repo.merge_config_override.return_value = {"llm_temperature": 0.7}
        with patch(
            "services.user_config_service.get_settings",
            return_value=_managed_settings(),
        ):
            result = await service.update_override(
                USER_ID, {"llm_temperature": 0.7}
            )
        assert result == {"llm_temperature": 0.7}
        mock_repo.merge_config_override.assert_awaited_once_with(
            USER_ID, {"llm_temperature": 0.7}
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
