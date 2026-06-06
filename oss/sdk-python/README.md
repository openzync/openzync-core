# OpenZep Python SDK

[![PyPI](https://img.shields.io/pypi/v/openzep-py)](https://pypi.org/project/openzep-py/)
[![Python](https://img.shields.io/pypi/pyversions/openzep-py)](https://pypi.org/project/openzep-py/)
[![License](https://img.shields.io/pypi/l/openzep-py)](https://www.apache.org/licenses/LICENSE-2.0)

Python SDK for [OpenZep](https://github.com/thelinkai/openzep) — the open-source agent memory platform with persistent, queryable, graph-based memory for AI agents.

## Installation

```bash
pip install openzep-py
```

Requires Python 3.11+.

## Quick Start

```python
from openzep import OpenZep

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
from openzep import OpenZep

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
from openzep import AsyncOpenZep

async def main():
    async with AsyncOpenZep(api_key="...") as client:
        resp = await client.memory.ingest(user_id, messages=[...])

asyncio.run(main())
```

## Error Handling

```python
from openzep import OpenZep
from openzep._errors import NotFoundError, RateLimitError

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

## Development

```bash
# Install with dev dependencies
pip install "openzep-py[dev]"

# Run tests
pytest
```
