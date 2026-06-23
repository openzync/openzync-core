"""LangChain chat message history backed by OpenZep memory.

Provides ``OZChatMessageHistory``, a ``BaseChatMessageHistory`` implementation
that persists conversation history to OpenZep, making it durable and
searchable across sessions.
"""

from __future__ import annotations

import asyncio
from typing import Any, Sequence

from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

from openzync._errors import NotFoundError
from openzync.client import AsyncOpenZep

# ── Message conversion helpers ──────────────────────────────────────────────

_ROLE_MAP: dict[str, str] = {
    "human": "user",
    "ai": "assistant",
    "system": "system",
}

_REVERSE_ROLE_MAP: dict[str, type[BaseMessage]] = {
    "user": HumanMessage,
    "assistant": AIMessage,
    "system": SystemMessage,
}


def _oz_message_from_base(message: BaseMessage) -> dict[str, Any]:
    """Convert a LangChain ``BaseMessage`` to an OpenZep message dict."""
    return {
        "role": _ROLE_MAP.get(message.type, "user"),
        "content": message.content,
    }


def _base_message_from_oz(msg_dict: dict[str, Any]) -> BaseMessage:
    """Convert an OpenZep message dict to a LangChain ``BaseMessage``."""
    role: str = msg_dict.get("role", "user")
    content: str = msg_dict.get("content", "")
    cls = _REVERSE_ROLE_MAP.get(role, HumanMessage)
    return cls(content=content)


def _run_async(coro: Any) -> Any:
    """Run an async coroutine synchronously via ``asyncio.run()``.

    ⚠️  Not safe inside a running event loop (Jupyter, async apps).
        Use the async methods (``aget_messages``, ``aadd_messages``, etc.)
        in async environments.
    """
    return asyncio.run(coro)


# ── OZChatMessageHistory ────────────────────────────────────────────────────


class OZChatMessageHistory(BaseChatMessageHistory):
    """Chat message history backed by OpenZep.

    Stores conversation history in OpenZep's memory store, making it
    persistent and searchable across sessions.

    .. code-block:: python

        from openzync import AsyncOpenZep
        from openzync.integrations.langchain import OZChatMessageHistory

        client = AsyncOpenZep(api_key="...")
        history = OZChatMessageHistory(
            session_id="session-123",
            project_id="project-abc",
            client=client,
        )
        history.add_user_message("Hi!")
        history.add_ai_message("What can I help with?")

    Args:
        session_id: LangChain conversation identifier.
        project_id: OpenZep project UUID.
        client: An ``AsyncOpenZep`` client instance.
        max_messages: Maximum number of messages to fetch from the server.

    .. note::
        Sync methods use ``asyncio.run()`` internally and are **not safe**
        to call inside an existing event loop (Jupyter, async apps).
        Use async methods (``aget_messages``, ``aadd_messages``, ``aclear``)
        in async environments.
    """

    def __init__(
        self,
        session_id: str,
        project_id: str,
        client: AsyncOpenZep,
        *,
        max_messages: int = 1000,
    ) -> None:
        self.session_id = session_id
        self.project_id = project_id
        self._client = client
        self._max_messages = max_messages
        # None = not loaded; [] = loaded but empty; list = loaded with messages
        self._messages: list[BaseMessage] | None = None

    # ── Internal helpers ────────────────────────────────────────────────

    def _load_messages_if_needed(self) -> None:
        """Fetch messages from the server if the local cache is cold."""
        if self._messages is None:
            self._messages = _run_async(self._fetch_messages())

    async def _fetch_messages(self) -> list[BaseMessage]:
        """Fetch messages from the OpenZep server for the given session."""
        try:
            resp = await self._client.sessions.messages(
                self.project_id,
                self.session_id,
                limit=self._max_messages,
            )
            return [_base_message_from_oz(m.model_dump()) for m in resp.data]
        except NotFoundError:
            # No session yet — no messages
            return []

    # ── Sync interface (primitive) ──────────────────────────────────────

    @property
    def messages(self) -> list[BaseMessage]:
        self._load_messages_if_needed()
        # Return a copy to prevent external mutation of the cache
        return list(self._messages or [])

    def add_message(self, message: BaseMessage) -> None:
        self._load_messages_if_needed()
        if self._messages is not None:
            self._messages.append(message)
        _run_async(
            self._client.memory.ingest(
                self.project_id,
                messages=[_oz_message_from_base(message)],
                session_id=self.session_id,
            )
        )

    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        self._load_messages_if_needed()
        if self._messages is not None:
            self._messages.extend(messages)
        _run_async(
            self._client.memory.ingest(
                self.project_id,
                messages=[_oz_message_from_base(m) for m in messages],
                session_id=self.session_id,
            )
        )

    def clear(self) -> None:
        self._messages = []
        _run_async(self._client.memory.delete(self.project_id))

    # ── Async interface (primitive) ─────────────────────────────────────

    async def aget_messages(self) -> list[BaseMessage]:
        if self._messages is None:
            self._messages = await self._fetch_messages()
        return list(self._messages or [])

    async def aadd_messages(self, messages: Sequence[BaseMessage]) -> None:
        # Warm cache if cold
        if self._messages is None:
            self._messages = await self._fetch_messages()
        # Extend local cache
        if self._messages is not None:
            self._messages.extend(messages)
        # Persist to server
        await self._client.memory.ingest(
            self.project_id,
            messages=[_oz_message_from_base(m) for m in messages],
            session_id=self.session_id,
        )

    async def aclear(self) -> None:
        self._messages = []
        await self._client.memory.delete(self.project_id)
