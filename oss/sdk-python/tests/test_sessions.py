"""Tests for the OpenZep Python SDK — sessions domain."""

from __future__ import annotations

import pytest


class TestSessionsClient:
    """Tests for ``AsyncSessionsClient``."""

    @pytest.mark.asyncio
    async def test_create_session(self, async_client, mock_http):
        """POST /sessions returns SessionResponse."""
        user_id = "u1"
        mock_http.post(f"/v1/users/{user_id}/sessions").respond(json={
            "id": "s1",
            "user_id": user_id,
            "external_id": "demo",
            "metadata": {},
            "is_active": True,
            "message_count": 0,
            "fact_count": 0,
            "created_at": "2026-01-01T00:00:00Z",
        })

        session = await async_client.sessions.create(
            user_id=user_id, external_id="demo"
        )
        assert session.id == "s1"
        assert session.external_id == "demo"

    @pytest.mark.asyncio
    async def test_get_session(self, async_client, mock_http):
        """GET /sessions/{id} returns session."""
        user_id = "u1"
        session_id = "s1"
        mock_http.get(f"/v1/users/{user_id}/sessions/{session_id}").respond(json={
            "id": session_id, "user_id": user_id, "external_id": "demo",
            "metadata": {}, "is_active": True,
            "message_count": 0, "fact_count": 0,
            "created_at": "2026-01-01T00:00:00Z",
        })

        session = await async_client.sessions.get(user_id=user_id, session_id=session_id)
        assert session.external_id == "demo"

    @pytest.mark.asyncio
    async def test_get_messages(self, async_client, mock_http):
        """GET /sessions/{id}/messages returns messages."""
        user_id = "u1"
        session_id = "s1"
        mock_http.get(f"/v1/users/{user_id}/sessions/{session_id}/messages").respond(json={
            "data": [
                {"id": "e1", "role": "user", "content": "Hello",
                 "metadata": {}, "token_count": 0, "sequence_number": 0,
                 "created_at": "2026-01-01T00:00:00Z"},
                {"id": "e2", "role": "assistant", "content": "Hi",
                 "metadata": {}, "token_count": 0, "sequence_number": 1,
                 "created_at": "2026-01-01T00:00:00Z"},
            ],
            "next_cursor": None,
            "has_more": False,
        })

        msgs = await async_client.sessions.messages(
            user_id=user_id, session_id=session_id
        )
        assert len(msgs.data) == 2
        assert msgs.data[0].role == "user"
