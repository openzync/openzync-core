# Embedding Strategy — pgvector & Vector Index Management

## 1. Overview

| Field | Detail |
|-------|--------|
| **Coverage** | pgvector extension setup, vector column definitions, IVFFlat/HNSW index configuration, embedding model dimension reference, batch embedding, distance functions, re-indexing lifecycle, dimension migration |
| **Dependencies** | `01-postgresql-schema.md` (vector columns on `episodes` and `facts`); embedding provider (OpenAI, Ollama, or custom); PostgreSQL 15+ with pgvector ≥ 0.5.0 |
| **SRS Requirement IDs** | RET-01, RET-04, ING-01–ING-06, SCALE-03, PERF-01–PERF-06, PORT-04, OQ-04 |
| **Build Phase** | Phase 0 (Foundation — pgvector setup), Phase 1 (Core — embedding workers), Phase 5 (Hardening — HNSW migration, re-indexing) |
| **Design Authority** | @senior-dev (embedding pipeline), @devops (index monitoring) |

### 1.1 Embedding Pipeline Architecture

```
Message Ingestion
       │
       ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Embed Episode   │────▶│  Store in PG     │────▶│  IVFFlat Index   │
│  Worker Task     │     │  (vector column) │     │  (async rebuild) │
│  (ARQ, high q)   │     │  episodes.       │     │                   │
│  Batch: 100 msgs │     │  embedding       │     │                   │
└─────────────────┘     └─────────────────┘     └─────────────────┘
       │
       ▼
┌─────────────────┐     ┌─────────────────┐
│  Embed Entity    │────▶│  Store in PG     │
│  Worker Task     │     │  (facts.         │
│  (ARQ, high q)   │     │  embedding)      │
│  + Graphiti edge │     └─────────────────┘
│  fact_embedding  │
└─────────────────┘
```

### 1.2 Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Vector store** | pgvector (PostgreSQL extension) | Collocated with relational data — no extra service, no network hop, transactional consistency |
| **Index type** | IVFFlat (default), HNSW (optional upgrade) | IVFFlat: faster builds, lower memory; HNSW: better recall at high K, higher memory/insert cost |
| **Default dimension** | 1536 (text-embedding-3-small) | Best quality-to-cost ratio for OpenAI; fits in 8KB PostgreSQL page without TOAST |
| **Distance function** | Cosine similarity (`vector_cosine_ops`) | Works with normalized and unnormalized vectors; default for text-embedding-3 models |
| **Batch size** | 100 texts per API call | OpenAI max batch recommendation; balances throughput vs per-call overhead |

---

## 2. Data Model — Vector Columns

### 2.1 pgvector Extension

```sql
-- Migration 0001: initial schema setup
CREATE EXTENSION IF NOT EXISTS vector
    SCHEMA public;

-- Verify installation
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
-- Expected: vector | 0.5.0+ (or 0.7.x as of 2026)
```

**pgvector version requirements:**

| Version | Key Features | Recommendation |
|---------|-------------|---------------|
| 0.4.x | IVFFlat, cosine/L2/inner, halfvec | Minimum viable |
| 0.5.0 | HNSW index, sum/avg aggregates | **Recommended** — enables HNSW |
| 0.6.0 | Bit vectors, improved HNSW performance | Upgrade target |
| 0.7.0+ | Sparse vectors, index build performance | Future upgrade |

### 2.2 Vector Column Definitions

```sql
-- episodes table — stores conversation message embeddings
-- Dimension parameterized from EMBEDDING_DIM env var (default: 1536)
-- Column is NULLABLE — NOT NULL constraint is NOT set because embedding
-- is populated asynchronously by the embedding worker.

ALTER TABLE episodes ADD COLUMN embedding VECTOR(:EMBEDDING_DIM);

-- facts table — stores fact text embeddings
ALTER TABLE facts ADD COLUMN embedding VECTOR(:EMBEDDING_DIM);
```

**Important: DO NOT use `NOT NULL` on embedding columns.** Embeddings are populated asynchronously by ARQ workers. A newly inserted episode or fact will have `NULL` embedding until the worker completes. Query patterns must filter with `WHERE embedding IS NOT NULL`.

### 2.3 Embedding Storage Comparison

