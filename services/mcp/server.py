"""MCP protocol server — JSON-RPC 2.0 dispatch, tool registry, protocol handlers.

Exposes OpenZync as LLM-accessible tools via the Model Context Protocol.
Works over any transport (stdio, SSE).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openzync.client import AsyncOpenZync

logger = logging.getLogger("openzync.mcp")

# ── JSON-RPC error codes ────────────────────────────────────────────────

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolDef:
    """Definition of a single MCP tool."""

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: Callable[..., Coroutine[Any, Any, dict]],
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler


class MemGraphMCPServer:
    """MCP protocol server exposing OpenZync as LLM-accessible tools.

    Args:
        client: An initialised ``AsyncOpenZync`` client instance.
    """

    def __init__(self, client: AsyncOpenZync) -> None:
        self._client = client
        self._tools: dict[str, ToolDef] = {}
        self._register_default_tools()

    # ── Registration ────────────────────────────────────────────────────────

    def _register_default_tools(self) -> None:
        """Register all built-in tool handlers."""
        from services.mcp.tools.memory import (
            handle_add_memory,
            handle_get_context,
            handle_search_memory,
            handle_delete_memory,
        )
        from services.mcp.tools.facts import handle_add_fact, handle_list_facts
        from services.mcp.tools.graph import handle_get_user_graph
        from services.mcp.tools.users import handle_create_user
        from services.mcp.tools.sessions import handle_list_sessions

        self.register_tool(ToolDef(
            name="add_memory",
            description="Add messages to a user's memory. Messages are persisted immediately and queued "
                        "for async entity extraction, fact extraction, and embedding.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "Unique identifier for the user."},
                    "messages": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string", "enum": ["user", "assistant", "system", "tool"]},
                                "content": {"type": "string", "description": "Message body text."},
                            },
                            "required": ["role", "content"],
                        },
                        "minItems": 1,
                    },
                    "session_id": {"type": "string", "description": "Optional session external ID."},
                },
                "required": ["user_id", "messages"],
            },
            handler=handle_add_memory,
        ))
        self.register_tool(ToolDef(
            name="get_context",
            description="Assemble a context block for LLM injection from a natural-language query. "
                        "Returns recent episodes, extracted facts, and graph entities.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "query": {"type": "string", "description": "Natural-language query describing needed context."},
                    "limit": {"type": "integer", "default": 20, "maximum": 100},
                },
                "required": ["user_id", "query"],
            },
            handler=handle_get_context,
        ))
        self.register_tool(ToolDef(
            name="search_memory",
            description="Search across a user's memory using hybrid retrieval (BM25 + vector). "
                        "Returns episodes, facts, and optionally entities matching the query.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "query": {"type": "string", "description": "Search query string."},
                    "types": {"type": "string", "default": "episodes,facts",
                              "description": "Comma-separated: episodes, facts, entities"},
                    "limit": {"type": "integer", "default": 20, "maximum": 100},
                },
                "required": ["user_id", "query"],
            },
            handler=handle_search_memory,
        ))
        self.register_tool(ToolDef(
            name="delete_memory",
            description="Delete all memory (episodes + facts) for a user. This is the GDPR memory-wipe operation.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                },
                "required": ["user_id"],
            },
            handler=handle_delete_memory,
        ))
        self.register_tool(ToolDef(
            name="add_fact",
            description="Add business fact triples (subject-predicate-object) to a user's knowledge graph. "
                        "Maximum 500 triples per call.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "facts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "subject": {"type": "string"},
                                "predicate": {"type": "string"},
                                "object": {"type": "string"},
                                "confidence": {"type": "number", "default": 1.0},
                            },
                            "required": ["subject", "predicate", "object"],
                        },
                        "minItems": 1,
                        "maxItems": 500,
                    },
                    "session_id": {"type": "string", "description": "Optional session external ID."},
                },
                "required": ["user_id", "facts"],
            },
            handler=handle_add_fact,
        ))
        self.register_tool(ToolDef(
            name="list_facts",
            description="Search facts (knowledge triples) by keyword query.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "query": {"type": "string", "description": "Keyword search query."},
                    "limit": {"type": "integer", "default": 20, "maximum": 100},
                },
                "required": ["user_id", "query"],
            },
            handler=handle_list_facts,
        ))
        self.register_tool(ToolDef(
            name="get_user_graph",
            description="Get the entity graph for a user. Returns nodes (entities) and edges (relationships). "
                        "Optionally filter by entity type.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "entity_type": {"type": "string", "description": "Optional filter: Person, Organization, etc."},
                    "limit": {"type": "integer", "default": 50, "maximum": 200},
                },
                "required": ["user_id"],
            },
            handler=handle_get_user_graph,
        ))
        self.register_tool(ToolDef(
            name="create_user",
            description="Create a new user in the system.",
            input_schema={
                "type": "object",
                "properties": {
                    "external_id": {"type": "string", "description": "Caller-defined user identifier."},
                    "name": {"type": "string", "description": "Optional display name."},
                },
                "required": ["external_id"],
            },
            handler=handle_create_user,
        ))
        self.register_tool(ToolDef(
            name="list_sessions",
            description="List sessions for a user.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 50, "maximum": 200},
                },
                "required": ["user_id"],
            },
            handler=handle_list_sessions,
        ))

    def register_tool(self, tool: ToolDef) -> None:
        """Register a tool definition."""
        self._tools[tool.name] = tool

    # ── Protocol handlers ───────────────────────────────────────────────────

    def get_tool_list(self) -> list[dict]:
        """Return the ``tools/list`` response."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    async def dispatch(self, request: dict) -> dict | None:
        """Dispatch a JSON-RPC 2.0 request to the appropriate handler.

        Args:
            request: Parsed JSON-RPC 2.0 request dict.

        Returns:
            JSON-RPC 2.0 response dict, or ``None`` for notifications.
        """
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params", {})

        try:
            if method == "initialize":
                return self._handle_initialize(req_id, params)
            elif method == "notifications/initialized":
                return None
            elif method == "tools/list":
                return self._make_result(req_id, {"tools": self.get_tool_list()})
            elif method == "tools/call":
                return await self._handle_tool_call(req_id, params)
            elif method == "resources/list":
                return self._make_result(req_id, {"resources": []})
            else:
                return self._make_error(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        except Exception as e:
            logger.exception("Unhandled error dispatching request %s", method)
            return self._make_error(req_id, INTERNAL_ERROR, str(e))

    async def _handle_tool_call(self, req_id: int | str | None, params: dict) -> dict:
        """Handle a ``tools/call`` request."""
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if not tool_name:
            return self._make_error(req_id, INVALID_PARAMS, "Missing tool name")

        tool = self._tools.get(tool_name)
        if not tool:
            return self._make_error(req_id, METHOD_NOT_FOUND, f"Unknown tool: {tool_name}")

        try:
            result = await tool.handler(self._client, arguments)
            return self._make_result(req_id, result)
        except ValueError as e:
            return self._make_error(req_id, INVALID_PARAMS, str(e))
        except Exception as e:
            logger.error("Tool %s failed: %s", tool_name, e)
            return self._make_error(req_id, INTERNAL_ERROR, f"Tool {tool_name} failed: {e}")

    def _handle_initialize(self, req_id: int | str | None, params: dict) -> dict:  # noqa: ARG002
        """Handle protocol initialization handshake."""
        return self._make_result(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
                "resources": {},
            },
            "serverInfo": {
                "name": "OpenZync-mcp",
                "version": "0.1.0",
            },
        })

    # ── Response builders ──────────────────────────────────────────────────

    def _make_result(self, req_id: int | str | None, result: dict) -> dict:
        if req_id is None:
            return {}
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _make_error(self, req_id: int | str | None, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
