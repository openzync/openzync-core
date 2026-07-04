## Final Architecture (Locked)

### Constructor — Minimal, Stateless

```python
class FalkorGraphBackend(GraphBackend):
    def __init__(
        self,
        client: FalkorDB | None = None,
        max_traversal_depth: int = 2,
    ) -> None:
        self._client = client
        self._max_depth = min(max_traversal_depth, MAX_TRAVERSAL_DEPTH)
        self._schema_ensured: dict[str, bool] = {}
```

- **No org_id/project_id at construction** — the backend is pure stateless
- `_schema_ensured` is a `dict[str, bool]` keyed by graph key (not just a single flag), since a single backend instance may serve multiple orgs via the dispatcher

### Per-Method Resolution

Every method resolves its graph lazily from the params it already receives:

```python
async def create_entity(self, org_id, project_id, name, ...):
    graph = self._get_graph(org_id, project_id)
    # ... run Cypher against graph ...

def _get_graph(self, org_id: UUID, project_id: UUID):
    """Lazily select the per-tenant graph and ensure indexes."""
    key = f"openzync_{org_id}_{project_id}"
    if self._client is None:
        return None
    graph = self._client.select_graph(key)
    if not self._schema_ensured.get(key):
        self._ensure_schema(graph)
        self._schema_ensured[key] = True
    return graph
```

### Changes to `resolve_and_create()` — **Zero**

The `core/graph_backend.py` change is minimal — just add one registry entry:

```python
# In init_dispatcher():
from packages.graph_backend.falkordb import FalkorGraphBackend
dispatcher.register("falkordb", FalkorGraphBackend)
```

And in `resolve_and_create()`, add the backend-specific kwargs branch:

```python
elif backend_name == "falkordb":
    if falkordb_client is not None:
        kwargs["client"] = falkordb_client
    if org_config is not None and org_config.graph_max_traversal_depth is not None:
        kwargs["max_traversal_depth"] = org_config.graph_max_traversal_depth
```

### Per-Method Symmetry

Every ABC method follows this pattern:
1. `graph = self._get_graph(org_id, project_id)` → resolves the isolated graph
2. `graph.query("...", params)` → executes against that graph only
3. Parse results → return dict

The `org_id` and `project_id` parameters are consumed **only** to build the graph key — never in Cypher WHERE clauses.

---

## Final File Change Summary

| File | Change | Complexity |
|---|---|---|
| **`packages/graph_backend/falkordb.py`** | CREATE ~500-600 lines | High — all 11 ABC methods |
| **`packages/graph_backend/__init__.py`** | MODIFY +3 lines | Low |
| **`core/config.py`** | MODIFY +4 settings | Low |
| **`core/graph_backend.py`** | MODIFY +~15 lines | Low — registry + kwargs branch |
| **`main.py`** (lifespan) | MODIFY +~15 lines | Low — pool setup/teardown |
| **`dependencies/services.py`** | MODIFY +2 lines | Low — pass client from app.state |
| **`requirements.txt`** | MODIFY +1 line | Low |
| **`tests/unit/test_graph_backend_falkordb.py`** | CREATE ~400 lines | Medium |
| **`docs/adr/falkordb-graph-backend.md`** | CREATE ~80 lines | Low |

---

## Ready for Implementation

The architecture is fully locked:

1. **Tenant isolation**: Per-tenant graph keys `openzync_{org_id}_{project_id}` — database-level, zero overhead
2. **Connection pool**: Single app-level `BlockingConnectionPool` — efficient socket management
3. **Traversal**: `algo.bfs()` for single-type/all, Cypher variable-length paths for multi-type — single round-trip either way
4. **Schema bootstrap**: Lazy per-graph-key, idempotent, catches "already exists"
5. **State**: Fully stateless backend — dispatch-safe, no instance per tenant needed
6. **Upsert**: `MERGE` with `ON CREATE SET` / `ON MATCH SET` — same type-upgrade guard as other backends
7. **Search**: RediSearch BM25 full-text via `CALL db.idx.fulltext.queryNodes()`
8. **Pagination**: Offset-based with base64 cursors
9. **Attributes stored as JSON strings** parsed with `orjson`

I'm ready to generate the implementation files when you give the go-ahead. To summarize delivery order:

**Phase 1** → `falkordb.py` + `__init__.py` + unit tests  
**Phase 2** → `core/config.py` + `core/graph_backend.py` wiring  
**Phase 3** → `main.py` lifespan + `dependencies/services.py` DI  
**Phase 4** → ADR + `requirements.txt`
