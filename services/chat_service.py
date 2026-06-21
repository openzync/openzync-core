"""Chat service — ReAct loop backed by MCP tools and LLM.

Orchestrates a turn-based conversation where the LLM can invoke any of the
~28 OpenZep MCP tools to fulfill user requests.  Tool calls are dispatched
to the FastMCP server via :class:`OpenZepMCPClient`.

SSE events are yielded as an async generator for streaming to the frontend.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.db import get_db
from core.llm import LLMBackend, ToolCall, resolve_backend
from models.episode import Episode
from services.mcp_client import OpenZepMCPClient
from services.session_service import SessionService

logger = logging.getLogger("openzep.chat")

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_REACT_ITERATIONS = 10
MAX_HISTORY_MESSAGES = 40  # total messages to include (user + assistant)
SYSTEM_PROMPT_NAME = "support_chat_system.jinja2"


# ── SSE event helpers ─────────────────────────────────────────────────────────


def _sse(data: dict) -> str:
    """Format an SSE event."""
    return f"data: {json.dumps(data)}\n\n"


# ── Chat service ──────────────────────────────────────────────────────────────


class ChatService:
    """Handles a single chat turn with ReAct tool-calling loop."""

    def __init__(
        self,
        mcp_client: OpenZepMCPClient,
        session_service: SessionService,
        db: AsyncSession,
        settings: Settings,
    ) -> None:
        self._mcp = mcp_client
        self._session_service = session_service
        self._db = db
        self._settings = settings

    async def chat(
        self,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
        session_id: uuid.UUID,
        message: str,
    ) -> AsyncGenerator[str, None]:
        """Execute a chat turn and yield SSE-encoded events.

        Args:
            user_id: The user UUID.
            org_id: The organization UUID.
            session_id: The session UUID (conversation context).
            message: The user's message text.

        Yields:
            SSE ``data:`` lines with event types:
            ``message_stored``, ``tool_call``, ``tool_result``,
            ``start``, ``token``, ``error``, ``done``.
        """
        # 1. Resolve the LLM backend
        try:
            backend: LLMBackend = await resolve_backend()
        except Exception as exc:
            logger.error("Failed to resolve LLM backend: %s", exc)
            yield _sse({"type": "error", "content": f"LLM not configured: {exc}"})
            return

        # 2. Load MCP tools → LLM function definitions
        try:
            llm_tools = await self._mcp.get_llm_tool_defs()
        except Exception as exc:
            logger.error("Failed to fetch MCP tools: %s", exc)
            yield _sse({"type": "error", "content": f"MCP server unavailable: {exc}"})
            return

        # 3. Load conversation history from DB
        history_response = await self._session_service.get_messages(
            org_id=org_id,
            session_id=session_id,
            limit=MAX_HISTORY_MESSAGES,
            user_id=user_id,
        )

        # 4. Build message list
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _render_system_prompt()},
        ]
        for item in history_response.data if hasattr(history_response, "data") else history_response:
            role = getattr(item, "role", "user")
            content = getattr(item, "content", "")
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})

        # 5. Store user message
        await self._store_episode(user_id, session_id, "user", message)
        yield _sse({"type": "message_stored", "role": "user", "content": message})

        # 6. ReAct loop
        for iteration in range(1, MAX_REACT_ITERATIONS + 1):
            try:
                response = await backend.chat(
                    messages=messages,
                    tools=llm_tools,
                    tool_choice="auto",
                )
            except Exception as exc:
                logger.error("LLM chat error (iter %d): %s", iteration, exc)
                yield _sse({"type": "error", "content": f"LLM error: {exc}"})
                return

            # Handle tool calls
            if response.tool_calls:
                yield _sse({"type": "tool_calls_start", "count": len(response.tool_calls)})

                for tc in response.tool_calls:
                    yield _sse({
                        "type": "tool_call",
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                    })

                    try:
                        result = await self._mcp.call_tool(tc.name, tc.arguments)
                        result_str = json.dumps(result) if not isinstance(result, str) else str(result)
                        # Truncate large results for history
                        if len(result_str) > 5000:
                            result_str = result_str[:5000] + "... (truncated)"
                    except Exception as exc:
                        result_str = f"Error: {exc}"

                    yield _sse({
                        "type": "tool_result",
                        "id": tc.id,
                        "name": tc.name,
                        "content": result_str[:2000],
                    })

                    # Append tool result to message history
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_str,
                    })

                yield _sse({"type": "tool_calls_end"})
                continue  # Continue ReAct loop

            # Final text response — no more tool calls
            final_content = response.content
            yield _sse({"type": "start"})
            yield _sse({"type": "token", "content": final_content})

            # Store assistant response
            await self._store_episode(user_id, session_id, "assistant", final_content)
            yield _sse({"type": "done"})
            return

        # Max iterations exceeded
        yield _sse({"type": "error", "content": "Max iterations exceeded."})

    # ── Internal helpers ───────────────────────────────────────────────────

    async def _store_episode(
        self,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        role: str,
        content: str,
    ) -> None:
        """Persist a message as an Episode in the database."""
        episode = Episode(
            id=uuid.uuid4(),
            user_id=user_id,
            session_id=session_id,
            role=role,
            content=content,
        )
        self._db.add(episode)
        await self._db.flush()

    async def get_or_create_chat_session(
        self,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> uuid.UUID:
        """Return the most recent chat session for a user, or create one.

        Chat sessions use ``__chat__`` as their ``external_id`` sentinel.
        If none exists, a new session is created and returned.
        """
        # Try to find existing chat session
        from models.session import Session

        result = await self._db.execute(
            select(Session).where(
                Session.user_id == user_id,
                Session.external_id == "__chat__",
                Session.is_deleted == False,  # noqa: E712
            ).order_by(Session.created_at.desc()).limit(1)
        )
        session = result.scalar_one_or_none()
        if session is not None:
            return session.id

        # Create new chat session
        session = Session(
            id=uuid.uuid4(),
            user_id=user_id,
            organization_id=org_id,
            external_id="__chat__",
        )
        self._db.add(session)
        await self._db.flush()
        return session.id


# ── System prompt ─────────────────────────────────────────────────────────────


def _render_system_prompt() -> str:
    """Render the chat system prompt.

    For now uses a hardcoded prompt.  Can be migrated to Jinja2 templates
    (``prompts/``) when prompt versioning is needed.
    """
    return (
        "You are an AI assistant for the OpenZep memory platform. "
        "You help users interact with their agent memory system. "
        "You have access to the full OpenZep API through the tools available to you.\n\n"
        "Capabilities:\n"
        "• Manage users (create, update, delete, view summaries)\n"
        "• Manage sessions (create, list, view messages)\n"
        "• Store and retrieve memory\n"
        "• Search across memory with hybrid retrieval\n"
        "• Manage facts and graph entities\n"
        "• View classifications and structured extractions\n\n"
        "When the user asks about specific data, use the appropriate tool to retrieve it. "
        "Be thorough — if a tool returns partial results, use follow-up tools to get more.\n\n"
        "**Rules:**\n"
        "• Read-only operations (list, get, search) do NOT need confirmation.\n"
        "• Destructive operations (delete, wipe) MUST be confirmed with the user first.\n"
        "• If a tool call fails, explain the error to the user and suggest alternatives."
    )
