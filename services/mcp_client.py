"""FastMCP client wrapper for the OpenZep backend.

Connects to the standalone FastMCP MCP server over HTTP and provides
a clean async interface for tool invocation and LLM function-definition
conversion.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import Client

logger = logging.getLogger("openzep.mcp_client")


class OpenZepMCPClient:
    """Thin wrapper around :class:`fastmcp.Client`.

    Manages the connection lifecycle and provides helpers to convert MCP
    tool definitions to the format expected by LLM function-calling APIs.

    Usage::

        client = OpenZepMCPClient("http://localhost:8100/mcp")
        await client.start()
        try:
            tools = await client.get_llm_tool_defs()
            result = await client.call_tool("create_user", {"external_id": "abc"})
        finally:
            await client.stop()
    """

    def __init__(self, url: str = "http://localhost:8100/mcp") -> None:
        """Initialise the client.

        Args:
            url: URL of the running FastMCP server's HTTP endpoint
                (e.g. ``http://localhost:8100/mcp``).
        """
        self._url = url
        self._client: Client | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect to the MCP server and initialise the session."""
        self._client = Client(self._url)
        await self._client.__aenter__()
        logger.info("Connected to MCP server at %s", self._url)

    async def stop(self) -> None:
        """Disconnect from the MCP server."""
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None
            logger.info("Disconnected from MCP server")

    async def __aenter__(self) -> OpenZepMCPClient:
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.stop()

    # ── Tool operations ──────────────────────────────────────────────────────

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Invoke an MCP tool and return its result data.

        Args:
            name: Tool name (e.g. ``"create_user"``).
            arguments: Tool arguments as a dict.

        Returns:
            The tool's result data (auto-deserialised by FastMCP).

        Raises:
            RuntimeError: If the client is not connected.
        """
        if self._client is None:
            raise RuntimeError("MCP client is not connected — call start() first")
        result = await self._client.call_tool(name, arguments or {})
        return result.data

    async def list_tools(self) -> list:
        """List all available tools from the MCP server.

        Returns:
            A list of :class:`fastmcp.Tool` objects, each with ``name``,
            ``description``, ``inputSchema``, etc.
        """
        if self._client is None:
            raise RuntimeError("MCP client is not connected — call start() first")
        return await self._client.list_tools()

    async def get_llm_tool_defs(self) -> list[dict[str, Any]]:
        """Convert MCP tool definitions to LLM function-calling format.

        Returns a list of dicts in the format expected by OpenAI,
        Anthropic, and compatible LLM providers::

            [
                {
                    "type": "function",
                    "function": {
                        "name": "create_user",
                        "description": "...",
                        "parameters": {"type": "object", ...},
                    },
                },
                ...
            ]
        """
        tools = await self.list_tools()
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.inputSchema,
                },
            }
            for t in tools
        ]
