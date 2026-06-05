# Claude Desktop MCP Configuration — Implementation Guide

> **Domain:** MCP Server
> **SRS Phase:** Phase 2 — Full Feature Parity (Week 5-7)
> **Requirements:** MCP-03, MCP-04
> **Doc Dependencies:** [01-mcp-setup.md](01-mcp-setup.md), [02-tool-definitions.md](02-tool-definitions.md)

---

## 1. Overview

This document describes how to configure LLM agent hosts (Claude Desktop, Cursor, any MCP-compatible client) to connect to the MemGraph MCP server. The configuration is a JSON block that tells the host how to launch the MCP server process and what environment variables to set.

---

## 2. Claude Desktop Configuration

### 2.1 Configuration File Location

**macOS:**
```
~/Library/Application Support/Claude/claude_desktop_config.json
```

**Windows:**
```
%APPDATA%\Claude\claude_desktop_config.json
```

**Linux:**
```
~/.config/Claude/claude_desktop_config.json
```

### 2.2 Standard Configuration

```json
{
  "mcpServers": {
    "memgraph": {
      "command": "python",
      "args": ["-m", "memgraph.mcp_server"],
      "env": {
        "MEMGRAPH_API_KEY": "mg_live_your_api_key_here",
        "MEMGRAPH_BASE_URL": "http://localhost:8000"
      }
    }
  }
}
```

### 2.3 Virtual Environment Configuration

If MemGraph is installed in a virtual environment, use the full path to the Python interpreter:

```json
{
  "mcpServers": {
    "memgraph": {
      "command": "/path/to/venv/bin/python",
      "args": ["-m", "memgraph.mcp_server"],
      "env": {
        "MEMGRAPH_API_KEY": "mg_live_your_api_key_here",
        "MEMGRAPH_BASE_URL": "http://localhost:8000"
      }
    }
  }
}
```

### 2.4 UVX / pipx Configuration

If installed via `uvx` or `pipx`:

```json
{
  "mcpServers": {
    "memgraph": {
      "command": "uvx",
      "args": ["memgraph-mcp"],
      "env": {
        "MEMGRAPH_API_KEY": "mg_live_your_api_key_here",
        "MEMGRAPH_BASE_URL": "http://localhost:8000"
      }
    }
  }
}
```

---

## 3. Docker Alternative

Run the MCP server from a Docker container instead of a local Python process. This is useful when:
- You don't want Python installed locally
- You want process isolation
- You're deploying in a containerised environment

### 3.1 Docker Configuration

```json
{
  "mcpServers": {
    "memgraph": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "--network", "host",
        "-e", "MEMGRAPH_API_KEY",
        "-e", "MEMGRAPH_BASE_URL",
        "memgraph/mcp-server:latest"
      ],
      "env": {
        "MEMGRAPH_API_KEY": "mg_live_your_api_key_here",
        "MEMGRAPH_BASE_URL": "http://localhost:8000"
      }
    }
  }
}
```

### 3.2 Docker Compose Service (for SSE mode)

```yaml
# docker-compose.yml
services:
  mcp-server:
    image: memgraph/mcp-server:latest
    ports:
      - "8100:8100"
    environment:
      MEMGRAPH_API_KEY: "${MEMGRAPH_API_KEY}"
      MEMGRAPH_BASE_URL: "http://api:8000"
    command: ["--transport", "sse", "--port", "8100"]
    networks:
      - memgraph-net
```

Then configure Claude Desktop with SSE URL:

```json
{
  "mcpServers": {
    "memgraph": {
      "type": "sse",
      "url": "http://localhost:8100/sse"
    }
  }
}
```

### 3.3 Dockerfile for MCP Server

```dockerfile
# services/mcp/Dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8100

# Default: stdio transport for local MCP use
# Override with --transport sse for remote connections
ENTRYPOINT ["python", "-m", "memgraph.mcp_server"]
CMD ["--transport", "stdio"]
```

---

## 4. Testing with MCP Inspector

### 4.1 What is MCP Inspector?

MCP Inspector is a diagnostic tool provided by the MCP project. It connects to any MCP server and lets you:
- List all available tools
- Call individual tools with custom arguments
- View raw JSON-RPC messages
- Debug transport issues

### 4.2 Installation

```bash
npx @modelcontextprotocol/inspector
```

Or install globally:

```bash
npm install -g @modelcontextprotocol/inspector
```

### 4.3 Running Inspector with MemGraph

**Option A: Stdio transport (run from project directory)**

