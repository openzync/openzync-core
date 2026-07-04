"""Tests for OZChatMessageHistory."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from openzync._errors import NotFoundError
from openzync.client import AsyncOpenZync
from openzync.integrations.langchain.message_history import OZChatMessageHistory
from openzync.models.session import SessionMessagesResponse

# Sample messages as returned by the API
SAMPLE_MESSAGES = [
    {
        "id": "ep-1",
        "role": "user",
        "content": "Hello",
        "metadata": {},
        "token_count": 2,
        "sequence_number": 1,
        "created_at": "2025-01-01T00:00:00Z",
    },
    {
        "id": "ep-2",
        "role": "assistant",
        "content": "Hi there!",
        "metadata": {},
        "token_count": 3,
        "sequence_number": 2,
        "created_at": "2025-01-01T00:00:01Z",
    },
]


@pytest.fixture
def mock_client():
    """Create a real AsyncOpenZync with mocked sub-clients."""
    client = AsyncOpenZync(api_key="test", base_url="http://test")
    client.memory = AsyncMock()
    client.sessions = AsyncMock()
    return client


class TestOZChatMessageHistory:
    """Tests for OZChatMessageHistory."""

    def test_init(self, mock_client):
        """Initialise with required args."""
        history = OZChatMessageHistory(
            session_id="session-1",
            project_id="project-1",
            client=mock_client,
        )
        assert history.session_id == "session-1"
        assert history.project_id == "project-1"
        assert history._messages is None

    @pytest.mark.asyncio
    async def test_aget_messages_empty(self, mock_client):
        """Return empty list when no messages exist."""
        mock_client.sessions.messages.side_effect = NotFoundError(
            "Session not found"
        )

        history = OZChatMessageHistory(
            session_id="session-1",
            project_id="project-1",
            client=mock_client,
        )
        messages = await history.aget_messages()
        assert messages == []

    @pytest.mark.asyncio
    async def test_aget_messages_returns_messages(self, mock_client):
        """Return messages from the server."""
        resp = SessionMessagesResponse(**{"data": SAMPLE_MESSAGES})
        mock_client.sessions.messages.return_value = resp

        history = OZChatMessageHistory(
            session_id="session-1",
            project_id="project-1",
            client=mock_client,
        )
        messages = await history.aget_messages()

        assert len(messages) == 2
        assert isinstance(messages[0], HumanMessage)
        assert messages[0].content == "Hello"
        assert isinstance(messages[1], AIMessage)
        assert messages[1].content == "Hi there!"

    @pytest.mark.asyncio
    async def test_aadd_messages_persists_via_ingest(self, mock_client):
        """Add messages calls memory.ingest."""
        mock_client.sessions.messages.side_effect = NotFoundError(
            "Session not found"
        )
        mock_client.memory.ingest = AsyncMock(
            return_value=AsyncMock(episode_count=1)
        )

        history = OZChatMessageHistory(
            session_id="session-1",
            project_id="project-1",
            client=mock_client,
        )
        await history.aadd_messages([HumanMessage(content="Hi")])

        mock_client.memory.ingest.assert_awaited_once()
        call_args = mock_client.memory.ingest.await_args
        # project_id is the first positional arg
        assert call_args.args[0] == "project-1"
        assert call_args.kwargs["session_id"] == "session-1"
        assert call_args.kwargs["messages"] == [{"role": "user", "content": "Hi"}]

    @pytest.mark.asyncio
    async def test_aadd_messages_updates_cache(self, mock_client):
        """After adding, aget_messages returns the new messages."""
        mock_client.sessions.messages.side_effect = NotFoundError(
            "Session not found"
        )
        mock_client.memory.ingest = AsyncMock()

        history = OZChatMessageHistory(
            session_id="session-1",
            project_id="project-1",
            client=mock_client,
        )
        await history.aadd_messages([HumanMessage(content="Hi")])

        messages = await history.aget_messages()
        assert len(messages) == 1
        assert messages[0].content == "Hi"

    @pytest.mark.asyncio
    async def test_aclear_deletes_memory(self, mock_client):
        """Clear calls memory.delete and clears cache."""
        mock_client.memory.delete = AsyncMock()

        history = OZChatMessageHistory(
            session_id="session-1",
            project_id="project-1",
            client=mock_client,
        )
        history._messages = [HumanMessage(content="Hi")]  # seed cache
        await history.aclear()

        mock_client.memory.delete.assert_awaited_once_with("project-1")
        assert history._messages == []

    def test_add_message_sync_ingests(self, mock_client):
        """Sync add_message calls ingest (wrapped via asyncio.run)."""
        mock_client.memory.ingest = AsyncMock()

        history = OZChatMessageHistory(
            session_id="session-1",
            project_id="project-1",
            client=mock_client,
        )
        # Seed cache to avoid network call in _load_messages_if_needed
        history._messages = []
        history.add_message(HumanMessage(content="Hello"))

        mock_client.memory.ingest.assert_called_once()

    def test_messages_property(self, mock_client):
        """Messages property returns cached messages."""
        history = OZChatMessageHistory(
            session_id="session-1",
            project_id="project-1",
            client=mock_client,
        )
        # Seed cache
        history._messages = [HumanMessage(content="Hello")]
        msgs = history.messages
        assert len(msgs) == 1
        assert msgs[0].content == "Hello"

    def test_messages_property_returns_copy(self, mock_client):
        """Messages property returns a copy, not the internal list."""
        history = OZChatMessageHistory(
            session_id="session-1",
            project_id="project-1",
            client=mock_client,
        )
        history._messages = [HumanMessage(content="Hello")]
        msgs = history.messages
        msgs.append(AIMessage(content="World"))
        # Internal cache should not have changed
        assert len(history._messages) == 1
