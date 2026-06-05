# MCP Server Setup — Implementation Guide

> **Domain:** MCP Server
> **SRS Phase:** Phase 2 — Full Feature Parity (Week 5-7)
> **Requirements:** MCP-01, MCP-02, MCP-03, MCP-04
> **Doc Dependencies:** [01-api-key-auth.md](../02-auth-tenancy/01-api-key-auth.md), [03-core-memory/01-message-ingestion.md](../03-core-memory/01-message-ingestion.md), [07-user-session-mgmt/01-user-crud.md](../07-user-session-mgmt/01-user-crud.md), [09-sdks/01-shared-patterns.md](../09-sdks/01-shared-patterns.md)

---

## 1. Overview

The MCP (Model Context Protocol) server exposes MemGraph's core memory capabilities as **tools** that LLM agents (Claude Desktop, Cursor, custom agents) can invoke directly. Instead of calling REST endpoints, the agent calls named tools through JSON-RPC 2.0 messages over stdio or SSE transport.

### 1.1 Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Pure Python (minimal deps)** | Only requires `mcp` or `fastmcp` package — avoids heavy framework dependencies. Chosen over FastMCP because FastMCP adds abstraction layers that make debug harder. We use the low-level `mcp` package. |
| **Stdio as primary transport** | Required by Claude Desktop and Cursor. Zero network config — just `command` and `args` in the MCP config. |
| **SSE as secondary transport** | Enables remote MCP clients (e.g., a browser-based agent) that cannot run a local Python process. Serves HTTP with an SSE endpoint. |
| **Auth via injected MemGraphClient** | Every tool call delegates to an injected MemGraph REST client (same as SDK). No separate auth logic in the MCP server — auth is the API key in the client. |
| **One tool = one REST call** | No compound tools. Each MCP tool maps 1:1 to a REST endpoint for simplicity and debuggability. |

### 1.2 Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    LLM Agent (Claude, Cursor)                │
│  Calls tool via JSON-RPC over stdio or SSE                   │
└─────────────────────┬───────────────────────────────────────┘
                      │ JSON-RPC 2.0
┌─────────────────────▼───────────────────────────────────────┐
│                  MemGraphMCPServer                           │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Transport Layer (stdio or SSE)                       │  │
│  │  Parses JSON-RPC 2.0 request → dispatches to handler  │  │
│  └──────────────────────┬─────────────────────────────────┘  │
│                         │                                     │
│  ┌──────────────────────▼─────────────────────────────────┐  │
│  │  Tool Registry                                        │  │
│  │  add_memory / get_context / search_memory / ...       │  │
│  │  Each tool: validate input → call service → format    │  │
│  └──────────────────────┬─────────────────────────────────┘  │
│                         │                                     │
│  ┌──────────────────────▼─────────────────────────────────┐  │
│  │  MemGraphClient (injected, same as Python SDK)        │  │
│  │  api_key + base_url → HTTP calls to FastAPI backend   │  │
│  └────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. MCP Protocol Overview

MCP uses **JSON-RPC 2.0** over a bidirectional transport. Every message is a JSON object with `jsonrpc`, `id`, `method`, and `params` fields.

### 2.1 Request Format

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "add_memory",
    "arguments": {
      "user_id": "user_abc",
      "messages": [
        {"role": "user", "content": "Hello!"}
      ]
    }
  }
}
```

### 2.2 Response Format (Success)

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "Memory recorded. 1 messages ingested."
      }
    ],
    "isError": false
  }
}
```

### 2.3 Response Format (Error)

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32603,
    "message": "Internal error",
    "data": "Failed to ingest memory: REST API returned 500"
  }
}
```

### 2.4 MCP Protocol Methods

The MCP server must implement these protocol methods:

| Method | Description | Implemented |
|--------|-------------|-------------|
| `initialize` | Protocol handshake — exchange capabilities | Yes |
| `notifications/initialized` | Client confirms initialization | Yes |
| `tools/list` | Return list of all available tools | Yes |
| `tools/call` | Execute a specific tool by name | Yes |
| `resources/list` | Return list of available resources | Optional |
| `resources/read` | Read a specific resource by URI | Optional |

---

## 3. Stdio Transport

Stdio transport is the **default and primary transport**. The MCP server reads JSON-RPC requests from `stdin` and writes responses to `stdout`. Stderr is reserved for logging — the MCP host redirects it to the host's log stream.

### 3.1 How It Works

```python
import sys
import json

