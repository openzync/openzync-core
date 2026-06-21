"""SSE transport — HTTP server with SSE endpoint for remote MCP clients.

Endpoints:
    GET  /sse              — SSE stream (server pushes messages)
    POST /messages/{sid}   — Client sends JSON-RPC to server
"""

from __future__ import annotations

import asyncio
import orjson
import logging
import uuid

import aiohttp
from aiohttp import web

from services.mcp.server import MemGraphMCPServer

logger = logging.getLogger("openzep.mcp.sse")


class SSETransport:
    """Manages SSE connections for remote MCP clients.

    Args:
        server: An initialised ``MemGraphMCPServer`` instance.
        host: Host to bind the HTTP server to.
        port: Port to listen on.
    """

    def __init__(
        self,
        server: MemGraphMCPServer,
        host: str = "0.0.0.0",
        port: int = 8100,
    ) -> None:
        self._server = server
        self._host = host
        self._port = port
        self._sessions: dict[str, asyncio.Queue] = {}

    async def run(self) -> None:
        """Start the SSE HTTP server."""
        app = web.Application()
        app["transport"] = self
        app.router.add_get("/sse", self._handle_sse)
        app.router.add_post("/messages/{session_id}", self._handle_message)

        logger.info("Starting MCP SSE server on %s:%s", self._host, self._port)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()

        # Keep running until cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            logger.info("MCP SSE server shutting down")
        finally:
            await runner.cleanup()

    async def _handle_sse(self, request: web.Request) -> web.StreamResponse:
        """Handle SSE connection — opens a stream and sends events."""
        session_id = str(uuid.uuid4())
        queue: asyncio.Queue = asyncio.Queue()
        self._sessions[session_id] = queue

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
            },
        )
        await response.prepare(request)

        # Send the endpoint URL event first
        endpoint_url = f"/messages/{session_id}"
        await response.write(f"event: endpoint\ndata: {endpoint_url}\n\n".encode())

        try:
            while True:
                message = await queue.get()
                if message is None:  # Shutdown signal
                    break
                await response.write(
                    f"event: message\ndata: {orjson.dumps(message).decode()}\n\n".encode()
                )
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            self._sessions.pop(session_id, None)

        return response

    async def _handle_message(self, request: web.Request) -> web.Response:
        """Handle incoming JSON-RPC messages from the client."""
        session_id = request.match_info["session_id"]
        transport: SSETransport = request.app["transport"]

        if session_id not in transport._sessions:
            return web.json_response({"error": "Session not found"}, status=404)

        body = await request.json()

        # Validate required fields
        if not all(k in body for k in ("jsonrpc", "method")):
            return web.json_response(
                {"error": "Invalid Request: missing jsonrpc/method"}, status=400
            )

        # Dispatch and send response back via SSE
        response = await transport._server.dispatch(body)
        if response is not None:
            await transport._sessions[session_id].put(response)

        return web.json_response({"ok": True})