```bash
npx @modelcontextprotocol/inspector \
  python -m memgraph.mcp_server
```

**Option B: SSE transport**

First start the MCP server in SSE mode:

```bash
python -m memgraph.mcp_server --transport sse --port 8100
```

Then in another terminal:

```bash
npx @modelcontextprotocol/inspector http://localhost:8100/sse
```

### 4.4 Inspector Test Checklist

After launching Inspector, run these tests to verify all 8 tools respond correctly:

| Test | Steps | Expected Result |
|------|-------|-----------------|
| **1. Tools list** | Click "List Tools" button | 8 tools appear with names and descriptions |
| **2. Create user** | Select `create_user` → enter `{"user_id": "inspector_test"}` → Call | Response: "User created" |
| **3. Add memory** | Select `add_memory` → enter `{"user_id": "inspector_test", "messages": [{"role": "user", "content": "Test message"}]}` → Call | Response: "Memory recorded" |
| **4. Get context** | Select `get_context` → enter `{"user_id": "inspector_test", "query": "Test"}` → Call | Response: text block (may be empty if enrichment hasn't completed) |
| **5. Search memory** | Select `search_memory` → enter `{"user_id": "inspector_test", "query": "Test"}` → Call | Response: search results or empty |
| **6. Add fact** | Select `add_fact` → enter `{"user_id": "inspector_test", "subject": "inspector_test", "predicate": "tested", "object": "MCP Inspector"}` → Call | Response: "Fact recorded" |
| **7. List facts** | Select `list_facts` → enter `{"user_id": "inspector_test"}` → Call | Response: lists the fact just added |
| **8. Get graph** | Select `get_user_graph` → enter `{"user_id": "inspector_test", "max_nodes": 20}` → Call | Response: entity nodes and edges |
| **9. List sessions** | Select `list_sessions` → enter `{"user_id": "inspector_test"}` → Call | Response: lists session |
| **10. Error test** | Select `add_memory` → enter `{"user_id": "nonexistent", "messages": [{"role": "user", "content": "test"}]}` → Call | Response: isError=true, message about user not found |

### 4.5 Inspector Debug Features

1. **Raw JSON tab**: View the exact JSON-RPC request/response for each tool call
2. **Logs tab**: View stderr output from the MCP server process
3. **Transport tab**: See connection status and protocol version
4. **Timing**: Each tool call shows elapsed time

---

## 5. Debugging

### 5.1 Enable Debug Logging

Set environment variables before launching:

```bash
# Verbose MCP protocol logging
MEMGRAPH_LOG_LEVEL=DEBUG python -m memgraph.mcp_server

# Or in claude_desktop_config.json
{
  "mcpServers": {
    "memgraph": {
      "command": "python",
      "args": ["-m", "memgraph.mcp_server"],
      "env": {
        "MEMGRAPH_API_KEY": "mg_live_...",
        "MEMGRAPH_BASE_URL": "http://localhost:8000",
        "MEMGRAPH_LOG_LEVEL": "DEBUG"
      }
    }
  }
}
```

### 5.2 View Logs

Claude Desktop captures the MCP server's **stderr** output. Logs are written to stderr (never stdout — stdout is the protocol channel).

**macOS:**
```bash
# View Claude Desktop logs
tail -f ~/Library/Logs/Claude/mcp*.log

# Or for the memgraph server specifically
grep "memgraph" ~/Library/Logs/Claude/mcp*.log
```

**Linux:**
```bash
journalctl -u claude-desktop --no-pager | grep memgraph
```

**For stdio testing (manual):**
```bash
# Test the MCP server directly — send a tools/list request and see the response
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | \
  python -m memgraph.mcp_server
```

### 5.3 Common Issues and Solutions

| Symptom | Likely Cause | Solution |
|---------|--------------|----------|
| **"Could not start MCP server"** in Claude Desktop | Python not found or module not installed | Use absolute path to Python. Run `python -m memgraph.mcp_server` from terminal first to verify. |
| **"Connection refused"** | API backend not running | Start the FastAPI server (`uvicorn app.main:app`). Check `MEMGRAPH_BASE_URL`. |
| **"401 Unauthorized"** | Invalid API key | Check `MEMGRAPH_API_KEY` value. Generate a new key from the dashboard if needed. |
| **"Tool not found"** | Tool not in `tools/list` | Run Inspector to verify tool list. Check `_register_default_tools` is called. |
| **No tools shown (empty list)** | Module import error | Enable DEBUG logging. Check Python path for `services/mcp/tools/` modules. |
| **Claude Desktop hangs on tool call** | Response not flushed to stdout | Verify `sys.stdout.flush()` is called after every response. Stdio transport must flush. |
| **"Parse error"** | Non-JSON output on stdout | Ensure no `print()` statements in the code path. All logging goes to stderr. |
| **Slow tool response (>5s)** | Backend latency or enrichment in progress | Check `/health` endpoint. `add_memory` returns immediately (202), enrichment is async. |
| **MCP Inspector shows protocol version mismatch** | Outdated MCP package | Update `mcp` package: `pip install --upgrade mcp` |
| **Docker: "Cannot connect"** | Docker not running or wrong image | Run `docker run --rm memgraph/mcp-server:latest` to test. |

### 5.4 Debugging Checklist

Before reporting an issue, run through this checklist:

- [ ] Can you run `python -m memgraph.mcp_server` from the terminal without errors?
- [ ] Does `echo '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | python -m memgraph.mcp_server` return a valid JSON response?
- [ ] Is `MEMGRAPH_API_KEY` set correctly? (Check with `echo $MEMGRAPH_API_KEY`)
- [ ] Is the backend API server running? (Check with `curl http://localhost:8000/health`)
- [ ] Are you flushing stdout after every response?
- [ ] Are all log messages going to stderr, not stdout?
- [ ] Is the Python path correct in the Claude Desktop config?
- [ ] For Docker: is `--network host` set (or port mapping correct)?

---

## 6. Cursor Integration

Cursor supports the same MCP configuration format in a project-level file.

### 6.1 Configuration File

Place a `.cursor/mcp.json` file in your project root:

```json
{
  "mcpServers": {
    "memgraph": {
      "command": "python",
      "args": ["-m", "memgraph.mcp_server"],
      "env": {
        "MEMGRAPH_API_KEY": "mg_live_your_api_key_here",
        "MEMGRAPH_BASE_URL": "http://localhost:8000"
      }
    }
  }
}
```

### 6.2 Cursor-Specific Notes

- **Project scope**: Each project has its own `.cursor/mcp.json`. The MCP server only starts when that project is open.
- **Environment variables**: Cursor does not inherit shell environment variables by default. Always set `env` explicitly in the config.
- **Restart**: After changing `.cursor/mcp.json`, restart Cursor or run "Developer: Reload Window" from the command palette.
- **Verification**: Open Cursor's MCP panel (gear icon in the bottom bar) to see connected servers and available tools.

### 6.3 SSE Transport for Remote Cursor

If you run the MCP server on a remote machine (e.g., dev server):

```json
{
  "mcpServers": {
    "memgraph": {
      "type": "sse",
      "url": "https://your-server:8100/sse"
    }
  }
}
```

---

## 7. Other MCP Hosts

### 7.1 Continue.dev (VS Code / JetBrains)

```json
{
  "experimental": {
    "mcpServers": {
      "memgraph": {
        "command": "python",
        "args": ["-m", "memgraph.mcp_server"],
        "env": {
          "MEMGRAPH_API_KEY": "mg_live_...",
          "MEMGRAPH_BASE_URL": "http://localhost:8000"
        }
      }
    }
  }
}
```

### 7.2 Zed Editor

```json
// ~/.config/zed/settings.json
{
  "mcp": {
    "memgraph": {
      "command": "python",
      "args": ["-m", "memgraph.mcp_server"],
      "env": {
        "MEMGRAPH_API_KEY": "mg_live_...",
        "MEMGRAPH_BASE_URL": "http://localhost:8000"
      }
    }
  }
}
```

---

## 8. SSE Production Deployment

For production deployments where the MCP server runs remotely:

### 8.1 Nginx Reverse Proxy

```nginx
# /etc/nginx/sites-available/mcp.memgraph.example.com
server {
    listen 443 ssl;
    server_name mcp.memgraph.example.com;

    ssl_certificate /etc/letsencrypt/live/mcp.memgraph.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mcp.memgraph.example.com/privkey.pem;

    # SSE endpoint — needs buffering disabled
    location /sse {
        proxy_pass http://127.0.0.1:8100;
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding on;
    }

    # Message endpoint
    location /messages/ {
        proxy_pass http://127.0.0.1:8100;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # Health check
    location /health {
        proxy_pass http://127.0.0.1:8100;
    }
}
```

### 8.2 Client Configuration for Remote MCP

```json
{
  "mcpServers": {
    "memgraph": {
      "type": "sse",
      "url": "https://mcp.memgraph.example.com/sse"
    }
  }
}
```

---

## 9. Testing End-to-End with Claude

### 9.1 Natural Language Test Prompts

After configuring Claude Desktop, try these prompts to verify the MCP integration:

```
Prompt 1: "Remember that I work at Acme Corp as a software engineer."
Expected: Claude calls add_memory, stores the information.

Prompt 2: "What do you know about my work?"
Expected: Claude calls get_context, retrieves the stored fact about Acme Corp.

Prompt 3: "Search my memory for anything related to Acme Corp."
Expected: Claude calls search_memory, finds the work fact.

Prompt 4: "List all the facts you know about me."
Expected: Claude calls list_facts, returns stored facts.
```

### 9.2 Cleanup After Testing

```
Prompt: "Delete all my data from MemGraph."
Expected: Claude should refuse (no delete tool available in MVP) or call delete_memory (if implemented).
```

For MVP cleanup, use the REST API directly:

```bash
curl -X DELETE http://localhost:8000/v1/users/test_user \
  -H "Authorization: Bearer mg_live_..."
```

---

## 10. Environment Variables Reference

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `MEMGRAPH_API_KEY` | — | Yes | API key for MemGraph backend authentication |
| `MEMGRAPH_BASE_URL` | `http://localhost:8000` | No | Base URL of the MemGraph FastAPI backend |
| `MEMGRAPH_LOG_LEVEL` | `INFO` | No | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `MCP_TRANSPORT` | `stdio` | No | Transport protocol: `stdio` or `sse` |
| `MCP_PORT` | `8100` | No | Port for SSE transport |
| `MCP_HOST` | `0.0.0.0` | No | Host for SSE transport |

---

## 11. Quick Start Script

```bash
#!/usr/bin/env bash
# scripts/mcp-quickstart.sh
# Run this to verify the MCP server is working end-to-end

set -euo pipefail

echo "=== MemGraph MCP Server Quick Start ==="
echo ""

# Check prerequisites
echo "1. Checking prerequisites..."
command -v python >/dev/null 2>&1 || { echo "ERROR: python not found"; exit 1; }
python -c "import memgraph" 2>/dev/null || { echo "ERROR: memgraph package not installed"; exit 1; }
echo "   ✅ Python + memgraph package found"

# Check env vars
echo "2. Checking environment..."
if [ -z "${MEMGRAPH_API_KEY:-}" ]; then
    echo "   ⚠️  MEMGRAPH_API_KEY not set — using test key"
    export MEMGRAPH_API_KEY="mg_test_quickstart_key"
fi
if [ -z "${MEMGRAPH_BASE_URL:-}" ]; then
    export MEMGRAPH_BASE_URL="http://localhost:8000"
fi
echo "   ✅ MEMGRAPH_BASE_URL=$MEMGRAPH_BASE_URL"

# Check backend
echo "3. Checking backend..."
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" "$MEMGRAPH_BASE_URL/health" 2>/dev/null || echo "000")
if [ "$HEALTH" = "200" ]; then
    echo "   ✅ Backend is healthy"
else
    echo "   ⚠️  Backend not reachable (HTTP $HEALTH) — continuing anyway"
fi

# Test tools/list
echo "4. Testing tools/list..."
RESPONSE=$(echo '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | \
    python -m memgraph.mcp_server 2>/dev/null)
TOOL_COUNT=$(echo "$RESPONSE" | python -c "import sys,json; print(len(json.load(sys.stdin)['result']['tools']))" 2>/dev/null || echo "0")
echo "   ✅ $TOOL_COUNT tools registered"

# Test create_user + add_memory
echo "5. Testing create_user + add_memory..."
CREATE_REQ='{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"create_user","arguments":{"user_id":"mcp_qs_user"}}}'
ADD_REQ='{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"add_memory","arguments":{"user_id":"mcp_qs_user","messages":[{"role":"user","content":"Hello from quick start!"}]}}}'

echo "$CREATE_REQ" | python -m memgraph.mcp_server 2>/dev/null | python -c "
import sys, json
r = json.load(sys.stdin)
print('   ✅' if 'result' in r else '   ❌', 'create_user:', r.get('result', r.get('error')))
" 2>/dev/null || echo "   ⚠️  create_user test skipped"

echo ""
echo "=== Quick start complete! ==="
echo "To test with Claude Desktop, add the config from 03-claude-desktop-config.md"
```

---

*Corresponding SRS requirements: MCP-03, MCP-04. Previous: [02-tool-definitions.md](02-tool-definitions.md).*