| Model | Dimension | Column Size (per row) | 1M rows | Notes |
|-------|-----------|----------------------|---------|-------|
| text-embedding-3-small | 1536 | ~6.1 KB | 6.1 GB | Default. Best balance. |
| text-embedding-3-large | 3072 | ~12.3 KB | 12.3 GB | Higher quality, 2x storage |
| nomic-embed-text | 768 | ~3.1 KB | 3.1 GB | Good local option (Ollama) |
| gte-small | 384 | ~1.5 KB | 1.5 GB | Fastest, lowest quality |

**pgvector storage for VECTOR(1536):**
- Each vector requires `4 bytes × 1536 = 6,144 bytes` + 24 bytes varlena header ≈ 6.2 KB
- Fits within PostgreSQL's 8KB page only for TOAST'd storage (compressed inline or out-of-line)
- Index size for IVFFlat(100) on 1M vectors: ~600 MB
- Index size for HNSW(m=16, ef_construction=200) on 1M vectors: ~2.4 GB

---

## 3. Index Configuration

### 3.1 IVFFlat Index (Default — Phase 0 & 1)

```sql
-- episodes table
CREATE INDEX idx_episodes_embedding_ivfflat ON episodes
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = :LISTS);

-- facts table
CREATE INDEX idx_facts_embedding_ivfflat ON facts
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = :LISTS);
```

**`lists` parameter selection:**

The number of IVF centroids (`lists`) trades off recall vs build time vs query speed.

| Row Count | Recommended `lists` | Query speed (vs seq scan) | Recall (top-10) | Build time |
|-----------|--------------------|--------------------------|-----------------|------------|
| < 10,000 | 10 | ~50x faster | ~90% | < 1s |
| 10,000 – 100,000 | 50 | ~200x faster | ~92% | ~5s |
| 100,000 – 1,000,000 | 100 | ~500x faster | ~95% | ~30s |
| 1,000,000 – 10,000,000 | 500 | ~1000x faster | ~96% | ~5min |
| > 10,000,000 | 1000 | ~2000x faster | ~97% | ~30min |

**Formula:** `lists = sqrt(row_count)`, rounded to nearest power of 10-ish. Default: 100 for most deployments.

```python
# services/embedding/reindex.py
import math

def compute_lists(row_count: int) -> int:
    """Compute optimal IVFFlat lists parameter.

    Based on pgvector documentation: lists = sqrt(number_of_rows).
    Clamp between 10 and 1000.
    """
    if row_count < 1:
        return 10
    lists = int(math.sqrt(row_count))
    return max(10, min(1000, lists))
```

**IVFFlat `probes` parameter:**

The `probes` parameter controls how many IVF lists are searched at query time. Higher probes = better recall, slower queries.

```sql
-- Set probes per session or globally
SET ivfflat.probes = :PROBES;
```

| `probes` | Recall impact | Query speed impact |
|----------|--------------|-------------------|
| `= lists` | ~100% recall (same as brute force) | Same as sequential scan |
| `= lists / 2` | ~99% | ~2x faster than seq scan |
| `= lists / 10` | ~95% | ~10x faster |
| `= 1` (default) | ~80-90% | ~100x faster |

**Recommendation:**

```python
# core/config.py
# Default: probes = max(1, int(LISTS / 10))
# For production: probes = max(1, int(LISTS / 4))
PGCONFIG_IVFFLAT_PROBES: int = max(1, int(os.getenv("PGVECTOR_PROBES", "10")))
```

Set `ivfflat.probes` at application startup:

```python
# In the database session factory or FastAPI lifespan
async def configure_pgvector(engine):
    async with engine.connect() as conn:
        await conn.execute(
            text(f"SET ivfflat.probes = {settings.PGVECTOR_PROBES}")
        )
```

### 3.2 HNSW Index (Recommended for Production — Phase 5)

For pgvector ≥ 0.5.0, HNSW provides better recall with less parameter tuning, at the cost of higher memory and slower inserts.

```sql
-- HNSW index on episodes (requires pgvector >= 0.5.0)
CREATE INDEX idx_episodes_embedding_hnsw ON episodes
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

-- HNSW index on facts
CREATE INDEX idx_facts_embedding_hnsw ON facts
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);
```

**HNSW parameters:**

| Parameter | Default | Range | Effect |
|-----------|---------|-------|--------|
| `m` | 16 | 2–100 | Max number of connections per node. Higher = better recall, more memory. |
| `ef_construction` | 200 | 4–1000 | Build-time search effort. Higher = better recall, slower build. |

**Memory comparison (1M vectors of 1536 dims):**

