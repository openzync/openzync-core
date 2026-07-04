"""Tests for the MCP server — protocol handling, dispatch, tools."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from services.mcp.server import MemGraphMCPServer, ToolDef, PARSE_ERROR, INVALID_REQUEST
from openzync.client import AsyncOpenZync


@pytest.fixture
def mock_client() -> AsyncOpenZync:
    """Create a mock AsyncOpenZync client."""
    client = MagicMock(spec=AsyncOpenZync)
    client.memory = AsyncMock()
    client.facts = AsyncMock()
    client.graph = AsyncMock()
    client.users = AsyncMock()
    client.sessions = AsyncMock()
    return client


@pytest.fixture
def server(mock_client: AsyncOpenZync) -> MemGraphMCPServer:
    return MemGraphMCPServer(mock_client)


class TestProtocol:
    """Tests for MCP protocol methods."""

    @pytest.mark.asyncio
    async def test_initialize(self, server: MemGraphMCPServer):
        resp = await server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        })
        assert resp["result"]["protocolVersion"] == "2024-11-05"
        assert "tools" in resp["result"]["capabilities"]

    @pytest.mark.asyncio
    async def test_notification_returns_none(self, server: MemGraphMCPServer):
        resp = await server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "notifications/initialized",
        })
        assert resp is None

    @pytest.mark.asyncio
    async def test_tools_list(self, server: MemGraphMCPServer):
        resp = await server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        })
        tools = resp["result"]["tools"]
        names = [t["name"] for t in tools]
        assert "add_memory" in names
        assert "get_context" in names
        assert "add_fact" in names
        assert "create_user" in names
        assert "list_sessions" in names

    @pytest.mark.asyncio
    async def test_unknown_method(self, server: MemGraphMCPServer):
        resp = await server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "unknown",
        })
        assert resp["error"]["code"] == -32601

    @pytest.mark.asyncio
    async def test_unknown_tool(self, server: MemGraphMCPServer):
        resp = await server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "nonexistent"},
        })
        assert resp["error"]["code"] == -32601

    @pytest.mark.asyncio
    async def test_missing_tool_name(self, server: MemGraphMCPServer):
        resp = await server.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {},
        })
        assert resp["error"]["code"] == -32602


class TestToolHandlers:
    """Tests for individual tool handlers."""

    @pytest.mark.asyncio
    async def test_add_memory(self, server: MemGraphMCPServer, mock_client: AsyncOpenZync):
        mock_client.memory.ingest.return_value = MagicMock(
            episode_count=2, job_id="j1"
        )
        resp = await server.dispatch({
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_memory",
                "arguments": {
                    "user_id": "u1",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            },
        })
        assert not resp["result"]["isError"]
        assert "Memory recorded" in resp["result"]["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_create_user(self, server: MemGraphMCPServer, mock_client: AsyncOpenZync):
        mock_client.users.create.return_value = MagicMock(
            id="u1", external_id="alice", name="Alice"
        )
        resp = await server.dispatch({
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {
                "name": "create_user",
                "arguments": {"external_id": "alice", "name": "Alice"},
            },
        })
        assert not resp["result"]["isError"]
        assert "User created" in resp["result"]["content"][0]["text"]


class TestCustomToolRegistration:
    """Tests for custom tool registration."""

    @pytest.mark.asyncio
    async def test_register_custom_tool(self, server: MemGraphMCPServer):
        async def custom_handler(client, args):
            return {"content": [{"type": "text", "text": f"Hello {args['name']}"}]}

        server.register_tool(ToolDef(
            name="greet",
            description="Greet someone",
            input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
            handler=custom_handler,
        ))

        resp = await server.dispatch({
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"name": "greet", "arguments": {"name": "World"}},
        })
        assert "Hello World" in resp["result"]["content"][0]["text"]
