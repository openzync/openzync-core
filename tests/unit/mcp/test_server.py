"""Tests for the FastMCP server — tool registration, dispatch, and handlers.

Uses FastMCP's in-memory ``Client`` to test tools without a running server.
The SDK client is injected via ``mcp._oz_client`` — the lifespan picks it up
and yields it in ``ctx.lifespan_context["client"]`` for tool handlers.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("openzync")

from fastmcp.exceptions import ToolError  # noqa: E402

from services.mcp.server import mcp  # noqa: E402


@pytest.fixture(autouse=True)
def _setup_mock_client() -> None:
    """Attach a mock SDK client via ``mcp._oz_client``.

    The lifespan checks for ``server._oz_client`` first.  By pre-setting it
    here, the lifespan yields ``{"client": mock_client}`` without creating a
    real SDK client.  The lifespan also skips closing it in its ``finally``
    block because no ``OPENZYN_API_KEY`` is set (``created=False``).
    """
    client = MagicMock()
    client.memory = AsyncMock()
    client.facts = AsyncMock()
    client.graph = AsyncMock()
    client.users = AsyncMock()
    client.sessions = AsyncMock()
    mcp._oz_client = client  # type: ignore[attr-defined]
    yield
    mcp._oz_client = None  # type: ignore[attr-defined]


def _get_current_mock() -> Any:
    """Return the mock client currently injected on the server."""
    return mcp._oz_client


class TestToolRegistration:
    """Verify the tool catalog is correctly populated."""

    @pytest.mark.asyncio
    async def test_all_tools_registered(self) -> None:
        """All 9 expected tools should be present in the tool list."""
        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as client:
            tools = await client.list_tools()
            tool_names = {t.name for t in tools}

        expected = {
            "add_memory",
            "get_context",
            "search_memory",
            "delete_memory",
            "add_fact",
            "list_facts",
            "get_user_graph",
            "create_user",
            "list_sessions",
        }
        missing = expected - tool_names
        extra = tool_names - expected
        assert not missing, f"Missing tools: {missing}"
        assert not extra, f"Unexpected tools: {extra}"

    @pytest.mark.asyncio
    async def test_tool_has_description(self) -> None:
        """Every registered tool must have a non-empty description."""
        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as client:
            tools = await client.list_tools()

        for t in tools:
            assert t.description, f"Tool {t.name} has no description"

    @pytest.mark.asyncio
    async def test_tool_has_input_schema(self) -> None:
        """Every registered tool must have an input schema."""
        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as client:
            tools = await client.list_tools()

        for t in tools:
            assert t.inputSchema, f"Tool {t.name} has no input schema"
            assert "type" in t.inputSchema
            assert "properties" in t.inputSchema


class TestToolHandlers:
    """Test individual tool handler logic with mocked SDK responses."""

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _call_result_text(result: Any) -> str:
        """Extract the text content from a CallToolResult."""
        return result.content[0].text

    # ── Memory tools ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_add_memory(self) -> None:
        """add_memory should return a confirmation with episode count."""
        _get_current_mock().memory.ingest.return_value = MagicMock(
            episode_count=2, job_id="job-123"
        )

        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            result = await c.call_tool(
                "add_memory",
                {
                    "project_id": "p1",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )
        text = self._call_result_text(result)
        assert "Memory recorded" in text
        assert "2 messages" in text

    @pytest.mark.asyncio
    async def test_add_memory_empty_messages_raises_error(self) -> None:
        """add_memory should raise on empty messages list."""
        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            with pytest.raises(ToolError):
                await c.call_tool(
                    "add_memory",
                    {"project_id": "p1", "messages": []},
                )

    @pytest.mark.asyncio
    async def test_get_context(self) -> None:
        """get_context should return the context text from the SDK."""
        _get_current_mock().memory.get_context.return_value = MagicMock(
            context="Relevant context about machine learning."
        )

        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            result = await c.call_tool(
                "get_context",
                {"project_id": "p1", "query": "machine learning", "limit": 10},
            )
        assert "Relevant context" in self._call_result_text(result)

    @pytest.mark.asyncio
    async def test_get_context_invalid_limit_raises_error(self) -> None:
        """get_context should raise on limit out of range."""
        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            with pytest.raises(ToolError):
                await c.call_tool(
                    "get_context",
                    {"project_id": "p1", "query": "test", "limit": 0},
                )

    @pytest.mark.asyncio
    async def test_search_memory_with_results(self) -> None:
        """search_memory should format results with scores."""
        _get_current_mock().graph.search.return_value = [
            {"content": "Alice likes hiking", "rrf_score": 0.85, "confidence": 0.9},
            {"content": "Bob prefers cycling", "rrf_score": 0.72, "confidence": 0.8},
        ]

        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            result = await c.call_tool(
                "search_memory",
                {"project_id": "p1", "query": "hobbies", "limit": 20},
            )
        text = self._call_result_text(result)
        assert "2 result(s)" in text
        assert "0.8500" in text
        assert "hiking" in text

    @pytest.mark.asyncio
    async def test_search_memory_no_results(self) -> None:
        """search_memory should return a clear message when empty."""
        _get_current_mock().graph.search.return_value = []

        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            result = await c.call_tool(
                "search_memory",
                {"project_id": "p1", "query": "nonexistent"},
            )
        assert "No results found" in self._call_result_text(result)

    @pytest.mark.asyncio
    async def test_search_memory_invalid_types_raises_error(self) -> None:
        """search_memory should raise on invalid type values."""
        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            with pytest.raises(ToolError):
                await c.call_tool(
                    "search_memory",
                    {"project_id": "p1", "query": "test", "types": "invalid"},
                )

    @pytest.mark.asyncio
    async def test_delete_memory(self) -> None:
        """delete_memory should return a confirmation."""
        _get_current_mock().memory.delete = AsyncMock()

        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            result = await c.call_tool(
                "delete_memory",
                {"project_id": "p1"},
            )
        assert "Memory deleted" in self._call_result_text(result)

    # ── Fact tools ────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_add_fact(self) -> None:
        """add_fact should return a confirmation with accepted count."""
        _get_current_mock().facts.add.return_value = MagicMock(
            accepted_count=3, job_id="fact-job-1"
        )

        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            result = await c.call_tool(
                "add_fact",
                {
                    "project_id": "p1",
                    "facts": [
                        {"subject": "Alice", "predicate": "likes", "object": "hiking"},
                    ],
                },
            )
        assert "3 fact(s) accepted" in self._call_result_text(result)

    @pytest.mark.asyncio
    async def test_add_fact_missing_keys_raises_error(self) -> None:
        """add_fact should raise on facts missing required keys."""
        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            with pytest.raises(ToolError):
                await c.call_tool(
                    "add_fact",
                    {
                        "project_id": "p1",
                        "facts": [{"subject": "Alice"}],  # missing predicate, object
                    },
                )

    @pytest.mark.asyncio
    async def test_add_fact_empty_facts_raises_error(self) -> None:
        """add_fact should raise on empty facts list."""
        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            with pytest.raises(ToolError):
                await c.call_tool(
                    "add_fact",
                    {"project_id": "p1", "facts": []},
                )

    @pytest.mark.asyncio
    async def test_list_facts_with_results(self) -> None:
        """list_facts should format facts with confidence scores."""
        _get_current_mock().graph.search.return_value = [
            {"content": "Alice likes hiking", "confidence": 0.95},
            {"content": "Alice knows Bob", "confidence": 0.87},
        ]

        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            result = await c.call_tool(
                "list_facts",
                {"project_id": "p1", "query": "Alice", "limit": 20},
            )
        text = self._call_result_text(result)
        assert "2 fact(s)" in text
        assert "0.95" in text

    @pytest.mark.asyncio
    async def test_list_facts_no_results(self) -> None:
        """list_facts should return a clear message when empty."""
        _get_current_mock().graph.search.return_value = []

        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            result = await c.call_tool(
                "list_facts",
                {"project_id": "p1", "query": "nonexistent"},
            )
        assert "No facts found" in self._call_result_text(result)

    # ── Graph tool ────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_user_graph_with_entities(self) -> None:
        """get_user_graph should format entities and edges."""
        mock_node = MagicMock()
        mock_node.id = "node-1"
        mock_node.name = "Alice"
        mock_node.type = "Person"
        mock_node.summary = "A person"

        mock_nodes = MagicMock()
        mock_nodes.__aiter__.return_value = [mock_node]
        _get_current_mock().graph.nodes.return_value = mock_nodes

        mock_edge = MagicMock()
        mock_edge.type = "knows"
        mock_edge.source_id = "node-1"
        mock_edge.target_id = "node-2"

        mock_edges = MagicMock()
        mock_edges.__aiter__.return_value = [mock_edge]
        _get_current_mock().graph.edges.return_value = mock_edges

        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            result = await c.call_tool(
                "get_user_graph",
                {"project_id": "p1", "entity_type": "Person", "limit": 50},
            )
        text = self._call_result_text(result)
        assert "1 entity(ies)" in text
        assert "Alice" in text
        assert "knows" in text

    @pytest.mark.asyncio
    async def test_get_user_graph_no_entities(self) -> None:
        """get_user_graph should return a clear message when empty."""
        mock_result = MagicMock()
        mock_result.__aiter__.return_value = []
        _get_current_mock().graph.nodes.return_value = mock_result

        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            result = await c.call_tool(
                "get_user_graph",
                {"project_id": "p1"},
            )
        assert "No entities found" in self._call_result_text(result)

    # ── User tool ─────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_create_user(self) -> None:
        """create_user should return user details."""
        _get_current_mock().users.create.return_value = MagicMock(
            id="usr-1",
            external_id="alice",
            name="Alice",
        )

        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            result = await c.call_tool(
                "create_user",
                {"external_id": "alice", "name": "Alice"},
            )
        text = self._call_result_text(result)
        assert "User created" in text
        assert "usr-1" in text

    @pytest.mark.asyncio
    async def test_create_user_empty_external_id_raises_error(self) -> None:
        """create_user should raise on empty external_id."""
        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            with pytest.raises(ToolError):
                await c.call_tool(
                    "create_user",
                    {"external_id": ""},
                )

    # ── Session tool ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_list_sessions_with_results(self) -> None:
        """list_sessions should format session list."""
        _get_current_mock().sessions.list.return_value = {
            "data": [
                {"id": "s1", "external_id": "chat-1", "message_count": 5},
                {"id": "s2", "external_id": "chat-2", "message_count": 12},
            ],
            "next_cursor": None,
            "has_more": False,
        }

        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            result = await c.call_tool(
                "list_sessions",
                {"project_id": "p1", "limit": 50},
            )
        text = self._call_result_text(result)
        assert "2 session(s)" in text
        assert "chat-1" in text
        assert "5 messages" in text

    @pytest.mark.asyncio
    async def test_list_sessions_with_pagination(self) -> None:
        """list_sessions should include a pagination hint when has_more."""
        _get_current_mock().sessions.list.return_value = {
            "data": [
                {"id": "s1", "external_id": "chat-1", "message_count": 5},
            ],
            "next_cursor": "cursor-abc",
            "has_more": True,
        }

        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            result = await c.call_tool(
                "list_sessions",
                {"project_id": "p1", "limit": 50},
            )
        text = self._call_result_text(result)
        assert "cursor-abc" in text
        assert "More sessions available" in text

    @pytest.mark.asyncio
    async def test_list_sessions_no_results(self) -> None:
        """list_sessions should return a clear message when empty."""
        _get_current_mock().sessions.list.return_value = {
            "data": [],
            "next_cursor": None,
            "has_more": False,
        }

        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            result = await c.call_tool(
                "list_sessions",
                {"project_id": "p1"},
            )
        assert "No sessions found" in self._call_result_text(result)


class TestErrorHandling:
    """Test how the server handles errors."""

    @pytest.mark.asyncio
    async def test_tool_raises_on_missing_required_args(self) -> None:
        """Calling a tool without required args should produce a ToolError."""
        from fastmcp import Client  # noqa: E402

        async with Client(mcp) as c:
            with pytest.raises(ToolError):
                await c.call_tool(
                    "delete_memory",
                    {},  # missing project_id
                )