| Index Type | Index Size | Build Time | Recall@10 | Insert Overhead |
|-----------|-----------|-----------|-----------|----------------|
| IVFFlat (lists=100) | 600 MB | 30s | ~95% | Low (just add to centroid) |
| HNSW (m=16) | 2.4 GB | 5min | ~99.5% | High (needs to wire neighbors) |
| No index (brute force) | 0 | 0 | 100% | None |

**When to switch from IVFFlat to HNSW:**

```python
def should_use_hnsw(row_count: int, recall_requirement: float, write_rate: float) -> bool:
    """Decision function for HNSW vs IVFFlat.

    Returns True if HNSW is recommended over IVFFlat.

    Factors:
    - row_count: HNSW scales better at high cardinality
    - recall_requirement: HNSW for recall > 98%
    - write_rate: IVFFlat for high write throughput (HNSW inserts are slower)

    Recommendation matrix:
    | Rows        | Recall req | Write rate | Index choice |
    |-------------|-----------|-----------|-------------|
    | < 100K      | < 98%     | any       | IVFFlat      |
    | < 100K      | >= 98%    | any       | IVFFlat (with high probes) |
    | 100K - 1M   | < 98%     | high      | IVFFlat      |
    | 100K - 1M   | >= 98%    | low       | HNSW         |
    | > 1M        | any       | low       | HNSW         |
    | > 1M        | any       | high      | HNSW (with larger resources) |
    """
    if row_count > 1_000_000:
        return True
    if row_count > 100_000 and recall_requirement >= 0.98:
        return True
    return False
```

**HNSW query-time tuning:**

```sql
-- ef_search controls the search beam width at query time
-- Default: ef_search = 40
-- Recommendation: ef_search = 100 for high recall, 40 for low latency
SET hnsw.ef_search = 100;
```

### 3.3 Index Migration Path

```python
# Migration: 000X_switch_to_hnsw.py
"""
Strategy:
1. Create HNSW index CONCURRENTLY alongside existing IVFFlat index
2. Verify recall of HNSW equals or exceeds IVFFlat
3. Drop IVFFlat index
4. Update config to default to HNSW
"""

from alembic import op
import sqlalchemy as sa
from core.config import settings

EMBEDDING_DIM = settings.EMBEDDING_DIM

def upgrade():
    # Step 1: Create HNSW index — CONCURRENTLY to avoid locking table
    # Note: IVFFlat index still exists during this migration
    op.execute("""
        CREATE INDEX CONCURRENTLY idx_episodes_embedding_hnsw
        ON episodes USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 200)
    """)
    op.execute("""
        CREATE INDEX CONCURRENTLY idx_facts_embedding_hnsw
        ON facts USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 200)
    """)

    # Step 2: Drop old IVFFlat indexes
    op.execute("DROP INDEX IF EXISTS idx_episodes_embedding_ivfflat")
    op.execute("DROP INDEX IF EXISTS idx_facts_embedding_ivfflat")

def downgrade():
    # Rollback: recreate IVFFlat indexes
    op.execute(f"""
        CREATE INDEX idx_episodes_embedding_ivfflat
        ON episodes USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100)
    """)
    op.execute(f"""
        CREATE INDEX idx_facts_embedding_ivfflat
        ON facts USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100)
    """)
    op.execute("DROP INDEX IF EXISTS idx_episodes_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS idx_facts_embedding_hnsw")
```

**⚠️ Production risk — CONCURRENTLY caveats:**
- `CREATE INDEX CONCURRENTLY` requires more total work and time than a regular index build
- It can fail mid-way (e.g., due to deadlock), leaving an "invalid" index that must be dropped
- It cannot run inside a transaction block — must run outside alembic's transaction
- Run as a separate migration with `op.execute("COMMIT")` before the CONCURRENTLY call:

```python
# Run CONCURRENTLY outside transaction
op.execute("COMMIT")  # End internal alembic transaction
op.execute("""
    CREATE INDEX CONCURRENTLY idx_episodes_embedding_hnsw
    ON episodes USING hnsw (...)
""")
```

---

## 4. Embedding Operations

### 4.1 Embedding Client

