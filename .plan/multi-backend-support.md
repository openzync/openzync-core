Now I have the full picture. Here's the complete plan:

---

## Plan: `GraphBackendDispatcher` — Multi-Graph Backend Registry

### Design Decisions (confirmed)

| Decision | Value |
|---|---|
| **Pattern** | Dispatcher/Registry (Option B) |
| **Routing strategy** | Per-org config → `org_config.graph_backend` |
| **Dispatcher holds** | Backend **classes** (not instances) |
| **Backend instances** | Created per-request with request-scoped `AsyncSession` |
| **Dispatcher lifecycle** | App singleton in `app.state.graph_backend_dispatcher` |
| **File location** | `core/graph_backend.py` (refactor existing) |
| **`app.state.graph_backend`** | Replace with dispatcher — remove lifespan-scoped backend |
| **Backends now** | Only `PostgresGraphBackend` — but trivially extensible |

---

### What to Build

#### 1. `GraphBackendDispatcher` class in `core/graph_backend.py`

Replace the existing `init_graph_backend()` factory function with a class:

```python
class GraphBackendDispatcher:
    """Registry of backend classes + per-org resolution."""

    def __init__(self) -> None:
        self._registry: dict[str, type[GraphBackend]] = {}

    def register(self, name: str, backend_cls: type[GraphBackend]) -> None:
        """Register a backend class (e.g. 'postgres' → PostgresGraphBackend)."""
        self._registry[name] = backend_cls

    def resolve_and_create(
        self,
        org_config: OrgConfigBase | None,
        db: AsyncSession,
    ) -> GraphBackend | None:
        """Resolve backend from org_config, create a request-scoped instance.

        - If ``org_config`` is ``None`` or ``graph_backend`` is not set
          or ``"none"`` → returns ``None`` (graph disabled).
        - If ``org_config.graph_backend == "postgres"`` → creates a
          ``PostgresGraphBackend`` with the given ``db`` and reads
          ``graph_max_traversal_depth`` from org_config.
        - If the backend name is unknown → raises ``ValueError``.
        - Backend-specific kwargs (like max_traversal_depth) are
          resolved internally.
        """
        ...
```

And a top-level factory for lifespan init:

```python
def init_dispatcher() -> GraphBackendDispatcher:
    """Create and populate the dispatcher with all registered backends."""
    dispatcher = GraphBackendDispatcher()
    dispatcher.register("postgres", PostgresGraphBackend)
    return dispatcher
```

#### 2. Update `services/api/main.py` lifespan

**Before:**
```python
session_factory = get_async_session(db_engine)
async with session_factory() as graph_session:
    try:
        app.state.graph_backend = await init_graph_backend(db=graph_session)
    except Exception:
        app.state.graph_backend = None
    yield
```

**After:**
```python
app.state.graph_backend_dispatcher = init_dispatcher()
yield  # No session needed — backends created per-request
```

This fixes the existing concurrency bug (shared `AsyncSession` across all requests).

#### 3. Update all 3 callers

Each caller currently hardcodes `PostgresGraphBackend(db=db)`. Change to use the dispatcher:

**`dependencies/services.py`** (the main injection point):
```python
async def get_graph_service(
    request: Request,
    org_config: OrgConfigBase = Depends(get_org_config),
    db: AsyncSession = Depends(get_db),
    webhook: WebhookService = Depends(get_webhook_service),
) -> GraphService:
    dispatcher: GraphBackendDispatcher = request.app.state.graph_backend_dispatcher
    graph_backend = dispatcher.resolve_and_create(org_config, db)
    return GraphService(graph_backend=graph_backend, ...)
```

**`routers/search.py`** (line 105):
```python
# Before:
graph_backend = PostgresGraphBackend(db=db)
# After:
graph_backend = dispatcher.resolve_and_create(org_config, db)
```

**`routers/context.py`** (line 110): Same pattern.

#### 4. Update `packages/graph_backend/__init__.py` exports

No changes needed — `GraphBackend` and `PostgresGraphBackend` remain the main exports. The dispatcher lives in `core/`.

#### 5. Tests

- **`tests/unit/test_graph_service.py`** — Update mocks. Currently mocks `GraphBackend` directly, which still works (resolver returns an instance of the same interface).
- **`tests/unit/test_context_service.py`** — Same, `mock_graph_backend = AsyncMock(spec=GraphBackend)` still valid.
- **New test**: `tests/unit/test_graph_backend_dispatcher.py` — Test the dispatcher itself:
  - Resolution with valid config → returns correct backend class
  - Resolution with `graph_backend="none"` → returns `None`
  - Resolution with missing config → raises `ValueError`
  - Registration of duplicate name → overwrites (or raises?)

---

### File Change Summary

| File | Change Type | Notes |
|---|---|---|
| `core/graph_backend.py` | **Rewrite** | Replace `init_graph_backend()` with `GraphBackendDispatcher` class + `init_dispatcher()` factory |
| `services/api/main.py` | **Edit** 5 lines | Remove lifespan-scoped session init, add `init_dispatcher()` |
| `dependencies/services.py` | **Edit** ~12 lines | Add `request` + `org_config` deps, use dispatcher |
| `routers/search.py` | **Edit** ~3 lines | Use dispatcher instead of direct instantiation |
| `routers/context.py` | **Edit** ~3 lines | Use dispatcher instead of direct instantiation |
| `tests/unit/test_graph_backend_dispatcher.py` | **New** | Unit tests for the dispatcher itself |
| `tests/unit/test_graph_service.py` | **Check** | Likely no changes needed (mocks GraphBackend ABC) |
| `tests/unit/test_context_service.py` | **Check** | Same — mocks at ABC level |
| `docs/` | **Update** ADR or implementation doc | Optional — document the dispatcher pattern |

---

### Future Extensibility (No Code Yet)

Adding a new backend in the future requires:
1. Create `packages/graph_backend/neo4j.py` implementing `GraphBackend`
2. Register it: `dispatcher.register("neo4j", Neo4jGraphBackend)` in `init_dispatcher()`
3. Set `org_config.graph_backend = "neo4j"` for the target org

No callers change. No wiring changes.

---

### Migration Risk & Backward Compatibility

- **Low risk.** `app.state.graph_backend` is only written in `main.py`, never read from elsewhere. No existing code depends on the lifespan-scoped instance.
- All callers already create request-scoped `PostgresGraphBackend` instances — the dispatcher just changes how they get created.
- The `GraphBackend` ABC is unchanged — no service code needs modification.

---

Shall I proceed with implementing this plan?
