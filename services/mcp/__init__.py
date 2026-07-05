"""OpenZync FastMCP Server — exposes memory capabilities as LLM-accessible tools.

Powered by FastMCP (https://gofastmcp.com), the standard framework for
building MCP applications.  Handles JSON-RPC protocol, schema generation,
input validation, and transport negotiation automatically.

Usage:
    python -m services.mcp --transport stdio
    python -m services.mcp --transport http --port 8100
"""

from services.mcp.server import mcp

__all__ = ["mcp"]