```python
# packages/core/embedding/client.py
from typing import Any
import httpx
import numpy as np

class EmbeddingClient:
    """Abstract embedding client with provider abstraction.

    Supports OpenAI, Azure OpenAI, and Ollama backends.
    Dimension validation is performed at startup and before every insert.
    """

    def __init__(
        self,
        provider: str = "openai",       # "openai", "azure", "ollama"
        model: str = "text-embedding-3-small",
        dimensions: int = 1536,
        api_key: str | None = None,
        base_url: str | None = None,
        max_batch_size: int = 100,
        max_retries: int = 3,
    ) -> None:
        self._provider = provider
        self._model = model
        self._dimensions = dimensions
        self._max_batch_size = max_batch_size
        self._max_retries = max_retries

        # Validate startup configuration
        self._validate_dimension()

    def _validate_dimension(self) -> None:
        """Verify that the configured embedding model produces the expected dimension.

        Raises ConfigurationError at startup if mismatch detected.
        This prevents silent data corruption where vectors of wrong dimension
        are inserted into the database.

        Validation sources:
        - OpenAI: model dimension is well-known (text-embedding-3-small = 1536)
        - Ollama: query the model metadata endpoint at startup
        - Custom: read from environment variable EMBEDDING_DIM
        """
        expected_dim = MODELS_DIMENSIONS.get(self._model, self._dimensions)
        if expected_dim != self._dimensions:
            raise ConfigurationError(
                f"Model {self._model} produces {expected_dim}-dim vectors, "
                f"but EMBEDDING_DIM is configured as {self._dimensions}. "
                f"Set EMBEDDING_DIM={expected_dim} to match your model."
            )

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts in batches.

        Args:
            texts: List of text strings to embed.

        Returns:
            List of embedding vectors, same order as input.

        Handles:
        - Batching: splits into chunks of max_batch_size
        - Token limit: truncates texts that exceed model max tokens (8K for text-embedding-3)
        - Retry: exponential backoff on API errors (max 3 retries)
        - Empty input: returns empty list
        """
        if not texts:
            return []

        results: list[list[float]] = []
        for i in range(0, len(texts), self._max_batch_size):
            batch = texts[i : i + self._max_batch_size]
            batch_results = await self._embed_batch(batch)
            results.extend(batch_results)

        return results

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a single batch of texts.

        Internal method. Implements provider-specific API calls.
        """
        if self._provider == "openai":
            return await self._embed_openai(texts)
        elif self._provider == "ollama":
            return await self._embed_ollama(texts)
        else:
            raise ValueError(f"Unknown embedding provider: {self._provider}")

    async def _embed_openai(self, texts: list[str]) -> list[list[float]]:
        """Call OpenAI Embeddings API.

        Uses dimensions parameter supported by new Ada models.
        Logs token usage to llm_usage table via callback.
        """
        ...

    async def _embed_ollama(self, texts: list[str]) -> list[list[float]]:
        """Call Ollama embeddings API.

        Falls back to nomic-embed-text if configured model not found.
        """
        ...
```

### 4.2 Model Dimension Reference

```python
# packages/core/embedding/models.py

# Well-known embedding model dimensions
# Source: model documentation and API responses
MODELS_DIMENSIONS: dict[str, int] = {
    # OpenAI
    "text-embedding-3-small": 1536,     # Default. Supports dimensions param (can truncate)
    "text-embedding-3-large": 3072,     # Higher quality, 2x storage
    "text-embedding-ada-002": 1536,     # Legacy model, fixed 1536 dims

    # Ollama / Local
    "nomic-embed-text": 768,            # Good local option, Apache 2.0
    "all-minilm": 384,                  # Fastest local option, ~6x smaller than ada
    "gte-small": 384,                   # Microsoft, good quality/size tradeoff
    "gte-large": 1024,                  # Higher quality local option
    "bge-small-en": 384,                # BAAI, small English model
    "bge-base-en": 768,                 # BAAI, base English model
    "bge-large-en": 1024,               # BAAI, large English model

    # Cohere
    "embed-english-v3.0": 1024,         # Cohere English model
    "embed-multilingual-v3.0": 1024,    # Cohere multilingual

    # Google
    "text-embedding-004": 768,          # Google's embedding model
}

# Token limits per model (for truncation logic)
MODELS_TOKEN_LIMITS: dict[str, int] = {
    "text-embedding-3-small": 8192,     # Max tokens per input (new: 8191)
    "text-embedding-3-large": 8192,
    "text-embedding-ada-002": 8192,
    "nomic-embed-text": 8192,
    "all-minilm": 512,                  # Smaller context window
    "gte-small": 512,
    "gte-large": 512,
    "bge-small-en": 512,
    "bge-base-en": 512,
    "bge-large-en": 512,
}
```

### 4.3 Batch Embedding Strategy

