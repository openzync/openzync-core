# MCP Tool Definitions — Implementation Guide

> **Domain:** MCP Server
> **SRS Phase:** Phase 2 — Full Feature Parity (Week 5-7)
> **Requirements:** MCP-02 (all tools), MCP-03
> **Doc Dependencies:** [01-mcp-setup.md](01-mcp-setup.md), [03-core-memory/01-message-ingestion.md](../03-core-memory/01-message-ingestion.md), [04-knowledge-graph/02-entity-operations.md](../04-knowledge-graph/02-entity-operations.md), [07-user-session-mgmt/01-user-crud.md](../07-user-session-mgmt/01-user-crud.md)

---

## 1. Overview

This document defines the complete schema and implementation for every MCP tool. Each tool has:

- **Name**: snake_case identifier used in `tools/call`
- **Description**: Shown to the LLM to help it decide when to use the tool
- **Input Schema**: JSON Schema describing the expected arguments
- **Implementation**: Which Python function handles the call, which REST endpoint it maps to
- **Error Responses**: Specific error cases per tool
- **Example Usage**: Exact JSON-RPC request/response pair

---

## 2. Core Tools (P0 — All Required for MVP)

---

### 2.1 `add_memory`

Add messages to a user's memory. Messages are persisted as episodes, queued for async entity extraction, fact extraction, and embedding.

#### Input Schema

```json
{
  "name": "add_memory",
  "description": "Add messages to a user's memory. Messages are persisted immediately and queued for async entity extraction, fact extraction, and embedding. Returns after the messages are acknowledged by the backend.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "user_id": {
        "type": "string",
        "description": "Unique identifier for the user whose memory to update."
      },
      "messages": {
        "type": "array",
        "description": "List of conversation messages to persist. At least 1 message required.",
        "items": {
          "type": "object",
          "properties": {
            "role": {
              "type": "string",
              "enum": ["user", "assistant", "system", "tool"],
              "description": "Message sender role."
            },
            "content": {
              "type": "string",
              "description": "Message body text. Max 64KB."
            },
            "created_at": {
              "type": "string",
              "format": "date-time",
              "description": "ISO-8601 timestamp. Assigned server-side if omitted."
            },
            "metadata": {
              "type": "object",
              "description": "Optional caller-defined metadata."
            }
          },
          "required": ["role", "content"]
        },
        "minItems": 1
      },
      "session_id": {
        "type": "string",
        "description": "Optional session identifier. If omitted, a default session is auto-created."
      }
    },
    "required": ["user_id", "messages"]
  }
}
```

#### Implementation

```python
# services/mcp/tools/memory.py

async def handle_add_memory(client: MemGraphClient, args: dict) -> dict:
    """Add messages to a user's memory.

    Maps to: POST /v1/users/{user_id}/memory
    """
    user_id = args["user_id"]
    messages = args["messages"]
    session_id = args.get("session_id")

    try:
        response = await client.memory.add(
            user_id=user_id,
            messages=messages,
            session_id=session_id,
        )
    except client.NotFoundError:
        return {
            "content": [{"type": "text", "text": f"User '{user_id}' not found. Create the user first with create_user."}],
            "isError": True,
        }
    except client.ValidationError as e:
        return {
            "content": [{"type": "text", "text": f"Validation error: {e}"}],
            "isError": True,
        }

    return {
        "content": [{
            "type": "text",
            "text": (
                f"Memory recorded. {response['message_count']} messages ingested "
                f"in session '{response.get('session_id', 'default')}'. "
                f"Async enrichment job ID: {response.get('job_id', 'N/A')}."
            ),
        }],
        "isError": False,
    }
```

#### Error Responses