async def run_stdio_server(server: "MemGraphMCPServer") -> None:
    """Read JSON-RPC 2.0 messages from stdin, write responses to stdout."""
    # Each message is a single line of JSON, terminated by \n
    # Responses go to stdout as single lines
    # Logs go to stderr (visible in the host's log output)
    while True:
        line = sys.stdin.readline()
        if not line:
            break  # stdin closed — host terminated

        line = line.strip()
        if not line:
            continue  # skip empty lines (e.g., from newlines after messages)

        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            _write_error(None, -32700, f"Parse error: {e}")
            continue

        # Must have jsonrpc, id, method
        if not all(k in request for k in ("jsonrpc", "id", "method")):
            _write_error(request.get("id"), -32600, "Invalid Request")
            continue

        # Dispatch
        response = await server.dispatch(request)
        _write_response(response)
```

### 3.2 Response Writer

```python
def _write_response(response: dict) -> None:
    """Write a JSON-RPC response to stdout as a single line."""
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()  # Must flush — MCP host reads line-by-line

def _write_error(request_id: int | str | None, code: int, message: str) -> None:
    """Write a JSON-RPC error response."""
    response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    _write_response(response)
```

### 3.3 Critical Stdio Rules

| Rule | Why |
|------|-----|
| **Flush stdout after every write** | MCP hosts read line-by-line; buffered output causes deadlock |
| **Never write to stdout for logging** | Use `stderr` only — stdout is the protocol channel |
| **Never use `print()`** | `print()` writes to stdout. Use `logging` with stderr handler |
| **One JSON object per line** | Never pretty-print JSON over the transport |
| **Handle stdin EOF gracefully** | Clean up connections and exit — don't crash |

```python
import logging

# Configure logger to write to stderr only
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger("memgraph.mcp")
```

---

## 4. SSE Transport

SSE (Server-Sent Events) transport enables remote MCP clients that cannot run a local Python process. The server runs a lightweight HTTP server with two endpoints.

### 4.1 Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/sse` | SSE stream — server pushes JSON-RPC messages to client |
| `POST` | `/messages` | Client sends JSON-RPC messages to server |

### 4.2 SSE Flow

```
Client                              Server
  │                                   │
  │── GET /sse (Accept: text/event-stream) ──►
  │                                   │ Opens SSE connection
  │◄── event: endpoint               │ Sends the POST endpoint URL
  │    data: /messages/abc123        │ (unique session ID)
  │                                   │
  │── POST /messages/abc123 ──────►  │ Client sends JSON-RPC
  │    {jsonrpc, method, params}     │
  │                                   │
  │◄── SSE event: message ───────── │ Server pushes response
  │    data: {jsonrpc, result}       │
```

### 4.3 Implementation