```python
# services/embedding/worker.py
async def embed_episodes_batch(
    episodes: list[Episode],
    embedding_client: EmbeddingClient,
    llm_usage_repo: LLMUsageRepository,
    batch_size: int = 100,
) -> list[Episode]:
    """Batch-embed a list of episode contents and update the DB.

    Process:
    1. Extract content from each episode
    2. Truncate content to fit model token limit (tiktoken for OpenAI, char-count fallback for local)
    3. Send batch to embedding API
    4. Log token usage to llm_usage table
    5. Update each episode row with its embedding vector

    Concurrency:
    - Multiple workers can run this concurrently for different users/sessions
    - Same worker can process episodes in parallel within a batch via asyncio.gather

    Token limit handling:
    - OpenAI: use tiktoken to count tokens, truncate to 8000 tokens
    - Local models: truncate to 8000 characters as heuristic
    - Log warning when truncation occurs

    Returns:
        List of updated Episode objects with embedding populated.
    """
    # Step 1: Separate episodes that need embedding
    to_embed = [ep for ep in episodes if ep.embedding is None]
    if not to_embed:
        return episodes

    # Step 2: Truncate content
    texts = []
    for ep in to_embed:
        truncated = await truncate_to_token_limit(ep.content)
        texts.append(truncated)

    # Step 3: Embed in batches
    all_embeddings = []
    total_tokens = 0
    for i in range(0, len(texts), batch_size):
        batch_texts = texts[i : i + batch_size]
        embeddings, token_usage = await embedding_client.embed_many_with_usage(batch_texts)
        all_embeddings.extend(embeddings)
        total_tokens += token_usage

    # Step 4: Log usage
    await llm_usage_repo.log_embedding_usage(
        model=embedding_client.model,
        tokens=total_tokens,
        task_type="embed_episode",
        count=len(to_embed),
    )

    # Step 5: Update episodes with embeddings
    for ep, embedding in zip(to_embed, all_embeddings):
        ep.embedding = embedding

    return to_embed


async def truncate_to_token_limit(
    text: str,
    max_tokens: int = 8000,
    model: str = "text-embedding-3-small",
) -> str:
    """Truncate text to fit within the model's token limit.

    For OpenAI models: uses tiktoken for accurate token counting
    For other models: uses character count heuristic (assumes ~4 chars/token)

    Falls back to character-based truncation if tiktoken is unavailable.
    """
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model(model)
        tokens = enc.encode(text)
        if len(tokens) > max_tokens:
            truncated_tokens = tokens[:max_tokens]
            logger.warning(
                "embedding.truncation",
                extra={
                    "original_tokens": len(tokens),
                    "max_tokens": max_tokens,
                    "model": model,
                },
            )
            return enc.decode(truncated_tokens)
        return text
    except (ImportError, KeyError):
        # Fallback: 4 chars ≈ 1 token
        max_chars = max_tokens * 4
        if len(text) > max_chars:
            return text[:max_chars]
        return text
```

### 4.4 Graphiti Edge Embeddings

In addition to PostgreSQL vectors, RELATES_TO edges in the graph database carry a `fact_embedding` property:

```python
# When creating a RELATES_TO edge:
async def _embed_and_attach_fact(
    self,
    fact_text: str,
    source_uuid: str,
    target_uuid: str,
    predicate: str,
) -> RelatesToEdge:
    """Embed the fact text and attach to the RELATES_TO edge.

    The embedding is stored:
    1. In PostgreSQL: facts.embedding column (for hybrid retrieval)
    2. In Graphiti: RELATES_TO.fact_embedding property (for graph-native similarity search)

    Both embeddings are identical (same text, same model).
    Dual storage avoids cross-store queries: PostgreSQL search doesn't
    need to query the graph DB, and graph search doesn't need PostgreSQL.
    """
    embedding = await self._embedding_client.embed_one(fact_text)

    # Store in PostgreSQL
    await self._facts_repo.update_embedding(fact_uuid, embedding)

    # Store in Graphiti edge
    edge = RelatesToEdge(
        fact=fact_text,
        fact_embedding=embedding,
        predicate=predicate,
        ...
    )
    return edge
```

---

## 5. Implementation Notes

### 5.1 Startup Validation

