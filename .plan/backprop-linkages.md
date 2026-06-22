## Industry-Grade Entity Reconciliation — Full Plan

### Design Principles

1. **Prevention over correction** — resolve at write time so duplicates rarely form
2. **Semantic understanding** — beyond string matching, use embeddings to catch "John Smith" ≡ "Dr. John"
3. **Project-scoped entities** — unique constraint includes `project_id`; resolution searches current project only
4. **Existing infrastructure** — reuse `org_config.embedding_backend`, `resolve_backend()`, litellm patterns
5. **Observable** — every resolution decision is logged with confidence and strategy used

---

### Architecture

```
extract_entities worker
  │
  ├─ EntityResolver.resolve(org_id, project_id, name, type, summary)
  │   │
  │   ├─ 1. Exact match → upsert (existing behavior, unchanged)
  │   │     (ON CONFLICT (org, project, name) DO UPDATE)
  │   │
  │   ├─ 2. No exact match → EmbeddingService.embed(name + summary)
  │   │   └─ resolve_backend(provider=org_cfg.embedding_backend)
  │   │
  │   ├─ 3. GraphBackend.search_similar_entities(query_embedding, query_text)
  │   │   ├─ pgvector cosine similarity (embedding <=> query)
  │   │   └─ pg_trgm similarity (fallback for entities without embeddings)
  │   │
  │   ├─ 4. Match above threshold? → merge into existing entity
  │   │   └─ GraphBackend.create_entity() with canonical name (upsert)
  │   │   └─ Add alias: original_name → canonical_name
  │   │
  │   └─ 5. No match → create_entity() + update_embedding()
  │
  └─ Existing relationship creation, episode linking, fact chaining unchanged
```

---

### Phase 1 — Schema Migration

**New file:** `migrations/versions/0013_entity_reconciliation.py`

```sql
-- 1. Enable pgvector (idempotent)
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. Add embedding column (1536 is default for text-embedding-3-small,
--    actual dimension comes from org_config.embedding_dim at runtime)
ALTER TABLE graph_entities ADD COLUMN embedding vector(1536);

-- 3. Add IVFFlat index for approximate nearest-neighbor search
--    (lists = sqrt(n_rows) for small orgs; we use a fixed reasonable default)
CREATE INDEX CONCURRENTLY idx_graph_entities_embedding
    ON graph_entities USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- 4. Drop old unique constraint, add project-scoped constraint
ALTER TABLE graph_entities DROP CONSTRAINT IF EXISTS graph_entities_org_name_key;
ALTER TABLE graph_entities ADD CONSTRAINT graph_entities_org_project_name_key
    UNIQUE (organization_id, project_id, name);

-- 5. New aliases table
CREATE TABLE graph_entity_aliases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID NOT NULL REFERENCES graph_entities(id) ON DELETE CASCADE,
    alias VARCHAR(500) NOT NULL,
    organization_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, LOWER(alias))
);
CREATE INDEX idx_entity_aliases_lookup
    ON graph_entity_aliases (organization_id, LOWER(alias));
```

**Migration notes:**
- The old constraint `(org, name)` is **stricter** than the new `(org, project, name)` — data already satisfies it, so no dedup needed before the new constraint
- Entities with the same name in different projects are now separate rows (desired)
- `embedding` column starts `NULL`; backfilled on next entity extraction
- `CONCURRENTLY` on vector index to avoid locking the table during migration

---

### Phase 2 — Embedding Service

**New file:** `services/embedding_service.py`

```python
class EmbeddingService:
    """Generate text embeddings via the configured BYOK backend.

    Uses org_config.embedding_backend / embedding_model / embedding_dim
    to create an LLM client via resolve_backend().
    """

    def __init__(
        self,
        backend: Any,         # resolved via resolve_backend(provider=...)
        dimension: int = 1536,
    ) -> None:
        self._backend = backend
        self._dimension = dimension

    async def embed(self, text: str) -> list[float]:
        """Generate embedding vector for a single text string."""
        response = await self._backend.embed(text)
        vec = response.embedding
        if len(vec) != self._dimension:
            raise ValueError(
                f"Expected dimension {self._dimension}, got {len(vec)}"
            )
        return vec

    @property
    def dimension(self) -> int:
        return self._dimension
```