```python
import asyncio
import json
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


class SSETransport:
    """Manages SSE connections for remote MCP clients."""

    def __init__(self, server: "MemGraphMCPServer", host: str = "0.0.0.0", port: int = 8100):
        self._server = server
        self._host = host
        self._port = port
        self._sessions: dict[str, asyncio.Queue] = {}  # session_id → queue

    async def run(self) -> None:
        """Run the SSE HTTP server."""
        # In production, use uvicorn or hypercorn for async HTTP
        # This example uses a minimal aiohttp or starlette approach
        ...


# Minimal aiohttp implementation
from aiohttp import web

async def handle_sse(request: web.Request) -> web.StreamResponse:
    """Handle SSE connection."""
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    transport = request.app["transport"]
    transport._sessions[session_id] = queue

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
    await response.write(
        f"event: endpoint\ndata: /messages/{session_id}\n\n".encode()
    )

    try:
        while True:
            # Wait for messages to send to the client
            message = await queue.get()
            if message is None:  # Shutdown signal
                break
            await response.write(f"event: message\ndata: {json.dumps(message)}\n\n".encode())
    except asyncio.CancelledError:
        pass
    finally:
        transport._sessions.pop(session_id, None)

    return response


async def handle_messages(request: web.Request) -> web.Response:
    """Handle incoming JSON-RPC messages from client."""
    session_id = request.match_info["session_id"]
    transport = request.app["transport"]

    if session_id not in transport._sessions:
        return web.json_response({"error": "Session not found"}, status=404)

    body = await request.json()
    # Dispatch and send response back via SSE
    response = await transport._server.dispatch(body)
    await transport._sessions[session_id].put(response)

    return web.json_response({"ok": True})
```

---

## 5. Server Class: `MemGraphMCPServer`

This is the core class that handles JSON-RPC dispatch and tool registration.

### 5.1 Class Definition

```python
"""services/mcp/server.py — MCP server implementation."""

import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from memgraph.client import MemGraphClient  # Same client as Python SDK

logger = logging.getLogger("memgraph.mcp")


class ToolDef:
    """Definition of an MCP tool."""

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: Callable[..., Coroutine[Any, Any, dict]],
    ):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler


class MemGraphMCPServer:
    """MCP protocol server exposing MemGraph as LLM-accessible tools.

    Handles JSON-RPC 2.0 dispatch over any transport (stdio, SSE).
    Tool handlers delegate to the MemGraph REST client for actual work.

    Usage:
        client = MemGraphClient(api_key="mg_live_...", base_url="http://localhost:8000")
        server = MemGraphMCPServer(client)
        # Run over stdio:
        await run_stdio_server(server)
        # Or over SSE:
        await SSETransport(server, port=8100).run()
    """

    # JSON-RPC error codes
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    def __init__(self, client: MemGraphClient):
        self._client = client
        self._tools: dict[str, ToolDef] = {}
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register all built-in tools."""
        from services.mcp.tools.memory import (
            handle_add_memory,
            handle_get_context,
            handle_search_memory,
            handle_delete_memory,
        )
        from services.mcp.tools.facts import (
            handle_add_fact,
            handle_list_facts,
            handle_update_fact,
        )
        from services.mcp.tools.graph import handle_get_user_graph
        from services.mcp.tools.users import handle_create_user
        from services.mcp.tools.sessions import handle_list_sessions

        # Register tools with their schemas
        self.register_tool(ToolDef(
            name="add_memory",
            description="Add messages to a user's memory for persistence and graph extraction.",
            input_schema={...},  # See 02-tool-definitions.md
            handler=handle_add_memory,
        ))
        # ... (all 8+ tools registered here)

    def register_tool(self, tool: ToolDef) -> None:
        """Register a tool definition."""
        self._tools[tool.name] = tool

    def get_tool_list(self) -> list[dict]:
        """Return the tools/list response."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    async def dispatch(self, request: dict) -> dict:
        """Dispatch a JSON-RPC 2.0 request to the appropriate handler.

        Args:
            request: Parsed JSON-RPC 2.0 request dict.

        Returns:
            JSON-RPC 2.0 response dict (success or error).
        """
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params", {})

        try:
            if method == "initialize":
                return self._handle_initialize(req_id, params)
            elif method == "notifications/initialized":
                return self._handle_notification(req_id)  # No response for notifications
            elif method == "tools/list":
                return self._make_result(req_id, {"tools": self.get_tool_list()})
            elif method == "tools/call":
                return await self._handle_tool_call(req_id, params)
            elif method == "resources/list":
                return self._make_result(req_id, {"resources": []})
            else:
                return self._make_error(req_id, self.METHOD_NOT_FOUND,
                                        f"Method not found: {method}")
        except Exception as e:
            logger.exception("Unhandled error dispatching request %s", method)
            return self._make_error(req_id, self.INTERNAL_ERROR, str(e))

    async def _handle_tool_call(self, req_id: int | str | None, params: dict) -> dict:
        """Handle a tools/call request."""
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if not tool_name:
            return self._make_error(req_id, self.INVALID_PARAMS, "Missing tool name")

        tool = self._tools.get(tool_name)
        if not tool:
            return self._make_error(req_id, self.METHOD_NOT_FOUND,
                                    f"Unknown tool: {tool_name}")

        try:
            result = await tool.handler(self._client, arguments)
            return self._make_result(req_id, result)
        except ValueError as e:
            # Validation error from schema mismatch
            return self._make_error(req_id, self.INVALID_PARAMS, str(e))
        except Exception as e:
            logger.error("Tool %s failed: %s", tool_name, e)
            return self._make_error(req_id, self.INTERNAL_ERROR,
                                    f"Tool {tool_name} failed: {e}")

    def _handle_initialize(self, req_id: int | str | None, params: dict) -> dict:
        """Handle protocol initialization."""
        return self._make_result(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
                "resources": {},
            },
            "serverInfo": {
                "name": "memgraph-mcp",
                "version": "1.0.0",
            },
        })

    def _handle_notification(self, req_id: int | str | None) -> dict | None:
        """Notifications have no response body but still get an id."""
        return None

    # ── Response builders ──────────────────────────────────────────

    def _make_result(self, req_id: int | str | None, result: dict) -> dict:
        if req_id is None:
            return {}  # Notification — no response
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": result,
        }

    def _make_error(self, req_id: int | str | None, code: int, message: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": code,
                "message": message,
            },
        }
```