| Condition | MCP Error | Message |
|-----------|-----------|---------|
| Empty messages array | -32602 | `'messages' must contain at least 1 message` |
| User not found | -32603 (isError=true) | `User 'X' not found. Create the user first with create_user.` |
| Content exceeds 64KB | -32602 | `Content exceeds maximum length` |
| Invalid role | -32602 | `Role must be one of: user, assistant, system, tool` |
| Backend unavailable | -32603 | `REST API returned 502: Backend unavailable` |
| Auth failure | -32603 | `REST API returned 401: Invalid API key` |

#### Example Usage

```json
// Request
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "add_memory",
    "arguments": {
      "user_id": "user_123",
      "session_id": "session_abc",
      "messages": [
        {"role": "user", "content": "I work at Acme Corp as a software engineer."},
        {"role": "assistant", "content": "Great, I'll remember that!"}
      ]
    }
  }
}

// Response
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [{
      "type": "text",
      "text": "Memory recorded. 2 messages ingested in session 'session_abc'. Async enrichment job ID: job_01j9xmf..."
    }],
    "isError": false
  }
}
```

---

### 2.2 `get_context`

Retrieve an assembled context block for a query. Returns a plain-text string of relevant facts, entity summaries, and recent episodes optimised for LLM injection.

#### Input Schema

```json
{
  "name": "get_context",
  "description": "Retrieve an assembled context block for a user, relevant to a query. Returns a plain-text string containing facts, entity summaries, and recent episodes — ready for LLM prompt injection.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "user_id": {
        "type": "string",
        "description": "Unique identifier for the user."
      },
      "query": {
        "type": "string",
        "description": "Natural language query to find relevant context. E.g., 'what are their programming preferences?'"
      },
      "limit": {
        "type": "integer",
        "description": "Maximum number of context items to include (default: 10, max: 50).",
        "default": 10,
        "minimum": 1,
        "maximum": 50
      }
    },
    "required": ["user_id", "query"]
  }
}
```

#### Implementation

```python
# services/mcp/tools/memory.py

async def handle_get_context(client: MemGraphClient, args: dict) -> dict:
    """Retrieve assembled context block for a query.

    Maps to: GET /v1/users/{user_id}/context?query={query}&limit={limit}
    """
    user_id = args["user_id"]
    query = args["query"]
    limit = args.get("limit", 10)

    try:
        context = await client.memory.get(
            user_id=user_id,
            query=query,
            limit=limit,
        )
    except client.NotFoundError:
        return {
            "content": [{"type": "text", "text": f"User '{user_id}' not found."}],
            "isError": True,
        }

    # Context is a plain string by default
    return {
        "content": [{"type": "text", "text": context}],
        "isError": False,
    }
```

#### Error Responses

| Condition | MCP Error | Message |
|-----------|-----------|---------|
| User not found | -32603 (isError=true) | `User 'X' not found.` |
| No context found | Not an error | Returns empty string or "No relevant context found." |
| Query empty | -32602 | `'query' must not be empty` |

#### Example Usage

```json
// Request
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "get_context",
    "arguments": {
      "user_id": "user_123",
      "query": "what does the user do for work?",
      "limit": 5
    }
  }
}

// Response
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "content": [{
      "type": "text",
      "text": "The user works at Acme Corp as a software engineer. They mentioned this on 2026-06-03.\n\nRelated facts:\n- User is employed at Acme Corp (confidence: 0.95)\n- User's role is software engineer (confidence: 0.92)\n\nRecent episodes:\n- User: I work at Acme Corp as a software engineer.\n- Assistant: Great, I'll remember that!"
    }],
    "isError": false
  }
}
```

---

### 2.3 `search_memory`

Search memory across facts, episodes, and entities using hybrid retrieval (vector + BM25 + graph). Returns ranked results with scores.

#### Input Schema

