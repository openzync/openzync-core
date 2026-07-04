"""Stdio transport — read JSON-RPC from stdin, write responses to stdout.

Critical rules:
- Flush stdout after every write (MCP hosts read line-by-line)
- Never write to stdout for logging (use stderr)
- Never use print() — use logging with stderr handler
- One JSON object per line
- Handle stdin EOF gracefully
"""

from __future__ import annotations

import orjson
import logging
import sys

from services.mcp.server import MemGraphMCPServer, PARSE_ERROR, INVALID_REQUEST

logger = logging.getLogger("openzync.mcp.stdio")


def _write_response(response: dict | None) -> None:
    """Write a JSON-RPC response to stdout as a single line.

    Args:
        response: Response dict, or ``None`` for notifications.
    """
    if response is None:
        return
    sys.stdout.write(orjson.dumps(response).decode() + "\n")
    sys.stdout.flush()


def _write_error(req_id: int | str | None, code: int, message: str) -> None:
    """Write a JSON-RPC error response."""
    _write_response({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    })


async def run_stdio_server(server: MemGraphMCPServer) -> None:
    """Read JSON-RPC 2.0 messages from stdin, write responses to stdout.

    Args:
        server: An initialised ``MemGraphMCPServer`` instance.
    """
    logger.info("Starting MCP stdio server")

    while True:
        line = sys.stdin.readline()
        if not line:
            break  # stdin closed — host terminated

        line = line.strip()
        if not line:
            continue

        try:
            request = orjson.loads(line.encode())
        except orjson.JSONDecodeError as e:
            _write_error(None, PARSE_ERROR, f"Parse error: {e}")
            continue

        if not all(k in request for k in ("jsonrpc", "id", "method")):
            _write_error(request.get("id"), INVALID_REQUEST, "Invalid Request: missing jsonrpc/id/method")
            continue

        response = await server.dispatch(request)
        _write_response(response)

    logger.info("MCP stdio server shutting down (stdin closed)")
