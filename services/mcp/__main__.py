"""Entry point for the FastMCP MCP server.

Usage:
    python -m services.mcp

Environment variables:
    OPENZEP_API_KEY    — API key for the OpenZep REST API (required)
    OPENZEP_API_URL    — Base URL for the API (default: http://localhost:8000)
    MCP_HOST           — HTTP bind host (default: 0.0.0.0)
    MCP_PORT           — HTTP bind port (default: 8100)
"""

from __future__ import annotations

from services.mcp.server import bootstrap, mcp

if __name__ == "__main__":
    import os
    import sys

    api_key = os.environ.get("OPENZEP_API_KEY")
    if not api_key:
        print("FATAL: OPENZEP_API_KEY environment variable is required.", file=sys.stderr)
        sys.exit(1)

    api_base = os.environ.get("OPENZEP_API_URL", "http://localhost:8000")
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8100"))

    bootstrap(api_base=api_base, api_key=api_key)
    print(f"Starting OpenZep MCP server on {host}:{port} …", file=sys.stderr)
    mcp.run(transport="http", host=host, port=port)