```json
{
  "name": "search_memory",
  "description": "Search memory across facts, episodes, and entities using hybrid retrieval. Returns ranked results with relevance scores.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "user_id": {
        "type": "string",
        "description": "Unique identifier for the user."
      },
      "query": {
        "type": "string",
        "description": "Natural language search query."
      },
      "types": {
        "type": "array",
        "description": "Content types to search. Default: all types.",
        "items": {
          "type": "string",
          "enum": ["facts", "episodes", "entities"]
        },
        "default": ["facts", "episodes", "entities"]
      },
      "limit": {
        "type": "integer",
        "description": "Maximum results to return (default: 10, max: 50).",
        "default": 10,
        "minimum": 1,
        "maximum": 50
      }
    },
    "required": ["user_id", "query"]
  }
}
```

#### Implementation

```python
# services/mcp/tools/memory.py

async def handle_search_memory(client: MemGraphClient, args: dict) -> dict:
    """Search memory across types using hybrid retrieval.

    Maps to: GET /v1/users/{user_id}/search?query={query}&types={types}&limit={limit}
    """
    user_id = args["user_id"]
    query = args["query"]
    types = args.get("types", ["facts", "episodes", "entities"])
    limit = args.get("limit", 10)

    try:
        results = await client.memory.search(
            user_id=user_id,
            query=query,
            types=types,
            limit=limit,
        )
    except client.NotFoundError:
        return {
            "content": [{"type": "text", "text": f"User '{user_id}' not found."}],
            "isError": True,
        }

    # Format results as readable text
    lines = [f"Search results for '{query}':\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. [{r['type']}] {r['content']} (score: {r.get('score', 'N/A')})")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "isError": False,
    }
```

#### Example Usage

```json
// Request
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "search_memory",
    "arguments": {
      "user_id": "user_123",
      "query": "programming languages",
      "types": ["facts", "entities"],
      "limit": 5
    }
  }
}

// Response
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{
      "type": "text",
      "text": "Search results for 'programming languages':\n\n1. [fact] User prefers Python over JavaScript (score: 0.89)\n2. [entity] Python (type: programming_language) (score: 0.85)\n3. [entity] JavaScript (type: programming_language) (score: 0.72)"
    }],
    "isError": false
  }
}
```

---

### 2.4 `add_fact`

Manually assert a fact triple into a user's knowledge graph. This is how you inject structured business data (purchases, preferences, relationships) directly.

#### Input Schema

```json
{
  "name": "add_fact",
  "description": "Manually assert a fact triple (subject, predicate, object) into a user's knowledge graph. Use for injecting structured business data like purchases, preferences, or relationships.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "user_id": {
        "type": "string",
        "description": "Unique identifier for the user."
      },
      "subject": {
        "type": "string",
        "description": "The subject of the fact triple. Usually the user_id or an entity name."
      },
      "predicate": {
        "type": "string",
        "description": "The relationship or action connecting subject to object. E.g., 'purchased', 'prefers', 'works_at'."
      },
      "object": {
        "type": "string",
        "description": "The object of the fact triple. E.g., 'Pro plan', 'Python', 'Acme Corp'."
      },
      "valid_at": {
        "type": "string",
        "format": "date-time",
        "description": "ISO-8601 timestamp when this fact became true. Defaults to now."
      },
      "expires_at": {
        "type": "string",
        "format": "date-time",
        "description": "ISO-8601 timestamp when this fact expires. Omit if the fact does not expire."
      },
      "confidence": {
        "type": "number",
        "description": "Confidence score between 0.0 and 1.0. Default: 1.0.",
        "minimum": 0,
        "maximum": 1,
        "default": 1.0
      }
    },
    "required": ["user_id", "subject", "predicate", "object"]
  }
}
```

#### Implementation

