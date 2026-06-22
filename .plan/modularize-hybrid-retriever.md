## Final Implementation Plan

### Phase 1 — Add `retrieve_graph` to the interface

**File**: `packages/graph_backend/interface.py`

Add one new abstract method to `GraphBackend`:

```python
@abstractmethod
async def retrieve_graph(
    self,
    org_id: UUID,
    project_id: UUID,
    query: str,
    *,
    match_limit: int = 5,
    max_depth: int = 2,
    max_results: int = 50,
) -> list[dict]:
    """Search entities matching query, then BFS-traverse outward.

    Combines entity text search with graph traversal so each backend
    can use its native strengths (recursive CTE, Cypher, etc.).

    Args:
        org_id: Organisational scope.
        project_id: Project scope.
        query: Free-text search string.
        match_limit: Max entities to match before traversal.
        max_depth: Max BFS depth from each matched entity.
        max_results: Max total results to return.

    Returns:
        Entity dicts with id, name, type, summary, and distance keys.
        Distance 0 = directly matched, 1+ = reached via traversal.
        Sorted by distance ascending.
    """
```

**Verification**: Existing `search_entities()` and `traverse()` stay untouched. `GraphBackend` now has 9 abstract methods.

---

### Phase 2 — Implement `retrieve_graph` in PostgresGraphBackend

**File**: `packages/graph_backend/postgres.py`

Move the search-entities-then-BFS logic from `HybridRetriever._graph_bfs_search()` (lines 519–593 of `hybrid_retriever.py`) into a new `retrieve_graph()` method on `PostgresGraphBackend`.

The method:
1. Calls `self.search_entities(org_id, project_id, query, limit=match_limit)`
2. For each matched entity, calls `self.traverse(org_id, project_id, start_node_id=..., max_depth=max_depth)`
3. Shapes results with `distance` key (0 for matched, 1+ for traversed)
4. Deduplicates by `id`, sorts by `distance`, limits to `max_results`

**Untouched**: `search_entities()`, `traverse()`, and all other existing methods.

---

### Phase 3 — Add `create_all_backends` to GraphBackendDispatcher

**File**: `core/graph_backend.py`

Add to `GraphBackendDispatcher`:

```python
def create_all_backends(
    self,
    db: AsyncSession,
    org_config: OrgConfigBase | None = None,
) -> list[GraphBackend]:
    """Create one instance of every registered backend.

    Args:
        db: Request-scoped AsyncSession.
        org_config: Optional org config for backend-specific kwargs.

    Returns:
        List of initialised GraphBackend instances (may be empty).
    """
    instances: list[GraphBackend] = []
    for backend_name, cls in self._registry.items():
        kwargs: dict = {}
        if backend_name == "postgres" and org_config is not None:
            if org_config.graph_max_traversal_depth is not None:
                kwargs["max_traversal_depth"] = org_config.graph_max_traversal_depth
        instances.append(cls(db=db, **kwargs))
    return instances
```

**`resolve_and_create()`** stays unchanged for backward compatibility.

---

### Phase 4 — Update HybridRetriever

**File**: `services/hybrid_retriever.py`

Two changes:

#### 4a. Constructor: accept `graph_backends` list

```python
# Before:
graph_backend: GraphBackend | None = None,
self._graph_backend = graph_backend

# After:
graph_backends: list[GraphBackend] | None = None,
self._graph_backends = graph_backends or []
```

#### 4b. Replace `_graph_bfs_search()` body

```python
async def _graph_bfs_search(
    self, query: str, project_id: UUID
) -> list[dict[str, Any]]:
    if not self._graph_backends:
        logger.debug("hybrid_retriever.graph_bfs_unavailable")
        return []

    results = await asyncio.gather(
        *[
            b.retrieve_graph(
                org_id=self._org_id,
                project_id=project_id,
                query=query,
            )
            for b in self._graph_backends
        ],
        return_exceptions=True,
    )

    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning("hybrid_retriever.backend_failed", exc_info=r)
            continue
        for item in r:
            item_id = item.get("id", "")
            if item_id and item_id not in seen:
                seen.add(item_id)
                merged.append(item)

    merged.sort(key=lambda x: x.get("distance", 99))
    return merged[:MAX_BFS_RESULTS]
```

**Everything else untouched**: `hybrid_search()`, `_vector_search_*`, `_bm25_search_*`, `_rrf_merge()`, `_embed_query()`, `_execute_ranked_query()`.

**Add import**: `import asyncio` at the top of the file.

---

### Phase 5 — Update callers (routers + dependency)

#### 5a. `routers/search.py` (lines 104–108)

```python
# Before:
dispatcher = request.app.state.graph_backend_dispatcher
graph_backend = dispatcher.resolve_and_create(org_config, db)
retriever = HybridRetriever(
    db, org_id, graph_backend=graph_backend, org_config=org_config
)

# After:
dispatcher = request.app.state.graph_backend_dispatcher
graph_backends = dispatcher.create_all_backends(db, org_config)
retriever = HybridRetriever(
    db, org_id, graph_backends=graph_backends, org_config=org_config
)
```

#### 5b. `routers/context.py` (lines 109–112) — same pattern

#### 5c. `dependencies/services.py` (lines 180–184) — same pattern in `get_graph_service()`

Update the `GraphService` constructor call to pass `graph_backends` list instead of single backend.

---

### Phase 6 — Update tests

**File**: `tests/unit/test_hybrid_retriever.py` (or wherever `HybridRetriever` tests live)

- Update any test that passes `graph_backend=Mock()` to pass `graph_backends=[Mock()]`
- Add test: `create_all_backends` returns instances of all registered backends
- Add test: multi-backend results are merged and deduplicated by `id`
- Add test: one backend failing doesn't crash the whole search (covered by `return_exceptions=True`)
- Add test: `graph_backends=[]` returns empty gracefully

**File**: `tests/unit/test_graph_backend_dispatcher.py`

- Add test: `create_all_backends` with empty registry → empty list
- Add test: `create_all_backends` with one registered backend → `[PostgresGraphBackend]`

---

### Dependency order

```
Phase 1 (interface)
    ↓
Phase 2 (postgres impl)
    ↓
Phase 3 (dispatcher)
    ↓
Phase 4 (hybrid_retriever) ── depends on 1 + 2 + 3
    ↓
Phase 5 (routers) ── depends on 3 + 4
    ↓
Phase 6 (tests) ── depends on everything
```

Phases 1 and 3 are independent and could be done in parallel. Phase 2 depends on 1. Phase 4 depends on 1+2+3.

**No schema changes, no DB migrations, no config changes.**
