"""Entry point for the MCP server.

Usage:
    python -m services.mcp --transport stdio
    python -m services.mcp --transport sse --port 8100
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys


def main() -> None:
    """Run the MCP server with the specified transport."""
    parser = argparse.ArgumentParser(description="OpenZep MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="SSE server host")
    parser.add_argument("--port", type=int, default=8100, help="SSE server port")
    parser.add_argument("--api-key", help="OpenZep API key (default: OPENZEP_API_KEY env var)")
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="OpenZep API base URL (default: http://localhost:8000)",
    )
    args = parser.parse_args()

    # Configure logging to stderr only
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("openzep.mcp")

    api_key = args.api_key or os.environ.get("OPENZEP_API_KEY")
    if not api_key:
        logger.error("API key required. Set --api-key or OPENZEP_API_KEY env var.")
        sys.exit(1)

    # Import here to avoid loading SDK at module level
    from openzep.client import AsyncOpenZep, OpenZep

    from services.mcp.server import MemGraphMCPServer

    # Use async client for the server (all handlers are async)
    async_client = AsyncOpenZep(api_key=api_key, base_url=args.base_url)
    server = MemGraphMCPServer(async_client)

    logger.info(
        "Starting MCP server (transport=%s, base_url=%s)",
        args.transport,
        args.base_url,
    )

    if args.transport == "sse":
        from services.mcp.transport.sse import SSETransport

        asyncio.run(SSETransport(server, host=args.host, port=args.port).run())
    else:
        from services.mcp.transport.stdio import run_stdio_server

        asyncio.run(run_stdio_server(server))


if __name__ == "__main__":
    main()