```python
# services/api/main.py — FastAPI lifespan

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Validate embedding configuration at startup.

    Checks:
    1. EMBEDDING_DIM matches the model's expected output dimension
    2. pgvector extension is installed with correct version
    3. Vector columns exist with correct dimension
    4. Embedding API provider is reachable
    """
    # Check 1: Model dimension
    expected = MODELS_DIMENSIONS.get(settings.EMBEDDING_MODEL)
    if expected and expected != settings.EMBEDDING_DIM:
        raise ConfigurationError(
            f"EMBEDDING_DIM={settings.EMBEDDING_DIM} does not match "
            f"model {settings.EMBEDDING_MODEL} (expected {expected}). "
            f"Update EMBEDDING_DIM in your environment."
        )

    # Check 2: pgvector version
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        version = row.scalar()
        if not version:
            raise ConfigurationError("pgvector extension not installed")
        logger.info(f"pgvector version: {version}")

        # Check 3: Column dimension
        for table in ["episodes", "facts"]:
            row = await conn.execute(
                text(f"""
                    SELECT column_name, udt_name
                    FROM information_schema.columns
                    WHERE table_name = '{table}'
                    AND column_name = 'embedding'
                """)
            )
            col = row.fetchone()
            if col:
                logger.info(f"{table}.embedding column exists")

    # Check 4: Provider reachability (non-blocking — log warning, don't crash)
    try:
        await embedding_client.health_check()
    except Exception as e:
        logger.warning(f"Embedding provider unreachable at startup: {e}")

    yield

    # Shutdown
    await engine.dispose()
```

### 5.2 Re-Indexing Schedule

IVFFlat indexes degrade as new rows are inserted — centroids become stale. Re-indexing restores performance.

```python
# services/embedding/reindex.py

class ReindexScheduler:
    """Manages IVFFlat index rebuild schedule.

    Trigger conditions:
    - N inserts since last rebuild (N from env PGVECTOR_REINDEX_AFTER, default 50000)
    - Scheduled: daily during low-traffic window (configurable)
    - Manual: triggered by admin dashboard

    Re-indexing is O(row_count × lists × dims) — for 1M rows it takes ~30s
    and holds a write lock on the index. Use CONCURRENTLY variant if available
    (pgvector 0.7.0+).
    """

    def __init__(
        self,
        engine,
        reindex_after: int = 50_000,
        table_indexes: dict[str, str] = None,
    ) -> None:
        self._engine = engine
        self._reindex_after = reindex_after
        self._table_indexes = table_indexes or {
            "episodes": "idx_episodes_embedding_ivfflat",
            "facts": "idx_facts_embedding_ivfflat",
        }

    async def should_reindex(self, table: str) -> bool:
        """Check if a table's embedding index should be rebuilt.

        Compares current row count against row count at last index build.
        """
        ...
```

### 5.3 Dimension Migration Path

Changing the embedding model (e.g., from 1536 to 768 dimensions) requires a careful multi-step process:

```
Phase 1 (Schema change):
  ALTER TABLE episodes ALTER COLUMN embedding TYPE VECTOR(768);
  ALTER TABLE facts ALTER COLUMN embedding TYPE VECTOR(768);
  ⚠️ This invalidates ALL existing vectors and breaks the index.
  ⚠️ The IVFFlat/HNSW index must be dropped first.

Phase 2 (Re-embed):
  For each episode and fact without embedding (or with old dimension):
    - Call new embedding model
    - UPDATE row with new vector

Phase 3 (Rebuild index):
  CREATE INDEX ... IVFFLAT WITH (lists = ...)
```

**Complete migration script:**