### 5.2 Entry Point

```python
"""services/mcp/__main__.py — Entry point for `python -m memgraph.mcp_server`."""

import argparse
import asyncio
import os
import sys

from memgraph.client import MemGraphClient
from services.mcp.server import MemGraphMCPServer
from services.mcp.transport.stdio import run_stdio_server
from services.mcp.transport.sse import run_sse_server


def main() -> None:
    parser = argparse.ArgumentParser(description="MemGraph MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio",
                        help="Transport protocol (default: stdio)")
    parser.add_argument("--host", default="0.0.0.0", help="SSE server host")
    parser.add_argument("--port", type=int, default=8100, help="SSE server port")
    args = parser.parse_args()

    api_key = os.environ.get("MEMGRAPH_API_KEY")
    base_url = os.environ.get("MEMGRAPH_BASE_URL", "http://localhost:8000")

    if not api_key:
        print("ERROR: MEMGRAPH_API_KEY environment variable is required", file=sys.stderr)
        sys.exit(1)

    client = MemGraphClient(api_key=api_key, base_url=base_url)
    server = MemGraphMCPServer(client)

    if args.transport == "sse":
        asyncio.run(run_sse_server(server, host=args.host, port=args.port))
    else:
        asyncio.run(run_stdio_server(server))


if __name__ == "__main__":
    main()
```

---

## 6. Authentication

MCP server authentication is **identical to REST API authentication**. The MCP server does not implement its own auth layer — it delegates to the Python SDK's `MemGraphClient`, which includes the API key in every HTTP request.

### 6.1 Auth Flow

```
MCP Client                  MCP Server                        FastAPI Backend
    │                           │                                    │
    │  tools/call               │                                    │
    │  add_memory(params)       │  MemGraphClient.get(               │
    │──────────────────────────►│    api_key="mg_live_..."           │
    │                           │───────────────────────────────────►│
    │                           │    Authorization: Bearer mg_...   │
    │                           │                                    │
    │                           │◄─── 200 / 401 / 403 ────────────│
    │◄── result / error ──────│                                    │
```

### 6.2 No Additional Auth

- The MCP server does **not** accept or validate its own API keys
- The API key is passed at construction time to `MemGraphClient`
- If the key is invalid or expired, the REST call fails and the error propagates as an MCP error response
- **Do not** embed the API key in tool `arguments` — it's a server-level config, not a per-call parameter