```python
# services/mcp/tools/facts.py

async def handle_add_fact(client: MemGraphClient, args: dict) -> dict:
    """Manually assert a fact triple.

    Maps to: POST /v1/users/{user_id}/facts
    """
    user_id = args["user_id"]

    fact = {
        "subject": args["subject"],
        "predicate": args["predicate"],
        "object": args["object"],
        "valid_at": args.get("valid_at"),
        "expires_at": args.get("expires_at"),
        "confidence": args.get("confidence", 1.0),
    }

    try:
        result = await client.facts.add(user_id=user_id, facts=[fact])
    except client.NotFoundError:
        return {
            "content": [{"type": "text", "text": f"User '{user_id}' not found."}],
            "isError": True,
        }
    except client.ValidationError as e:
        return {
            "content": [{"type": "text", "text": f"Validation error: {e}"}],
            "isError": True,
        }

    return {
        "content": [{
            "type": "text",
            "text": f"Fact recorded: ({args['subject']}) --[{args['predicate']}]-->({args['object']}). Fact ID: {result.get('fact_id', 'N/A')}.",
        }],
        "isError": False,
    }
```

#### Example Usage

```json
// Request
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "tools/call",
  "params": {
    "name": "add_fact",
    "arguments": {
      "user_id": "user_123",
      "subject": "user_123",
      "predicate": "purchased",
      "object": "Enterprise plan",
      "valid_at": "2026-06-01T00:00:00Z",
      "confidence": 1.0
    }
  }
}

// Response
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "content": [{
      "type": "text",
      "text": "Fact recorded: (user_123) --[purchased]-->(Enterprise plan). Fact ID: fact_01j9xmf..."
    }],
    "isError": false
  }
}
```

---

### 2.5 `list_facts`

List all extracted facts for a user, with optional filtering by date range and confidence threshold.

#### Input Schema

```json
{
  "name": "list_facts",
  "description": "List all extracted facts for a user. Optionally filter by date range and minimum confidence.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "user_id": {
        "type": "string",
        "description": "Unique identifier for the user."
      },
      "min_confidence": {
        "type": "number",
        "description": "Minimum confidence threshold (0.0 to 1.0). Only facts with confidence >= this value are returned.",
        "minimum": 0,
        "maximum": 1,
        "default": 0.0
      },
      "limit": {
        "type": "integer",
        "description": "Maximum facts to return (default: 20, max: 100).",
        "default": 20,
        "minimum": 1,
        "maximum": 100
      }
    },
    "required": ["user_id"]
  }
}
```

#### Implementation

```python
# services/mcp/tools/facts.py

async def handle_list_facts(client: MemGraphClient, args: dict) -> dict:
    """List extracted facts for a user.

    Maps to: GET /v1/users/{user_id}/facts?min_confidence={min_confidence}&limit={limit}
    """
    user_id = args["user_id"]
    min_confidence = args.get("min_confidence", 0.0)
    limit = args.get("limit", 20)

    try:
        facts = await client.facts.list(
            user_id=user_id,
            min_confidence=min_confidence,
            limit=limit,
        )
    except client.NotFoundError:
        return {
            "content": [{"type": "text", "text": f"User '{user_id}' not found."}],
            "isError": True,
        }

    if not facts:
        return {
            "content": [{"type": "text", "text": "No facts found for this user."}],
            "isError": False,
        }

    lines = [f"Facts for user '{user_id}' (confidence >= {min_confidence}):\n"]
    for i, f in enumerate(facts, 1):
        valid = f.get("valid_from", "unknown")
        confidence = f.get("confidence", "N/A")
        lines.append(f"{i}. {f['content']} [confidence: {confidence}, valid from: {valid}]")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "isError": False,
    }
```

#### Example Usage

```json
// Request
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "tools/call",
  "params": {
    "name": "list_facts",
    "arguments": {
      "user_id": "user_123",
      "min_confidence": 0.8,
      "limit": 10
    }
  }
}

// Response
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "content": [{
      "type": "text",
      "text": "Facts for user 'user_123' (confidence >= 0.8):\n\n1. User works at Acme Corp [confidence: 0.95, valid from: 2026-06-03T10:00:00Z]\n2. User prefers Python over JavaScript [confidence: 0.92, valid from: 2026-06-03T10:30:00Z]\n3. User purchased Enterprise plan [confidence: 1.0, valid from: 2026-06-01T00:00:00Z]"
    }],
    "isError": false
  }
}
```

