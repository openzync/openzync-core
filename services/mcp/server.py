"""FastMCP server — exposes OpenZep REST endpoints as MCP tools.

Auto-discovers tools from FastAPI route definitions at startup.

Usage:
    # Start standalone MCP server:
    python -m services.mcp --api-key sk-xxx --base-url http://localhost:8000

    # Or as a module:
    python -m services.mcp
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP

from services.mcp._handler_factory import make_handler
from services.mcp.discovery import discover_tools

logger = logging.getLogger("openzep.mcp.server")

# Module-level FastMCP instance — created at import time, tools registered
# during ``bootstrap()``.
mcp = FastMCP(
    "OpenZep",
    instructions=(
        "OpenZep agent memory platform.  Use these tools to manage users, "
        "sessions, memory, facts, graph entities, classifications, and "
        "structured extractions.  Every tool maps to a REST endpoint on "
        "the OpenZep API."
    ),
)


def bootstrap(api_base: str, api_key: str) -> None:
    """Discover all API routes and register each as an MCP tool.

    Args:
        api_base: Base URL for the OpenZep REST API
            (e.g. ``http://localhost:8000``).
        api_key: OpenZep API key for ``Authorization: Bearer`` header.
    """
    tools = discover_tools()
    registered = 0

    for tool_def in tools:
        try:
            handler = make_handler(tool_def, api_base=api_base, api_key=api_key)
            mcp.add_tool(handler)
            registered += 1
        except Exception:
            logger.exception("Failed to register tool %s", tool_def.name)

    logger.info("Registered %d MCP tools", registered, extra={"total": len(tools)})


if __name__ == "__main__":
    import os
    import sys

    import structlog

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    structlog.configure(
        processors=[structlog.dev.ConsoleRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )

    api_key = os.environ.get("OPENZEP_API_KEY")
    if not api_key:
        print("FATAL: OPENZEP_API_KEY environment variable is required.", file=sys.stderr)
        sys.exit(1)

    api_base = os.environ.get("OPENZEP_API_URL", "http://localhost:8000")
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8100"))

    bootstrap(api_base=api_base, api_key=api_key)
    logger.info("Starting OpenZep MCP server on %s:%s …", host, port)
    mcp.run(transport="http", host=host, port=port)