- Uses the same `resolve_backend()` pattern as `embed_fact.py` and `embed_episode.py`
- Backend could be OpenAI, Ollama, or any litellm-supported provider
- One-shot usage in the worker — no long-lived service needed

---

### Phase 3 — GraphBackend Changes

**Modified:** `packages/graph_backend/interface.py`

Add three new abstract methods:

```python
@abstractmethod
async def search_similar_entities(
    self,
    org_id: UUID,
    project_id: UUID,
    query_text: str,
    query_embedding: list[float] | None = None,
    threshold: float = 0.85,
    limit: int = 5,
) -> list[dict]:
    """Search for existing entities similar to a query.

    Uses a two-strategy approach:
    1. pgvector cosine similarity on embedding (when query_embedding provided)
    2. pg_trgm similarity on name (fallback for entities without embeddings)

    Returns entities sorted by similarity descending, each with a
    ``similarity`` float and ``strategy`` key ("vector" | "trigram").
    """

@abstractmethod
async def update_embedding(
    self,
    entity_id: UUID,
    embedding: list[float],
) -> None:
    """Store/update the embedding vector for an entity."""

@abstractmethod
async def add_alias(
    self,
    entity_id: UUID,
    alias: str,
    org_id: UUID,
) -> None:
    """Register an alias name for an entity (for exact-match lookup)."""
```

**Modified:** `packages/graph_backend/postgres.py`

Implement `search_similar_entities`:

```python
async def search_similar_entities(
    self,
    org_id: UUID,
    project_id: UUID,
    query_text: str,
    query_embedding: list[float] | None = None,
    threshold: float = 0.85,
    limit: int = 5,
) -> list[dict]:
    results: dict[str, dict] = {}

    # Strategy 1: pgvector cosine similarity
    if query_embedding:
        vec_str = f"[{','.join(str(v) for v in query_embedding)}]"
        rows = await self._db.execute(text("""
            SELECT id, name, entity_type, summary,
                   1 - (embedding <=> :vec::vector) AS sim
            FROM graph_entities
            WHERE organization_id = :org_id
              AND project_id = :project_id
              AND is_merged = false
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> :vec::vector) > :threshold
            ORDER BY sim DESC
            LIMIT :limit
        """), {...})
        for r in rows:
            results[str(r[0])] = {..., "similarity": r[4], "strategy": "vector"}

    # Strategy 2: pg_trgm similarity (catches entities without embeddings)
    rows = await self._db.execute(text("""
        SELECT id, name, entity_type, summary,
               similarity(LOWER(name), LOWER(:query)) AS sim
        FROM graph_entities
        WHERE organization_id = :org_id
          AND project_id = :project_id
          AND is_merged = false
          AND similarity(LOWER(name), LOWER(:query)) > 0.7
        ORDER BY sim DESC
        LIMIT :limit
    """), {...})
    for r in rows:
        eid = str(r[0])
        sim = r[4]
        if eid not in results or sim > results[eid]["similarity"]:
            results[eid] = {..., "similarity": sim, "strategy": "trigram"}

    return sorted(results.values(), key=lambda x: x["similarity"], reverse=True)
```

Implement `update_embedding` and `add_alias` as straightforward UPDATE / INSERT.

---

### Phase 4 — Entity Resolution Service

**New file:** `services/entity_resolver.py`