---

### 2.6 `get_user_graph`

Return the entity knowledge graph for a user — all entity nodes and their relationships.

#### Input Schema

```json
{
  "name": "get_user_graph",
  "description": "Return the entity knowledge graph for a user — all entity nodes, their types, and connecting relationships.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "user_id": {
        "type": "string",
        "description": "Unique identifier for the user."
      },
      "max_nodes": {
        "type": "integer",
        "description": "Maximum number of nodes to return (default: 50, max: 200). For large graphs, use a smaller limit.",
        "default": 50,
        "minimum": 1,
        "maximum": 200
      },
      "entity_types": {
        "type": "array",
        "description": "Filter by entity types. E.g., ['Person', 'Company', 'Product']. Returns all types if omitted.",
        "items": {
          "type": "string"
        }
      }
    },
    "required": ["user_id"]
  }
}
```

#### Implementation

```python
# services/mcp/tools/graph.py

async def handle_get_user_graph(client: MemGraphClient, args: dict) -> dict:
    """Return entity nodes and edges for a user.

    Maps to: GET /v1/users/{user_id}/graph/nodes + GET /v1/users/{user_id}/graph/edges
    """
    user_id = args["user_id"]
    max_nodes = args.get("max_nodes", 50)
    entity_types = args.get("entity_types")

    try:
        nodes = await client.graph.get_nodes(
            user_id=user_id,
            limit=max_nodes,
            types=entity_types,
        )
        edges = await client.graph.get_edges(user_id=user_id)
    except client.NotFoundError:
        return {
            "content": [{"type": "text", "text": f"User '{user_id}' not found."}],
            "isError": True,
        }

    # Format as structured text
    lines = [f"Knowledge graph for user '{user_id}':\n"]
    lines.append(f"Entities ({len(nodes)}):")
    for n in nodes[:max_nodes]:
        lines.append(f"  - {n.get('name', n['id'])} [type: {n.get('type', 'unknown')}]")

    if edges:
        lines.append(f"\nRelationships ({len(edges)}):")
        for e in edges[:50]:  # Cap output size
            lines.append(f"  - ({e['subject']}) --[{e.get('predicate', 'relates_to')}]-->({e['object']})")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "isError": False,
    }
```

#### Example Usage

```json
// Request
{
  "jsonrpc": "2.0",
  "id": 6,
  "method": "tools/call",
  "params": {
    "name": "get_user_graph",
    "arguments": {
      "user_id": "user_123",
      "max_nodes": 20,
      "entity_types": ["Person", "Company"]
    }
  }
}

// Response
{
  "jsonrpc": "2.0",
  "id": 6,
  "result": {
    "content": [{
      "type": "text",
      "text": "Knowledge graph for user 'user_123':\n\nEntities (3):\n  - user_123 [type: Person]\n  - Acme Corp [type: Company]\n  - Enterprise plan [type: Product]\n\nRelationships (2):\n  - (user_123) --[works_at]-->(Acme Corp)\n  - (user_123) --[purchased]-->(Enterprise plan)"
    }],
    "isError": false
  }
}
```

---

### 2.7 `create_user`

Create a new user record in the system. Must be called before adding memory or facts for a user.

#### Input Schema

```json
{
  "name": "create_user",
  "description": "Create a new user record. Must be called before adding memory or facts for a user.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "user_id": {
        "type": "string",
        "description": "Unique identifier for the new user. This is your application's user ID."
      },
      "name": {
        "type": "string",
        "description": "Optional display name for the user."
      },
      "email": {
        "type": "string",
        "format": "email",
        "description": "Optional email address."
      },
      "metadata": {
        "type": "object",
        "description": "Optional metadata key-value pairs."
      }
    },
    "required": ["user_id"]
  }
}
```

#### Implementation

