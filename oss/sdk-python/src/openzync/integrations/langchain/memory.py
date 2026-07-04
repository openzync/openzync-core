"""LangChain memory backed by OpenZync.

Provides ``OZMemory``, a ``BaseChatMemory`` implementation that persists
conversation context to OpenZync. Designed to be a drop-in replacement for
``ConversationBufferMemory`` in LangChain chains.
"""

from __future__ import annotations

from typing import Any

from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.memory import BaseMemory
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, Field

from openzync.integrations.langchain.message_history import OZChatMessageHistory
from openzync.models.memory import ContextResponse


class OZMemory(BaseMemory):
    """LangChain memory backed by OpenZync.

    Wraps ``OZChatMessageHistory`` to integrate with LangChain's memory
    system, enabling persistent conversation history in chains.

    .. code-block:: python

        from langchain.chains import ConversationChain
        from openzync import AsyncOpenZync
        from openzync.integrations.langchain import OZMemory

        client = AsyncOpenZync(api_key="...")
        memory = OZMemory(
            session_id="session-123",
            project_id="project-abc",
            client=client,
            memory_key="chat_history",
            return_messages=True,
        )
        chain = ConversationChain(llm=llm, memory=memory)

    Args:
        session_id: LangChain conversation / session identifier.
        project_id: OpenZync project UUID.
        client: An ``AsyncOpenZync`` client instance.
        memory_key: Key under which memory variables are stored (default
            ``"chat_history"``).
        return_messages: If ``True``, returns a list of ``BaseMessage``;
            if ``False``, returns a string.
        input_key: Optional key for the input variable. Auto-detected if
            not provided.
        output_key: Optional key for the output variable. Auto-detected if
            not provided.
        max_messages: Maximum number of messages to fetch from the server.
    """

    session_id: str
    project_id: str
    client: Any  # typed as Any to avoid import issues — must be AsyncOpenZync
    memory_key: str = "chat_history"
    return_messages: bool = True
    input_key: str | None = None
    output_key: str | None = None
    max_messages: int = 1000

    # Internal — populated in model_post_init
    _chat_memory: OZChatMessageHistory | None = None

    def model_post_init(self, __context: Any) -> None:
        """Create the underlying chat message history after init."""
        self._chat_memory = OZChatMessageHistory(
            session_id=self.session_id,
            project_id=self.project_id,
            client=self.client,
            max_messages=self.max_messages,
        )

    @property
    def chat_memory(self) -> BaseChatMessageHistory:
        """Return the underlying chat message history.

        Raises:
            ValueError: If ``model_post_init`` was not called.
        """
        if self._chat_memory is None:
            msg = "OZMemory not initialized. Use OZMemory(...) constructor."
            raise ValueError(msg)
        return self._chat_memory

    @property
    def memory_variables(self) -> list[str]:
        """Return the keys this memory produces."""
        return [self.memory_key]

    def load_memory_variables(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Load memory variables — returns the conversation history.

        Args:
            inputs: Inputs to the chain (unused, retained for API compat).

        Returns:
            Dict mapping ``memory_key`` to the conversation history (either
            a list of ``BaseMessage`` or a string, depending on
            ``return_messages``).
        """
        messages = self.chat_memory.messages
        if self.return_messages:
            return {self.memory_key: messages}
        return {self.memory_key: _messages_to_string(messages)}

    def save_context(
        self,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
    ) -> None:
        """Save the context of a conversation turn to OpenZync.

        Extracts the input and output messages and persists them.

        Args:
            inputs: Dict containing the input key.
            outputs: Dict containing the output key.
        """
        input_str = self._get_input(inputs)
        output_str = self._get_output(outputs)
        self.chat_memory.add_messages(
            [HumanMessage(content=input_str), AIMessage(content=output_str)]
        )

    def clear(self) -> None:
        """Clear all persisted memory."""
        self.chat_memory.clear()

    async def get_context(self, query: str, limit: int = 10) -> ContextResponse:
        """Retrieve relevant context from memory for LLM injection.

        Calls the OpenZync server-side context endpoint, which assembles
        a formatted context block from recent conversation history and
        semantic search results.

        Args:
            query: Natural-language query describing the context needed.
            limit: Maximum results per source type.

        Returns:
            ``ContextResponse`` with a ``context`` string suitable for
            use as a system-prompt prefix.
        """
        return await self.client.memory.get_context(
            self.project_id,
            query=query,
            limit=limit,
        )

    # ── Private helpers ─────────────────────────────────────────────────

    def _get_input(self, inputs: dict[str, Any]) -> str:
        if self.input_key is not None:
            return str(inputs[self.input_key])
        # Auto-detect: find the key that is not the memory key or output key
        for key in inputs:
            if key != self.memory_key:
                return str(inputs[key])
        return str(list(inputs.values())[0])

    def _get_output(self, outputs: dict[str, Any]) -> str:
        if self.output_key is not None:
            return str(outputs[self.output_key])
        # Auto-detect: use the first output value
        for key in outputs:
            if key != self.memory_key:
                return str(outputs[key])
        return str(list(outputs.values())[0])


def _messages_to_string(messages: list[BaseMessage]) -> str:
    """Convert a list of messages to a single string."""
    parts: list[str] = []
    for msg in messages:
        prefix = msg.type.upper() if msg.type else "MESSAGE"
        parts.append(f"{prefix}: {msg.content}")
    return "\n".join(parts)