```python
@dataclass
class EntityResolutionResult:
    entity: dict
    resolution: Literal["exact", "merged", "created"]
    matched_to: str | None   # canonical name if merged, None otherwise
    confidence: float | None # similarity score if merged, None otherwise

class EntityResolver:
    """Resolve an entity name against existing entities before creation.

    Three-stage strategy:
    1. Exact match → upsert (existing behavior)
    2. Semantic match → merge into existing
    3. No match → create new with embedding
    """

    MERGE_THRESHOLD: float = 0.85
    """Minimum similarity score to auto-merge (heuristic: safe zone)."""

    def __init__(
        self,
        backend: GraphBackend,
        embedding_service: EmbeddingService,
        entity_repo: EntityRepository,
    ) -> None:
        self._backend = backend
        self._embedder = embedding_service
        self._repo = entity_repo

    async def resolve(
        self,
        org_id: UUID,
        project_id: UUID,
        name: str,
        entity_type: str,
        summary: str | None = None,
    ) -> EntityResolutionResult:
        name_normalized = name.lower().strip()

        # ── Step 1: Exact match via alias table ──
        exact = await self._repo.get_entity_by_name(org_id, project_id, name)
        if exact is not None:
            entity = await self._backend.create_entity(
                org_id, project_id, exact["name"], entity_type, summary,
            )
            return EntityResolutionResult(
                entity=entity, resolution="exact", matched_to=exact["name"], confidence=1.0,
            )

        # ── Step 2: Semantic match ──
        embed_text = f"{name} | {summary or ''}"
        query_vec = await self._embedder.embed(embed_text)

        similar = await self._backend.search_similar_entities(
            org_id=org_id,
            project_id=project_id,
            query_text=name,
            query_embedding=query_vec,
            threshold=self.MERGE_THRESHOLD,
            limit=1,
        )

        if similar:
            best = similar[0]
            # Merge into canonical — upsert with canonical name
            entity = await self._backend.create_entity(
                org_id=org_id,
                project_id=project_id,
                name=best["name"],  # canonical name
                entity_type=entity_type,
                summary=summary,
            )
            # Store alias so future exact lookups find this entity
            await self._backend.add_alias(
                entity_id=UUID(entity["id"]),
                alias=name,
                org_id=org_id,
            )
            # Update embedding with combined text
            combined = f"{best['name']} | {name} | {summary or ''}"
            combined_vec = await self._embedder.embed(combined)
            await self._backend.update_embedding(UUID(entity["id"]), combined_vec)

            logger.info("entity_resolution.merged", ...)
            return EntityResolutionResult(
                entity=entity, resolution="merged",
                matched_to=best["name"], confidence=best["similarity"],
            )

        # ── Step 3: Create new ──
        entity = await self._backend.create_entity(
            org_id, project_id, name, entity_type, summary,
        )
        await self._backend.update_embedding(UUID(entity["id"]), query_vec)

        return EntityResolutionResult(
            entity=entity, resolution="created", matched_to=None, confidence=None,
        )
```

**Key design points:**
- Exact match checked FIRST (cheapest, fastest)
- Embedding generated ONCE per entity, reused for both search and storage
- Alias table enables future exact-name lookups for merged names
- Combined embedding merges semantic information from canonical + variant names
- Logs every resolution with strategy, confidence, and entity IDs

---

### Phase 5 — Update Entity Extraction Worker

**Modified:** `workers/tasks/extract_entities.py`

In the worker, where it currently calls `entity_repo.upsert_entity()`:

```python
# Before:
node = await entity_repo.upsert_entity(
    org_id=..., project_id=..., name=..., entity_type=..., summary=...,
)

# After:
embedding_service = EmbeddingService(
    backend=await resolve_backend(provider=org_cfg.embedding_backend,
                                   org_config=org_cfg.to_embedding_config_dict()),
    dimension=org_cfg.embedding_dim or 1536,
)
resolver = EntityResolver(
    backend=PostgresGraphBackend(_db),
    embedding_service=embedding_service,
    entity_repo=entity_repo,
)

result = await resolver.resolve(
    org_id=uuid.UUID(org_id),
    project_id=uuid.UUID(project_id),
    name=normalized_name,
    entity_type=entity_type,
    summary=summary,
)
node = result.entity  # dict with id, name, etc. — same shape as before
```

The rest of the worker (relationship creation, episode linking, fact chaining) stays **unchanged** since the return shape of `node` is identical.

The `org_cfg` is already fetched by the worker on line 160-164 — we just need to pass the embedding config.

---

### Phase 6 — Merge APIs (Cleanup)

**New file:** `schemas/entity_merge.py`

```python
class DuplicateCandidate(BaseModel):
    id: UUID
    name: str
    entity_type: str
    similarity: float
    strategy: str  # "exact" | "trigram" | "vector"

class DuplicateCluster(BaseModel):
    entities: list[DuplicateCandidate]
    merged_count: int

class MergePreviewResponse(BaseModel):
    clusters: list[DuplicateCluster]
    total_duplicates: int

class MergeRequest(BaseModel):
    canonical_id: UUID
    duplicate_ids: list[UUID]

class MergeResponse(BaseModel):
    canonical_id: UUID
    merged_count: int
    relationships_rewired: int
```

**New file:** `routers/entity_merge.py`