```python
# services/mcp/tools/users.py

async def handle_create_user(client: MemGraphClient, args: dict) -> dict:
    """Create a new user record.

    Maps to: POST /v1/users
    """
    user_id = args["user_id"]

    try:
        user = await client.users.create(
            user_id=user_id,
            name=args.get("name"),
            email=args.get("email"),
            metadata=args.get("metadata", {}),
        )
    except client.ConflictError:
        return {
            "content": [{"type": "text", "text": f"User '{user_id}' already exists."}],
            "isError": True,
        }
    except client.ValidationError as e:
        return {
            "content": [{"type": "text", "text": f"Validation error: {e}"}],
            "isError": True,
        }

    return {
        "content": [{
            "type": "text",
            "text": f"User '{user_id}' created successfully.",
        }],
        "isError": False,
    }
```

#### Example Usage

```json
// Request
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "tools/call",
  "params": {
    "name": "create_user",
    "arguments": {
      "user_id": "user_456",
      "name": "Alice Smith",
      "email": "alice@example.com",
      "metadata": {"source": "onboarding"}
    }
  }
}

// Response
{
  "jsonrpc": "2.0",
  "id": 7,
  "result": {
    "content": [{
      "type": "text",
      "text": "User 'user_456' created successfully."
    }],
    "isError": false
  }
}
```

---

### 2.8 `list_sessions`

List conversation sessions for a user. Sessions group messages into logical conversations.

#### Input Schema

```json
{
  "name": "list_sessions",
  "description": "List conversation sessions for a user. Sessions group messages into logical conversations.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "user_id": {
        "type": "string",
        "description": "Unique identifier for the user."
      },
      "limit": {
        "type": "integer",
        "description": "Maximum sessions to return (default: 20, max: 100).",
        "default": 20,
        "minimum": 1,
        "maximum": 100
      }
    },
    "required": ["user_id"]
  }
}
```

#### Implementation

```python
# services/mcp/tools/sessions.py

async def handle_list_sessions(client: MemGraphClient, args: dict) -> dict:
    """List sessions for a user.

    Maps to: GET /v1/users/{user_id}/sessions
    """
    user_id = args["user_id"]
    limit = args.get("limit", 20)

    try:
        sessions = await client.sessions.list(user_id=user_id, limit=limit)
    except client.NotFoundError:
        return {
            "content": [{"type": "text", "text": f"User '{user_id}' not found."}],
            "isError": True,
        }

    if not sessions:
        return {
            "content": [{"type": "text", "text": "No sessions found for this user."}],
            "isError": False,
        }

    lines = [f"Sessions for user '{user_id}':\n"]
    for i, s in enumerate(sessions, 1):
        created = s.get("created_at", "unknown")
        msg_count = s.get("message_count", "?")
        status = "closed" if s.get("closed_at") else "active"
        lines.append(f"{i}. {s.get('external_id', s['id'])} [{status}] — {msg_count} messages, created {created}")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "isError": False,
    }
```

#### Example Usage

```json
// Request
{
  "jsonrpc": "2.0",
  "id": 8,
  "method": "tools/call",
  "params": {
    "name": "list_sessions",
    "arguments": {
      "user_id": "user_123",
      "limit": 5
    }
  }
}

// Response
{
  "jsonrpc": "2.0",
  "id": 8,
  "result": {
    "content": [{
      "type": "text",
      "text": "Sessions for user 'user_123':\n\n1. session_abc [active] — 12 messages, created 2026-06-03T10:00:00Z\n2. session_xyz [closed] — 5 messages, created 2026-05-28T14:30:00Z"
    }],
    "isError": false
  }
}
```

---

## 3. Additional Recommended Tools (P1 — Deferred)

These tools are documented for completeness but are **not required for MVP**. Implement them in Phase 3 after the core 8 tools are stable.

---

### 3.1 `delete_memory`

Wipe all memory for a user — facts, episodes, graph nodes, and sessions.

#### Input Schema

