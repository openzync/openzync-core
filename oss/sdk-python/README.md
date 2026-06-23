# OpenZep Python SDK

[![PyPI](https://img.shields.io/pypi/v/openzync)](https://pypi.org/project/openzync/)
[![Python](https://img.shields.io/pypi/pyversions/openzync)](https://pypi.org/project/openzync/)
[![License](https://img.shields.io/pypi/l/openzync)](https://www.apache.org/licenses/LICENSE-2.0)

Python SDK for [OpenZep](https://github.com/rohnsha0/openzep) — the open-source agent memory platform with persistent, queryable, graph-based memory for AI agents.

## Installation

```bash
pip install openzync
```

Requires Python 3.11+.

## Quick Start

```python
from openzync import OpenZep

client = OpenZep(api_key="mg_live_your_api_key_here")

# Create a user
user = client.users.create(external_id="alice")
print(f"User: {user.name} ({user.id})")

# Ingest conversation messages
resp = client.memory.ingest(
    user.id,
    messages=[
        {"role": "user", "content": "Hi, I am Alice from Acme Corp."},
        {"role": "assistant", "content": "Hello Alice! How can I help you today?"},
    ],
)
print(f"Ingested {resp.episode_count} episodes")

# Search across memory
results = client.graph.search(user.id, "Alice Acme Corp", types="episodes,facts")
for r in results:
    print(f"  - {r['content']}")
```

## Client API

### Sync (default)

```python
from openzync import OpenZep

client = OpenZep(api_key="...")

# ── Memory ──
client.memory.ingest(user_id, messages=[...])
client.memory.get_context(user_id, query="...")
client.memory.delete(user_id)

# ── Facts ──
client.facts.add(user_id, facts=[...])

# ── Graph ──
for node in client.graph.nodes(user_id):
    print(node.name)
detail = client.graph.node_detail(user_id, node_id)
client.graph.delete_node(user_id, node_id)
for edge in client.graph.edges(user_id, subject_id):
    print(edge.type)
comms = client.graph.communities(user_id)
results = client.graph.search(user_id, "query")

# ── Users ──
user = client.users.create(external_id="bob")
user = client.users.get(user_id)
user = client.users.update(user_id, name="New Name")
client.users.delete(user_id)
for user in client.users.list_iter():
    print(user["name"])

# ── Sessions ──
session = client.sessions.create(user_id, external_id="s1")
msgs = client.sessions.messages(user_id, session_id)
client.sessions.delete(user_id, session_id)
```

### Async

```python
import asyncio
from openzync import AsyncOpenZep

async def main():
    async with AsyncOpenZep(api_key="...") as client:
        resp = await client.memory.ingest(user_id, messages=[...])

asyncio.run(main())
```

## Error Handling

```python
from openzync import OpenZep
from openzync._errors import NotFoundError, RateLimitError

client = OpenZep(api_key="...")

try:
    user = client.users.get("non-existent-id")
except NotFoundError:
    print("User not found")
except RateLimitError:
    print("Rate limited — slow down")
```

## Pagination

List endpoints return an iterator that auto-fetches subsequent pages:

```python
# Iterate over all users (auto-paginated)
for user in client.users.list_iter():
    print(user["name"])
```

## LangChain Integration

LangChain developers can use OpenZep as a drop-in memory provider, graph retriever, and tool set.

```bash
pip install "openzync[langchain]"
```

### Chat Message History

Persist conversation history to OpenZep:

```python
from openzync import OpenZep
from openzync.integrations.langchain import OZChatMessageHistory
from langchain_core.messages import HumanMessage

client = OpenZep(api_key="...")
history = OZChatMessageHistory(
    session_id="session-1",
    user_id="user-abc",
    client=client,  # accepts both sync and async clients
)

history.add_message(HumanMessage(content="Hello!"))
print(history.messages)
```

### Memory

Use `OZMemory` as a standard LangChain `BaseMemory` inside chains:

```python
from openzync import OpenZep
from openzync.integrations.langchain import OZMemory
from langchain_core.messages import HumanMessage, AIMessage

client = OpenZep(api_key="...")
memory = OZMemory(
    session_id="session-1",
    user_id="user-abc",
    client=client,
    return_messages=True,   # False returns string
    memory_key="history",   # key in memory_variables
)

memory.save_context({"input": "Hi"}, {"output": "Hello!"})
context = memory.load_memory_variables({})
# context["history"] — list of BaseMessage or str depending on return_messages
```

### Graph Retriever

Use `OZGraphRetriever` as a LangChain retriever for RAG pipelines:

```python
from openzync.integrations.langchain import OZGraphRetriever

retriever = OZGraphRetriever(
    user_id="user-abc",
    client=client,
    k=5,                    # max results
    types="episodes,facts", # filter by node type
    score_threshold=0.7,    # minimum relevance score
)

docs = retriever.invoke("What does Alice know about Acme Corp?")
for doc in docs:
    print(doc.page_content, doc.metadata)
```

### Tool plugins

Expose OpenZep graph search and fact management as LangChain tools:

```python
from openzync.integrations.langchain.tools.graph import GraphSearchTool
from openzync.integrations.langchain.tools.facts import AddFactsTool

tools = [
    GraphSearchTool(client=client),
    AddFactsTool(client=client),
]

# Use with LangGraph / ReAct agents
# agent = create_react_agent(model, tools)
```

## Development

```bash
# Install with dev dependencies
pip install "openzync[dev]"

# Install everything (dev + langchain)
pip install "openzync[dev,langchain]"

# Run tests
pytest
```
