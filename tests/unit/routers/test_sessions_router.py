"""Unit tests for the /v1/projects/{project_id}/sessions router — HTTP adapter layer.

Tests every endpoint in the sessions router via httpx.AsyncClient with
dependency_overrides.  Complements the existing test_sessions.py which tests
dependency guard-clause behaviour.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from schemas.common import PaginatedResponse
from schemas.sessions import SessionResponse, SessionListResponse, MessageResponse

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
SESSION_ID = UUID("00000000-0000-0000-0000-000000000004")


@pytest.fixture(autouse=True)
def _init_settings() -> None:
    """Initialise the Settings singleton with dummy values."""
    from core.config import Settings, set_settings

    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/test",
        REDIS_URL="redis://localhost:6379/1",
        SECRET_KEY="a" * 32,
        WEBHOOK_SIGNING_SECRET="b" * 32,
        ENVIRONMENT="test",
    )
    set_settings(settings)


def _make_session_response(overrides: dict | None = None) -> dict:
    """Build a realistic SessionResponse dict."""
    base = {
        "id": SESSION_ID,
        "project_id": PROJECT_ID,
        "created_by": USER_ID,
        "external_id": "session_001",
        "metadata": {},
        "is_active": True,
        "message_count": 5,
        "fact_count": 2,
        "pending_enrichment_count": 0,
        "observation_count": 0,
        "closed_at": None,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    base.update(overrides or {})
    return base


def _make_session_list_response(overrides: dict | None = None) -> dict:
    """Build a realistic SessionListResponse dict."""
    base = {
        "id": SESSION_ID,
        "project_id": PROJECT_ID,
        "created_by": USER_ID,
        "external_id": "session_001",
        "is_active": True,
        "message_count": 5,
        "fact_count": 2,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    base.update(overrides or {})
    return base


def _make_message_response(overrides: dict | None = None) -> dict:
    """Build a realistic MessageResponse dict."""
    base = {
        "id": UUID("00000000-0000-0000-0000-000000000010"),
        "role": "user",
        "content": "Hello",
        "metadata": {},
        "token_count": 10,
        "sequence_number": 0,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "blobs": [],
    }
    base.update(overrides or {})
    return base


def _make_fact_response(overrides: dict | None = None) -> dict:
    """Build a realistic FactResponse dict."""
    base = {
        "id": UUID("00000000-0000-0000-0000-000000000020"),
        "content": "Alice likes hiking",
        "subject": "Alice",
        "predicate": "likes",
        "object": "hiking",
        "confidence": 0.95,
        "source_episode_id": None,
        "subject_type": "entity",
        "object_type": "literal",
        "subject_entity_id": None,
        "object_entity_id": None,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    base.update(overrides or {})
    return base


class TestSessionsRouter:
    """Full HTTP-adapter tests for the sessions router."""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        """Set up shared mocks and app for each test."""
        self.session_service = AsyncMock()
        self.fact_service = AsyncMock()

        from dependencies.services import get_fact_service, get_session_service
        from dependencies.project_auth import require_project_membership
        from dependencies.auth import get_current_user_id
        from dependencies.request import get_current_org_id, get_project_id

        from routers.sessions import router

        self.app = FastAPI()
        self.app.include_router(router)

        self.app.dependency_overrides[get_session_service] = lambda: self.session_service
        self.app.dependency_overrides[get_fact_service] = lambda: self.fact_service
        self.app.dependency_overrides[require_project_membership] = lambda: None
        self.app.dependency_overrides[get_current_user_id] = lambda: USER_ID
        self.app.dependency_overrides[get_current_org_id] = lambda: ORG_ID
        self.app.dependency_overrides[get_project_id] = lambda: PROJECT_ID

        @self.app.middleware("http")
        async def _mock_auth(request: Request, call_next):
            request.state.org_id = str(ORG_ID)
            request.state.user_id = str(USER_ID)
            request.state.auth_type = "jwt"
            response = await call_next(request)
            return response

    async def test_create_session_success(self) -> None:
        """POST .../sessions → 201 with SessionResponse."""
        self.session_service.create_session.return_value = SessionResponse.model_validate(
            _make_session_response(),
        )

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/v1/projects/{PROJECT_ID}/sessions",
                json={"external_id": "session_001"},
            )

        assert response.status_code == 201
        data = response.json()
        assert data["external_id"] == "session_001"
        assert data["is_active"] is True
        self.session_service.create_session.assert_awaited_once()

    async def test_create_session_422_missing_external_id(self) -> None:
        """POST .../sessions with no external_id → 422."""
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/v1/projects/{PROJECT_ID}/sessions",
                json={},
            )

        assert response.status_code == 422
        self.session_service.create_session.assert_not_called()

    async def test_create_session_422_empty_external_id(self) -> None:
        """POST .../sessions with empty external_id → 422."""
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/v1/projects/{PROJECT_ID}/sessions",
                json={"external_id": ""},
            )

        assert response.status_code == 422
        self.session_service.create_session.assert_not_called()

    async def test_list_sessions_success(self) -> None:
        """GET .../sessions → 200 with PaginatedResponse."""
        self.session_service.list_sessions.return_value = PaginatedResponse[
            SessionListResponse
        ](
            data=[SessionListResponse.model_validate(_make_session_list_response())],
            next_cursor=None,
            has_more=False,
        )

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/sessions",
            )

        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert len(data["data"]) == 1
        assert data["data"][0]["external_id"] == "session_001"
        self.session_service.list_sessions.assert_awaited_once()

    async def test_list_sessions_422_invalid_limit(self) -> None:
        """GET .../sessions with limit out of range → 422."""
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/sessions?limit=999",
            )

        assert response.status_code == 422
        self.session_service.list_sessions.assert_not_called()

    async def test_get_session_success(self) -> None:
        """GET .../sessions/{session_id} → 200 with SessionResponse."""
        self.session_service.get_session.return_value = SessionResponse.model_validate(
            _make_session_response(),
        )

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/sessions/{SESSION_ID}",
            )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(SESSION_ID)
        self.session_service.get_session.assert_awaited_once()

    async def test_get_session_404_not_found(self) -> None:
        """GET .../sessions/{session_id} when not found → 404."""
        from core.exceptions import NotFoundError, register_exception_handlers

        register_exception_handlers(self.app)
        self.session_service.get_session.side_effect = NotFoundError(
            message="Session not found",
            detail={"session_id": str(SESSION_ID)},
        )

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/sessions/{SESSION_ID}",
            )

        assert response.status_code == 404

    async def test_get_session_messages_success(self) -> None:
        """GET .../sessions/{session_id}/messages → 200."""
        self.session_service.get_messages.return_value = PaginatedResponse[
            MessageResponse
        ](
            data=[MessageResponse.model_validate(_make_message_response())],
            next_cursor=None,
            has_more=False,
        )

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/sessions/{SESSION_ID}/messages",
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["content"] == "Hello"
        self.session_service.get_messages.assert_awaited_once()

    async def test_get_session_messages_with_cursor(self) -> None:
        """GET .../sessions/{id}/messages with cursor param → 200."""
        self.session_service.get_messages.return_value = PaginatedResponse[
            MessageResponse
        ](data=[], next_cursor=None, has_more=False)

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/sessions/{SESSION_ID}/messages",
                params={"cursor": "next_page_token", "limit": 25},
            )

        assert response.status_code == 200
        self.session_service.get_messages.assert_awaited_once()
        call_kwargs = self.session_service.get_messages.call_args.kwargs
        assert call_kwargs["cursor"] == "next_page_token"
        assert call_kwargs["limit"] == 25

    async def test_get_session_facts_success(self) -> None:
        """GET .../sessions/{session_id}/facts → 200."""
        from schemas.facts import FactResponse

        self.session_service.get_session.return_value = SessionResponse.model_validate(
            _make_session_response(),
        )
        self.fact_service.list_facts_by_session.return_value = (
            [FactResponse.model_validate(_make_fact_response())],
            None,
        )

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/v1/projects/{PROJECT_ID}/sessions/{SESSION_ID}/facts",
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["content"] == "Alice likes hiking"
        self.session_service.get_session.assert_awaited_once()
        self.fact_service.list_facts_by_session.assert_awaited_once()

    async def test_delete_session_success(self) -> None:
        """DELETE .../sessions/{session_id} → 204 No Content."""
        self.session_service.delete_session.return_value = None

        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.delete(
                f"/v1/projects/{PROJECT_ID}/sessions/{SESSION_ID}",
            )

        assert response.status_code == 204
        assert response.content == b""
        self.session_service.delete_session.assert_awaited_once()