```json
{
  "name": "delete_memory",
  "description": "Permanently delete all memory for a user — facts, episodes, graph nodes, and sessions. This action is irreversible.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "user_id": {
        "type": "string",
        "description": "Unique identifier for the user whose memory to delete."
      },
      "confirm": {
        "type": "boolean",
        "description": "Must be set to true to confirm deletion. Safety check to prevent accidental data loss."
      }
    },
    "required": ["user_id", "confirm"]
  }
}
```

#### Implementation

```python
async def handle_delete_memory(client: MemGraphClient, args: dict) -> dict:
    if not args.get("confirm"):
        return {
            "content": [{"type": "text", "text": "Confirmation required. Set 'confirm' to true to proceed with deletion."}],
            "isError": True,
        }

    user_id = args["user_id"]
    await client.memory.delete(user_id=user_id)
    return {
        "content": [{"type": "text", "text": f"All memory deleted for user '{user_id}'."}],
        "isError": False,
    }
```

---

### 3.2 `delete_user`

Delete a user and all associated data. Full GDPR-compliant cascade.

#### Input Schema

```json
{
  "name": "delete_user",
  "description": "Permanently delete a user and all associated data (sessions, facts, graph nodes). Full cascade — irreversible.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "user_id": {
        "type": "string",
        "description": "Unique identifier for the user to delete."
      },
      "confirm": {
        "type": "boolean",
        "description": "Must be set to true to confirm deletion."
      }
    },
    "required": ["user_id", "confirm"]
  }
}
```

#### Implementation

```python
async def handle_delete_user(client: MemGraphClient, args: dict) -> dict:
    if not args.get("confirm"):
        return {
            "content": [{"type": "text", "text": "Confirmation required. Set 'confirm' to true to proceed."}],
            "isError": True,
        }

    await client.users.delete(user_id=args["user_id"])
    return {
        "content": [{"type": "text", "text": f"User '{args['user_id']}' and all data deleted."}],
        "isError": False,
    }
```

---

### 3.3 `update_fact`

Update a fact's confidence score or validity window.

#### Input Schema

```json
{
  "name": "update_fact",
  "description": "Update a fact's confidence score or validity window. Useful for correcting auto-extracted facts or expiring outdated information.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "user_id": {
        "type": "string",
        "description": "Unique identifier for the user."
      },
      "fact_id": {
        "type": "string",
        "description": "ID of the fact to update."
      },
      "confidence": {
        "type": "number",
        "description": "New confidence score (0.0 to 1.0).",
        "minimum": 0,
        "maximum": 1
      },
      "expires_at": {
        "type": "string",
        "format": "date-time",
        "description": "New expiration timestamp. Set to a past date to expire the fact."
      }
    },
    "required": ["user_id", "fact_id"]
  }
}
```

#### Implementation

```python
async def handle_update_fact(client: MemGraphClient, args: dict) -> dict:
    await client.facts.update(
        user_id=args["user_id"],
        fact_id=args["fact_id"],
        confidence=args.get("confidence"),
        expires_at=args.get("expires_at"),
    )
    return {
        "content": [{"type": "text", "text": f"Fact '{args['fact_id']}' updated."}],
        "isError": False,
    }
```

---

## 4. Tools List Response

When the MCP host calls `tools/list`, the server returns the complete list of registered tools:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "tools": [
      {
        "name": "add_memory",
        "description": "Add messages to a user's memory...",
        "inputSchema": { "...": "..." }
      },
      {
        "name": "get_context",
        "description": "Retrieve an assembled context block...",
        "inputSchema": { "...": "..." }
      },
      {
        "name": "search_memory",
        "description": "Search memory across facts, episodes, and entities...",
        "inputSchema": { "...": "..." }
      },
      {
        "name": "add_fact",
        "description": "Manually assert a fact triple...",
        "inputSchema": { "...": "..." }
      },
      {
        "name": "list_facts",
        "description": "List all extracted facts for a user...",
        "inputSchema": { "...": "..." }
      },
      {
        "name": "get_user_graph",
        "description": "Return the entity knowledge graph for a user...",
        "inputSchema": { "...": "..." }
      },
      {
        "name": "create_user",
        "description": "Create a new user record...",
        "inputSchema": { "...": "..." }
      },
      {
        "name": "list_sessions",
        "description": "List conversation sessions for a user...",
        "inputSchema": { "...": "..." }
      }
    ]
  }
}
```

(In MVP, only the 8 core tools are returned. The deferred tools are registered in a later release.)

---

## 5. Tool Registration Pattern

For maintainability, each tool module exports a `register` function:

```python
# services/mcp/tools/memory.py

