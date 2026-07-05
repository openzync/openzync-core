"""Entry point for the OpenZync FastMCP server.

Usage:
    python -m services.mcp --transport stdio
    python -m services.mcp --transport http --port 8100
    python -m services.mcp --transport sse --port 8100

The server connects to the OpenZync API via the Python SDK (``AsyncOpenZync``)
and exposes its capabilities as MCP tools over the chosen transport.

FastMCP handles protocol compliance, schema generation, input validation,
and transport negotiation automatically — no custom JSON-RPC dispatch needed.

The SDK client is created during the FastMCP lifespan, which reads the
``OPENZYN_API_KEY`` and ``OPENZYN_BASE_URL`` environment variables.
This entry point sets them from CLI arguments before starting the server.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys


def main() -> None:
    """Run the MCP server with the specified transport and configuration."""
    parser = argparse.ArgumentParser(description="OpenZync FastMCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "http"],
        default="stdio",
        help="Transport protocol (default: stdio).  Use 'http' for Streamable HTTP.",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Server bind host")  # noqa: S104 — intentional for server
    parser.add_argument("--port", type=int, default=8100, help="Server port")
    parser.add_argument(
        "--api-key",
        help="OpenZync API key (default: OPENZYN_API_KEY env var)",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="OpenZync API base URL (default: http://localhost:8000)",
    )
    args = parser.parse_args()

    # Configure logging to stderr (stdout is reserved for stdio transport)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("openzync.mcp")

    api_key = args.api_key or os.environ.get("OPENZYN_API_KEY")
    if not api_key:
        logger.error(
            "API key is required.  Pass --api-key or set the OPENZYN_API_KEY "
            "environment variable."
        )
        sys.exit(1)

    # Set environment variables for the FastMCP lifespan to read.
    os.environ["OPENZYN_API_KEY"] = api_key
    os.environ["OPENZYN_BASE_URL"] = args.base_url

    # Import the FastMCP server (triggers tool registration).
    from services.mcp.server import mcp

    logger.info(
        "Starting MCP server (transport=%s, base_url=%s)",
        args.transport,
        args.base_url,
    )

    if args.transport == "stdio":
        mcp.run()
    elif args.transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port)
    elif args.transport == "sse":
        # SSE transport — legacy, kept for compatibility
        mcp.run(transport="sse", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