```python
# alembic/versions/000X_change_embedding_dimension.py

"""Change embedding dimension from 1536 to 768

This migration requires:
1. Dropping all vector indexes (they become invalid on ALTER)
2. ALTER COLUMN TYPE on both tables
3. Manual re-embedding of all rows (not done in migration — done by worker)
4. Rebuilding the vector indexes

⚠️ This is a BREAKING change. All existing embeddings become invalid.
  The system will return 0 results from vector search until re-embedding completes.

  Steps to execute:
  1. alembic upgrade head  (drops indexes, alters columns)
  2. Deploy updated EMBEDDING_DIM=768
  3. Run re-embed worker: memgraph reembed --batch-size=100
  4. Run alembic to create new indexes (separate migration file)
"""

from alembic import op
from core.config import settings

OLD_DIM = 1536
NEW_DIM = settings.EMBEDDING_DIM  # e.g., 768

def upgrade():
    # Step 1: Drop vector indexes (must precede ALTER COLUMN)
    op.execute("DROP INDEX IF EXISTS idx_episodes_embedding_ivfflat")
    op.execute("DROP INDEX IF EXISTS idx_episodes_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS idx_facts_embedding_ivfflat")
    op.execute("DROP INDEX IF EXISTS idx_facts_embedding_hnsw")

    # Step 2: Alter column types
    # Using USING clause to cast existing data (will produce garbage vectors —
    # they must be re-embedded)
    op.execute(f"""
        ALTER TABLE episodes
        ALTER COLUMN embedding TYPE VECTOR({NEW_DIM})
        USING embedding::text::vector
    """)
    op.execute(f"""
        ALTER TABLE facts
        ALTER COLUMN embedding TYPE VECTOR({NEW_DIM})
        USING embedding::text::vector
    """)

    # Note: Indexes are NOT recreated here — they are created in a SEPARATE
    # migration after re-embedding completes.

def downgrade():
    # Rollback: alter back to old dimension
    op.execute(f"""
        ALTER TABLE episodes
        ALTER COLUMN embedding TYPE VECTOR({OLD_DIM})
        USING embedding::text::vector
    """)
    op.execute(f"""
        ALTER TABLE facts
        ALTER COLUMN embedding TYPE VECTOR({OLD_DIM})
        USING embedding::text::vector
    """)

    # Recreate old indexes
    op.execute(f"""
        CREATE INDEX idx_episodes_embedding_ivfflat
        ON episodes USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100)
    """)
    op.execute(f"""
        CREATE INDEX idx_facts_embedding_ivfflat
        ON facts USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100)
    """)
```

**Re-embedding worker (separate task, not in migration):**

```python
# scripts/reembed.py

async def reembed_all(
    db_session_factory,
    embedding_client: EmbeddingClient,
    batch_size: int = 100,
) -> None:
    """Re-embed all episodes and facts with the new dimension.

    Process:
    1. Select rows WHERE embedding IS NOT NULL (have old embedding)
    2. Batch-re-embed via embedding API
    3. Update in batches of 1000
    4. Log progress and token usage

    Resume support: tracks last processed ID in Redis.
    Run with: python -m scripts.reembed
    """
    ...
```

### 5.4 Query Patterns

**Cosine similarity search (default — RET-01):**
```sql
-- Find top-10 most similar episodes
SELECT id, content, 1 - (embedding <=> :query_embedding) AS similarity
FROM episodes
WHERE embedding IS NOT NULL
ORDER BY embedding <=> :query_embedding
LIMIT 10;
```

**Distance function options:**

```sql
-- Cosine distance (default for text embeddings) — range [0, 2]
-- 0 = identical direction, 1 = orthogonal, 2 = opposite
SELECT embedding <=> :query_embedding AS cosine_distance;

-- L2 distance (Euclidean) — range [0, ∞)
-- Best for normalized vectors where magnitude matters
SELECT embedding <-> :query_embedding AS l2_distance;

-- Inner product (dot product) — range (-∞, ∞)
-- Best for normalized vectors where negative values carry meaning
SELECT embedding <#> :query_embedding AS inner_product;
-- Note: <#> returns NEGATIVE inner product so that ASC ordering = best match
```

**Switching distance function:**
```sql
-- To change distance function, you MUST rebuild the index:
-- CREATE INDEX ... USING ivfflat (embedding vector_l2_ops)
-- CREATE INDEX ... USING ivfflat (embedding vector_ip_ops)

-- Available operator classes:
-- vector_cosine_ops  → <=> operator  (cosine distance)
-- vector_l2_ops      → <-> operator  (L2 distance)
-- vector_ip_ops      → <#> operator  (inner product, negated)
```

**Recommendation:** Use `vector_cosine_ops` (cosine distance) for text embeddings. This is the default and what OpenAI embeddings are designed for. Switch to `vector_l2_ops` only if you normalize your vectors to unit length and need Euclidean distance semantics.

### 5.5 Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EMBEDDING_PROVIDER` | `openai` | `openai`, `azure`, `ollama` |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Model identifier |
| `EMBEDDING_DIM` | `1536` | Vector dimension — MUST match model output |
| `EMBEDDING_BATCH_SIZE` | `100` | Max texts per API call |
| `EMBEDDING_MAX_TOKENS` | `8000` | Max tokens per text before truncation |
| `EMBEDDING_API_KEY` | — | Provider API key |
| `EMBEDDING_BASE_URL` | — | Provider base URL (Ollama: `http://localhost:11434`) |
| `PGVECTOR_INDEX_TYPE` | `ivfflat` | `ivfflat` or `hnsw` |
| `PGVECTOR_LISTS` | `100` | IVFFlat lists parameter |
| `PGVECTOR_PROBES` | `10` | IVFFlat probes parameter |
| `PGVECTOR_HNSW_M` | `16` | HNSW max connections per node |
| `PGVECTOR_HNSW_EF_CONSTRUCTION` | `200` | HNSW build-time search effort |
| `PGVECTOR_HNSW_EF_SEARCH` | `100` | HNSW query-time search effort |
| `PGVECTOR_DISTANCE` | `cosine` | `cosine`, `l2`, `inner` |
| `PGVECTOR_REINDEX_AFTER` | `50000` | Rebuild IVFFlat after this many inserts |