def register(server: MemGraphMCPServer) -> None:
    """Register all memory-related tools."""

    server.register_tool(ToolDef(
        name="add_memory",
        description="Add messages to a user's memory...",
        input_schema=ADD_MEMORY_SCHEMA,
        handler=handle_add_memory,
    ))

    server.register_tool(ToolDef(
        name="get_context",
        description="Retrieve an assembled context block...",
        input_schema=GET_CONTEXT_SCHEMA,
        handler=handle_get_context,
    ))

    server.register_tool(ToolDef(
        name="search_memory",
        description="Search memory across facts, episodes, and entities...",
        input_schema=SEARCH_MEMORY_SCHEMA,
        handler=handle_search_memory,
    ))
```

```python
# services/mcp/server.py — Registration loop

from services.mcp.tools import memory, facts, graph, users, sessions

def _register_default_tools(self) -> None:
    """Register all built-in tool modules."""
    memory.register(self)
    facts.register(self)
    graph.register(self)
    users.register(self)
    sessions.register(self)
```

---

## 6. Testing Tools

### 6.1 Unit Test Example

```python
"""tests/unit/mcp/test_tools.py"""

import pytest
from services.mcp.tools.memory import handle_add_memory


@pytest.mark.asyncio
async def test_add_memory_happy_path(mock_client):
    """Test that add_memory returns a well-formed response."""
    mock_client.memory.add.return_value = {
        "message_count": 2,
        "session_id": "session_abc",
        "job_id": "job_123",
    }

    result = await handle_add_memory(mock_client, {
        "user_id": "user_123",
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ],
    })

    assert not result["isError"]
    assert "memory recorded" in result["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_add_memory_user_not_found(mock_client):
    """Test error handling when user doesn't exist."""
    from memgraph.client import NotFoundError
    mock_client.memory.add.side_effect = NotFoundError("User not found")

    result = await handle_add_memory(mock_client, {
        "user_id": "nonexistent",
        "messages": [{"role": "user", "content": "Hello"}],
    })

    assert result["isError"]
    assert "not found" in result["content"][0]["text"].lower()
```

### 6.2 Integration Test

```python
"""tests/integration/mcp/test_tools_e2e.py"""

@pytest.mark.integration
@pytest.mark.parametrize("tool_name,args", [
    ("create_user", {"user_id": "mcp_test_user"}),
    ("add_memory", {"user_id": "mcp_test_user", "messages": [{"role": "user", "content": "Hello"}]}),
    ("get_context", {"user_id": "mcp_test_user", "query": "Hello"}),
    ("list_facts", {"user_id": "mcp_test_user"}),
    ("list_sessions", {"user_id": "mcp_test_user"}),
    ("get_user_graph", {"user_id": "mcp_test_user", "max_nodes": 10}),
])
async def test_tool_returns_success(mcp_server, tool_name, args):
    """Every tool should return a well-formed response."""
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": args},
    }
    response = await mcp_server.dispatch(request)
    assert "result" in response, f"Tool {tool_name} failed: {response.get('error')}"
```

---

*Corresponding SRS requirements: MCP-02 (all 8 core tools). Next: [03-claude-desktop-config.md](03-claude-desktop-config.md) for integration instructions.*