### 6.3 API Key Sources (in priority order)

1. `MEMGRAPH_API_KEY` environment variable
2. `.env` file (for local development)
3. Explicitly passed to `MemGraphClient(api_key=...)`

---

## 7. Error Handling

All errors are returned as JSON-RPC 2.0 error responses with standard error codes.

### 7.1 Error Code Mapping

| Condition | JSON-RPC Code | HTTP Equivalent | Notes |
|-----------|---------------|-----------------|-------|
| Malformed JSON | -32700 (Parse Error) | 400 | Don't even have a valid request |
| Missing `jsonrpc`/`id`/`method` | -32600 (Invalid Request) | 400 | Structural validation fails |
| Unknown tool name | -32601 (Method Not Found) | 404 | Returned by `tools/call` |
| Invalid arguments | -32602 (Invalid Params) | 422 | Schema validation fails |
| REST API returns 4xx | -32603 (Internal Error) | varies | Propagated in `data` field |
| REST API returns 5xx | -32603 (Internal Error) | 502 | Backend unavailable |
| Unexpected exception | -32603 (Internal Error) | 500 | Any unhandled exception |
| Auth failure (401) | -32603 (Internal Error) | 401 | Invalid/expired API key |

### 7.2 Error Response Examples

```json
// Input validation error
{
  "jsonrpc": "2.0",
  "id": 5,
  "error": {
    "code": -32602,
    "message": "Invalid params",
    "data": "'messages' must contain at least 1 message"
  }
}

// Backend error
{
  "jsonrpc": "2.0",
  "id": 6,
  "error": {
    "code": -32603,
    "message": "Internal error",
    "data": "REST API returned 401: Invalid API key"
  }
}

// Unknown tool
{
  "jsonrpc": "2.0",
  "id": 7,
  "error": {
    "code": -32601,
    "message": "Method not found: unknown_tool"
  }
}
```

### 7.3 Structured Result with `isError`

When a tool call completes but the result indicates a logical error (not a system error), return it as a successful result with `isError: true`:

```python
async def handle_tool_error(client, arguments) -> dict:
    try:
        result = await client.some_operation(arguments)
        return {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "isError": False,
        }
    except client.NotFoundError as e:
        return {
            "content": [{"type": "text", "text": f"Resource not found: {e}"}],
            "isError": True,  # Tell the LLM this was a logical failure
        }
```

---

## 8. Pure Python vs FastMCP Recommendation

### 8.1 Comparison

| Criterion | Pure Python (`mcp` package) | FastMCP |
|-----------|------------------------------|---------|
| **Dependencies** | Only `mcp` (lightweight) | `fastmcp` + `mcp` (wrapper adds abstraction) |
| **Control** | Full control over dispatch loop | Decorator-based, less control |
| **Custom transport** | Easy to add stdio/SSE | Built-in transport only |
| **Type safety** | Manual | Built-in validation via Pydantic |
| **Debugging** | Direct — can see every message | Indirect — wrapper hides internals |
| **Community** | Smaller but stable spec | Growing, but API changes frequently |
| **Stdio support** | Manual (stdin.readline loop) | Built-in (`fastmcp run`) |
| **SSE support** | Manual | Built-in (`fastmcp serve`) |

### 8.2 Recommendation

**Use pure Python with the `mcp` package** for these reasons:

1. **Minimal dependency surface** — only `mcp` as an extra dependency in `pyproject.toml`. FastMCP pulls in additional dependencies (`httpx`, `pydantic-settings`, etc.) that overlap with what we already have.
2. **Direct control over transport** — we need both stdio and SSE. Pure Python gives us full control over the readline loop and SSE event format.
3. **Debug transparency** — when an LLM agent behaves unexpectedly, we need to see the exact JSON-RPC messages. Pure Python makes this trivial.
4. **No additional abstraction** — FastMCP's tool decorators add magic that makes the code harder to reason about. Our tool handlers are already clean async functions.