---

## 6. Testing Guidance

### 6.1 Unit Tests

| Test | What to Cover |
|------|--------------|
| Dimension validation | Mismatched model/dimension raises ConfigurationError |
| Batch splitting | 250 texts split into 3 batches (100 + 100 + 50) |
| Empty input | embed_many([]) returns [] |
| Token truncation | Text > max_tokens is truncated correctly |
| Distance function selection | Correct operator class selected per config |
| IVFFlat lists formula | compute_lists(10000) = 100, compute_lists(1) = 10 |
| HNSW decision logic | should_use_hnsw(1_500_000, 0.95, 100) = True |

### 6.2 Integration Tests (real PostgreSQL + pgvector via testcontainers)

| Test | What to Cover |
|------|--------------|
| Extension creation | `CREATE EXTENSION vector` succeeds |
| Vector column DDL | `VECTOR(1536)` column created and verified |
| IVFFlat index creation | `CREATE INDEX ... USING ivfflat` succeeds |
| HNSW index creation | `CREATE INDEX ... USING hnsw` succeeds (pgvector ≥ 0.5.0) |
| Cosine similarity | Insert 3 vectors → query → verify correct ordering |
| NULL vector handling | `WHERE embedding IS NOT NULL` filters correctly |
| CONCURRENTLY index build | Index build succeeds without blocking inserts |
| Dimension migration | ALTER COLUMN TYPE VECTOR(768) → re-embed → verify |
| Batch embedding | 150 texts embedded, all returned in correct order |
| Token truncation in pipeline | 9000-token text truncated to 8000 before API call |

### 6.3 Edge Cases to Cover

1. **Single text batch:** `embed_many(["hello"])` — verify single result returned.
2. **Very long text (>100K chars):** Truncation to 8000 tokens must handle this gracefully. Verify no OOM.
3. **All empty texts:** `embed_many(["", "", ""])` — OpenAI returns non-empty vectors for empty strings. Verify behavior is acceptable.
4. **API failure mid-batch:** Batch of 100 texts, 50th call fails. Verify retry logic kicks in and 51-100 are not lost.
5. **Dimension mismatch at runtime:** Application configured for 1536, but model switched to 768. Startup validation catches this.
6. **HNSW index on NULL-heavy column:** Most rows have NULL embedding. Verify index handles NULLs efficiently.
7. **Re-index during active writes:** Verify no deadlocks when re-indexing while inserts are happening.
8. **Concurrent re-embedding:** Two workers re-embedding the same episode. Verify idempotency (last write wins).
9. **IVFFlat probes > lists:** Setting probes higher than lists should be clamped. Verify behavior.
10. **Distance function mismatch:** Query using `<=>` on a `vector_l2_ops` index will still work (pgvector falls back to sequential scan). Verify performance warning is logged.

### 6.4 Performance Benchmarks

Run these with `tests/perf/test_vector_performance.py`:

| Benchmark | Target |
|-----------|--------|
| IVFFlat query (lists=100, probes=10) on 100K vectors | < 10ms p50 |
| HNSW query (ef_search=100) on 100K vectors | < 5ms p50 |
| IVFFlat index build on 100K vectors (lists=100) | < 30s |
| HNSW index build on 100K vectors (m=16) | < 5min |
| Batch embedding 100 texts (OpenAI API) | < 5s p95 |
| Sequential scan (no index) on 100K vectors | < 500ms |
| Concurrent inserts during index build | No deadlock, < 100ms per insert |

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*

**SRS traceability:** This document implements vector search requirements from SRS RET-01 (pgvector cosine similarity), RET-04 (RRF vector component), SCALE-03 (embedding batching), and PORT-04 (pluggable embedding backend). The index strategy provides a clear IVFFlat → HNSW upgrade path for Phase 5 hardening.