```python
router = APIRouter(prefix="/orgs/{org_id}/entities", tags=["entity-resolution"])

@router.get("/duplicates", response_model=MergePreviewResponse)
async def preview_duplicates(
    org_id: UUID,
    project_id: UUID,
    similarity_threshold: float = Query(0.85, ge=0.0, le=1.0),
    # auth + service deps
) -> MergePreviewResponse:
    """Preview potential duplicate entity clusters for review."""
    ...

@router.post("/merge", response_model=MergeResponse)
async def merge_entities(
    org_id: UUID,
    body: MergeRequest,
    # auth + service deps
) -> MergeResponse:
    """Merge a set of duplicate entities into a canonical one."""
    ...

@router.post("/merge/preview", response_model=MergePreviewResponse)
async def preview_merge(
    org_id: UUID,
    body: MergeRequest,
    # auth + service deps
) -> MergePreviewResponse:
    """Preview what would happen if these entities were merged."""
    ...
```

The merge logic reuses the existing `_merge_cluster` from `merge_duplicate_entities.py` (canonical selection, relationship rewiring, soft-delete, audit log), refactored into a shared `EntityMergeService`.

---

### Phase 7 — Tests

| Test file | Coverage |
|-----------|----------|
| `tests/unit/test_embedding_service.py` | Embedding generation, dimension validation, error handling |
| `tests/unit/test_entity_resolver.py` | All three resolution paths (exact, merged, created), alias storage, threshold behavior |
| `tests/unit/test_graph_backend_postgres.py` | `search_similar_entities` with pgvector + pg_trgm, `update_embedding`, `add_alias` |
| `tests/unit/test_entity_merge_api.py` | Duplicate preview, merge execution, audit log |
| `tests/evals/test_entity_resolution.py` | End-to-end resolution scenarios with real DB |

---

### Summary of files changed/created

| Action | File | Purpose |
|--------|------|---------|
| **New** | `migrations/versions/0013_entity_reconciliation.py` | pgvector, embedding column, new unique constraint, aliases table |
| **New** | `services/embedding_service.py` | Embedding generation via resolve_backend |
| **New** | `services/entity_resolver.py` | Three-stage entity resolution orchestrator |
| **Modified** | `packages/graph_backend/interface.py` | Add `search_similar_entities`, `update_embedding`, `add_alias` |
| **Modified** | `packages/graph_backend/postgres.py` | Implement new interface methods |
| **Modified** | `repositories/entity_repository.py` | Add `get_by_alias`, `get_exact_match` helpers |
| **Modified** | `workers/tasks/extract_entities.py` | Use `EntityResolver` instead of direct `upsert_entity` |
| **New** | `schemas/entity_merge.py` | Pydantic schemas for merge APIs |
| **New** | `routers/entity_merge.py` | Merge preview + execution endpoints |
| **Modified** | `services/api/main.py` | Register new router |
| **New** | `tests/unit/test_embedding_service.py` | Embedding service tests |
| **New** | `tests/unit/test_entity_resolver.py` | Resolution orchestrator tests |
| **Modified** | `tests/unit/test_graph_backend_dispatcher.py` | Tests for new interface methods |
| **New** | `tests/unit/test_entity_merge_api.py` | Merge API tests |

---

### Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Embedding API call fails during entity extraction | `@with_retry` on the worker catches it; entity is still created (without embedding). Next extraction will attempt embedding again. |
| False positive merge (two different entities merged incorrectly) | Merge uses high threshold (0.85). Alias table preserves original name. Audit log tracks every merge with before/after snapshot. Soft-delete (`is_merged`) allows 7-day recovery. |
| pgvector index build time on large tables | `CREATE INDEX CONCURRENTLY` avoids locking. Can be run as a background migration step. |
| `embedding_dim` mismatch between org_config and model | `EmbeddingService` validates dimension on every call and raises a clear error. |
| Pre-existing duplicates from before this feature | Phase 6 Merge APIs provide a cleanup path. |

---

### Future enhancements (not in scope for this plan)

- **LLM-assisted verification** for medium-confidence matches (0.7-0.85) — currently we use a single threshold; a follow-up could add a verification LLM call
- **Cross-project entity linking API** — explicit API to link entities across projects within an org
- **Scheduled batch reconciliation** — periodic run of the merge preview (only if write-time resolution proves insufficient)
- **Merge quality score** — track how often merges are correct via user feedback