### 8.3 Dependency

```toml
# pyproject.toml (in services/mcp/)
[project.optional-dependencies]
mcp = ["mcp>=1.0.0"]
sse = ["aiohttp>=3.9.0"]  # Only needed for SSE transport
```

---

## 9. Testing

### 9.1 Unit Tests

```python
"""tests/unit/mcp/test_server.py"""

import pytest
from services.mcp.server import MemGraphMCPServer, ToolDef


@pytest.mark.asyncio
async def test_initialize():
    server = MemGraphMCPServer(mock_client)
    response = await server.dispatch({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {},
    })
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert "tools" in response["result"]["capabilities"]


@pytest.mark.asyncio
async def test_tools_list():
    server = MemGraphMCPServer(mock_client)
    response = await server.dispatch({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    })
    tools = response["result"]["tools"]
    assert any(t["name"] == "add_memory" for t in tools)
    assert any(t["name"] == "get_context" for t in tools)


@pytest.mark.asyncio
async def test_unknown_method():
    server = MemGraphMCPServer(mock_client)
    response = await server.dispatch({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "unknown",
    })
    assert response["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_unknown_tool():
    server = MemGraphMCPServer(mock_client)
    response = await server.dispatch({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "nonexistent"},
    })
    assert response["error"]["code"] == -32601
```

### 9.2 Integration Tests

```python
"""tests/integration/mcp/test_with_backend.py"""

import json
import pytest
from services.mcp.transport.stdio import _process_line
from services.mcp.server import MemGraphMCPServer
from memgraph.client import MemGraphClient


@pytest.mark.integration
async def test_add_memory_tool(mcp_client: MemGraphClient):
    """End-to-end test of add_memory tool against a running backend."""
    server = MemGraphMCPServer(mcp_client)

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "add_memory",
            "arguments": {
                "user_id": "test_user_001",
                "messages": [
                    {"role": "user", "content": "What is MemGraph?"},
                    {"role": "assistant", "content": "MemGraph is an open-source agent memory platform."}
                ],
                "session_id": "test_session_001",
            },
        },
    }

    response = await server.dispatch(request)
    assert "result" in response
    assert not response.get("isError", False)
    assert "memory recorded" in response["result"]["content"][0]["text"].lower()
```

---

## 10. File Structure

```
services/
  mcp/
    __init__.py
    __main__.py              # Entry point: `python -m memgraph.mcp_server`
    server.py                # MemGraphMCPServer class
    tool_def.py              # ToolDef dataclass
    transport/
      stdio.py               # Stdio transport (read stdin, write stdout)
      sse.py                 # SSE transport (HTTP server)
    tools/
      __init__.py
      memory.py              # add_memory, get_context, search_memory, delete_memory
      facts.py               # add_fact, list_facts, update_fact
      graph.py               # get_user_graph
      users.py               # create_user, delete_user
      sessions.py            # list_sessions
    utils/
      __init__.py
      response.py            # JSON-RPC response builders
      validation.py          # Argument validation helpers
```

---

## 11. Open Questions

| # | Question | Decision |
|---|----------|----------|
| Q1 | Should we support the `resources` protocol for streaming graph data? | Defer — not needed for MVP. Tools cover all use cases. |
| Q2 | Should we support `sampling` (LLM calling back to the host)? | No — MemGraph is a tool provider, not a chat host. |
| Q3 | SSE auth: should the SSE endpoint require an API key header? | Yes — add Bearer auth check on `/sse` and `/messages` endpoints in production. For local use, auth is handled by the backend. |
| Q4 | Should tools support streaming responses (e.g., for large context blocks)? | Defer — standard response is fine for context blocks < 32KB. Add streaming in Phase 5 if needed. |

---

*Corresponding SRS requirements: MCP-01, MCP-02, MCP-03. Next: [02-tool-definitions.md](02-tool-definitions.md) for full tool specifications.*
