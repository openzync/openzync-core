"""Tests for OZMemory."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from openzep.integrations.langchain.memory import OZMemory, _messages_to_string


@pytest.fixture
def mock_client():
    """Mock AsyncOpenZep client."""
    client = AsyncMock()
    client.sessions = AsyncMock()
    client.memory = AsyncMock()
    return client


class TestOZMemory:
    """Tests for OZMemory."""

    def test_init(self, mock_client):
        """Initialise with required args."""
        memory = OZMemory(
            session_id="session-1",
            user_id="user-1",
            client=mock_client,
            memory_key="history",
            return_messages=True,
        )
        assert memory.session_id == "session-1"
        assert memory.user_id == "user-1"
        assert memory.memory_key == "history"
        assert memory.return_messages is True

    def test_memory_variables(self, mock_client):
        """memory_variables returns [memory_key]."""
        memory = OZMemory(
            session_id="session-1",
            user_id="user-1",
            client=mock_client,
            memory_key="chat_history",
        )
        assert memory.memory_variables == ["chat_history"]

    def test_load_memory_variables_returns_messages(self, mock_client):
        """load_memory_variables returns the memory_key with messages."""
        memory = OZMemory(
            session_id="session-1",
            user_id="user-1",
            client=mock_client,
            return_messages=True,
        )
        # Seed the chat_memory cache directly
        memory._chat_memory._messages = [
            HumanMessage(content="Hi"),
            AIMessage(content="Hello"),
        ]

        result = memory.load_memory_variables({})
        assert "chat_history" in result
        msgs = result["chat_history"]
        assert len(msgs) == 2
        assert msgs[0].content == "Hi"

    def test_load_memory_variables_returns_string(self, mock_client):
        """When return_messages=False, returns a string."""
        memory = OZMemory(
            session_id="session-1",
            user_id="user-1",
            client=mock_client,
            return_messages=False,
        )
        memory._chat_memory._messages = [
            HumanMessage(content="Hi"),
            AIMessage(content="Hello"),
        ]

        result = memory.load_memory_variables({})
        text = result["chat_history"]
        assert isinstance(text, str)
        assert "HUMAN: Hi" in text
        assert "AI: Hello" in text

    def test_save_context_adds_messages(self, mock_client):
        """save_context adds Human + AI messages."""
        memory = OZMemory(
            session_id="session-1",
            user_id="user-1",
            client=mock_client,
        )
        # Seed cache
        memory._chat_memory._messages = []

        memory.save_context(
            {"input": "What's up?"},
            {"output": "Not much!"},
        )

        assert len(memory._chat_memory._messages) == 2
        assert isinstance(memory._chat_memory._messages[0], HumanMessage)
        assert memory._chat_memory._messages[0].content == "What's up?"
        assert isinstance(memory._chat_memory._messages[1], AIMessage)
        assert memory._chat_memory._messages[1].content == "Not much!"

    def test_save_context_custom_keys(self, mock_client):
        """save_context uses input_key/output_key when specified."""
        memory = OZMemory(
            session_id="session-1",
            user_id="user-1",
            client=mock_client,
            input_key="question",
            output_key="answer",
        )
        memory._chat_memory._messages = []

        memory.save_context(
            {"question": "How are you?"},
            {"answer": "Great!"},
        )

        assert memory._chat_memory._messages[0].content == "How are you?"
        assert memory._chat_memory._messages[1].content == "Great!"

    def test_clear_clears_messages(self, mock_client):
        """clear delegates to chat_memory."""
        memory = OZMemory(
            session_id="session-1",
            user_id="user-1",
            client=mock_client,
        )
        memory._chat_memory._messages = [HumanMessage(content="Hi")]

        # Mock the async delete to avoid actual call
        memory._chat_memory._client.memory.delete = AsyncMock()

        memory.clear()
        assert memory._chat_memory._messages == []

    def test_messages_to_string(self):
        """_messages_to_string formats messages correctly."""
        msgs: list[BaseMessage] = [
            HumanMessage(content="Hello"),
            AIMessage(content="World"),
        ]
        result = _messages_to_string(msgs)
        assert result == "HUMAN: Hello\nAI: World"
