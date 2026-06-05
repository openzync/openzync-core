# PostgreSQL Schema — Complete DDL & Migration Strategy

## 1. Overview

| Field | Detail |
|-------|--------|
| **Coverage** | All PostgreSQL tables for OpenZep — relational data, vector storage, full-text search |
| **Dependencies** | PostgreSQL 15+ with `pgvector` extension; `pg_trgm` extension for BM25 |
| **SRS Requirement IDs** | MT-01, MT-02, ING-01–ING-06, KG-05–KG-12, NLP-05–NLP-14, USR-01–USR-05, SES-01–SES-05, AUTH-01–AUTH-06, SEC-01, SEC-03, SEC-04, SEC-08, SEC-09, PERF-01–PERF-06, SCALE-02, MAINT-04 |
| **Build Phase** | Phase 0 (Foundation) |
| **Design Authority** | @architect for schema decisions, @devops for migration runbook |

### 1.1 Table Inventory (11 tables)

| # | Table | SRS Origin | Purpose |
|---|-------|-----------|---------|
| 1 | `organizations` | §7.1 | Multi-tenant orgs with plan/quotas |
| 2 | `api_keys` | §7.1, AUTH-01–AUTH-06 | API key hashes, prefixes, scopes |
| 3 | `users` | §7.1, USR-01–USR-05 | Users scoped to organizations |
| 4 | `sessions` | §7.1, SES-01–SES-05 | Conversation sessions per user |
| 5 | `episodes` | §7.1, ING-01–ING-06 | Raw conversation messages |
| 6 | `facts` | §7.1, NLP-05–NLP-07 | Extracted fact triples |
| 7 | `structured_extractions` | §7.1, NLP-12–NLP-14 | JSON Schema-based extractions |
| 8 | `dialog_classifications` | §7.1, NLP-08–NLP-11 | Intent + emotion per episode |
| 9 | `extraction_schemas` | §7.1 audit gap, NLP-12 | Org-defined JSON Schemas |
| 10 | `refresh_tokens` | §7.1 audit gap, AUTH-05, SEC-08 | Dashboard JWT refresh tokens |
| 11 | `audit_log` | §7.1 audit gap | Immutable audit trail |
| 12 | `llm_usage` | §7.1 audit gap | LLM/embedding token accounting |

---

## 2. Data Model — Complete DDL

### 2.1 Extension Setup

```sql
-- Run as superuser before any migrations
CREATE EXTENSION IF NOT EXISTS pgcrypto;        -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS vector;           -- pgvector
CREATE EXTENSION IF NOT EXISTS pg_trgm;          -- trigram for BM25 fallback
```

### 2.2 TimestampMixin Pattern

All tables use the same timestamp pattern for consistency. The Alembic `Base` class should implement this as a mixin:

```python
# packages/core/db/mixins.py
import sqlalchemy as sa
from sqlalchemy.orm import declared_attr, Mapped, mapped_column
from datetime import datetime

class TimestampMixin:
    """Standard created_at/updated_at columns for all entities.

    - created_at: server-default, never updated after insert
    - updated_at: server-default, auto-updated on row modification
    """
    @declared_attr
    def created_at(cls) -> Mapped[datetime]:
        return mapped_column(
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Row creation timestamp (server UTC)",
        )

    @declared_attr
    def updated_at(cls) -> Mapped[datetime]:
        return mapped_column(
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
            comment="Last row modification timestamp (server UTC)",
        )
```

**DDL for the mixin columns (identical across all tables that use it):**
```sql
created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
```

**Rationale:**
- `TIMESTAMPTZ` (TIMESTAMP WITH TIME ZONE) avoids timezone ambiguity — always stored as UTC, converted to client locale at read time.
- `server_default` uses the database clock, not the application clock, ensuring consistency across horizontal replicas.
- `onupdate` fires on every row-level `UPDATE` — critical for cache invalidation and audit.
- Every table MUST have these columns. No exceptions — do not skip on "simple" lookup tables.

---

### 2.3 `organizations`

```sql
CREATE TABLE organizations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    plan        TEXT NOT NULL DEFAULT 'free'
                CHECK (plan IN ('free', 'pro', 'enterprise')),
    quotas      JSONB DEFAULT '{}'::jsonb,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE organizations IS 'Multi-tenant organizations — every data row is scoped by organization_id';
COMMENT ON COLUMN organizations.plan IS 'Subscription tier: free, pro, enterprise. Controls feature access and rate limits.';
COMMENT ON COLUMN organizations.quotas IS 'JSONB map of quota_name: limit_value, e.g. {"max_users": 100, "max_api_calls_per_day": 50000}';
COMMENT ON COLUMN organizations.is_active IS 'Soft-disable an org without deleting data. All API calls for deactivated orgs return 403.';
```

**Indexes:**

```sql
-- No additional indexes needed: PK covers id lookups.
-- plan has very low cardinality (3 values) — an index would never be used.
```

**Rationale:**
- `plan` CHECK constraint ensures only valid tiers. Changing tiers requires a migration to alter the CHECK.
- `quotas` is a flexible JSONB blob — org-specific limits (max_users, max_graph_nodes, max_api_calls/min, max_embedding_tokens/month). The service layer reads it and enforces limits at runtime.
- `is_active` provides a circuit-breaker for billing failures or terms violations. All query layers MUST filter `WHERE o.is_active = TRUE`.

---

### 2.4 `api_keys`

```sql
CREATE TABLE api_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    key_hash        TEXT NOT NULL,
    prefix          TEXT NOT NULL
                    CHECK (prefix IN ('mg_live_', 'mg_test_')),
    name            TEXT,
    scopes          TEXT[] NOT NULL DEFAULT ARRAY['read', 'write'],
    last_used_at    TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,
    is_revoked      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_api_keys_key_hash UNIQUE (key_hash)
);

COMMENT ON TABLE api_keys IS 'API key hashes for programmatic access. Raw key shown only once at creation.';
COMMENT ON COLUMN api_keys.key_hash IS 'bcrypt hash of the raw API key. The raw key is never persisted — shown once at creation and discarded.';
COMMENT ON COLUMN api_keys.prefix IS 'Key prefix: mg_live_ (production) or mg_test_ (sandbox). Used for routing and UX, not security.';
COMMENT ON COLUMN api_keys.scopes IS 'Postgres TEXT array of scopes, e.g. {read, write, admin}. Future: per-endpoint scope enforcement.';
COMMENT ON COLUMN api_keys.is_revoked IS 'Soft revocation — retains key_hash to prevent re-issuance of the same raw key.';
COMMENT ON COLUMN api_keys.expires_at IS 'Optional key expiration. NULL = never expires. Checked on every auth request.';
```

**Indexes:**

```sql
-- FK index: every foreign key column MUST be indexed (see §4.2 Audit)
CREATE INDEX idx_api_keys_organization_id ON api_keys(organization_id);

-- Lookup by key_hash during authentication (critical path — every API call)
-- key_hash has a UNIQUE constraint, so PostgreSQL auto-creates a unique index.
```

**CREATE OR REPLACE FUNCTION to enforce key uniqueness (defensive):**
```sql
-- Ensure the same raw key cannot be hashed differently and inserted twice.
-- This is enforced by the UNIQUE constraint on key_hash, but we add a comment
-- in case someone later changes the hash algorithm.
-- No additional index needed — UNIQUE constraint covers lookup.
```

**Rationale:**
- `key_hash` uses bcrypt (cost factor 10–12) — the raw key is shown to the caller once at creation, then discarded. See `02-auth-tenancy/01-api-key-auth.md` for key generation flow.
- `prefix` CHECK enforces the convention from AUTH-03 at the database level.
- `scopes` uses PostgreSQL TEXT array — scoped checks are done in the service layer. Future: migrate to a normalized `api_key_scopes` join table if scope cardinality grows.
- `is_revoked` enables soft revocation without deleting the row (preserves key_hash for dedup).

---

### 2.5 `users`

```sql
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    external_id     TEXT NOT NULL,
    name            TEXT,
    email           TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_users_org_external UNIQUE (organization_id, external_id)
);

COMMENT ON TABLE users IS 'End-users whose memory is tracked. Scoped to an organization.';
COMMENT ON COLUMN users.external_id IS 'Caller-defined user ID (e.g., from their application). Unique within an org.';
COMMENT ON COLUMN users.metadata IS 'Arbitrary caller JSON — tags, custom fields, preferences. Queryable via JSONB operators.';
COMMENT ON COLUMN users.is_active IS 'Soft-delete. When FALSE, the user is invisible to queries and cascade deletes are NOT triggered.';
```

**Indexes:**

```sql
-- FK index (critical for multi-tenant queries filtering by organization_id)
CREATE INDEX idx_users_organization_id ON users(organization_id);

-- Lookup by external_id within an org (USR-01, USR-02)
-- The UNIQUE(organization_id, external_id) constraint auto-creates a composite index.
-- Covered queries: WHERE organization_id = $1 AND external_id = $2

-- Search by email (USR-05)
CREATE INDEX idx_users_email ON users(email)
    WHERE email IS NOT NULL;

-- Partial index for active users (most queries filter on is_active = TRUE)
CREATE INDEX idx_users_org_active ON users(organization_id, is_active)
    WHERE is_active = TRUE;
```

**Rationale:**
- `external_id` is the caller's user identifier, not an auto-generated UUID. The `UNIQUE (organization_id, external_id)` constraint enforces caller-side uniqueness within a tenant.
- `metadata` is JSONB with a default empty object — never NULL. This avoids NULL checks in the service layer.
- The partial index `idx_users_org_active` covers the most common query pattern: "list active users for this org".

---

### 2.6 `sessions`

```sql
CREATE TABLE sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    external_id     TEXT NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ,

    CONSTRAINT uq_sessions_user_external UNIQUE (user_id, external_id)
);

COMMENT ON TABLE sessions IS 'Conversation sessions grouping episodes.';
COMMENT ON COLUMN sessions.external_id IS 'Caller-defined session ID (e.g., conversation_abc). Unique within a user.';
COMMENT ON COLUMN sessions.is_active IS 'TRUE while session is open. Set to FALSE on close or timeout.';
COMMENT ON COLUMN sessions.closed_at IS 'Timestamp when session was closed. NULL while open. Workers check this to trigger end-of-session tasks.';
```

**Indexes:**

```sql
-- FK index
CREATE INDEX idx_sessions_user_id ON sessions(user_id);

-- List sessions for a user ordered by creation (SES-02)
CREATE INDEX idx_sessions_user_created ON sessions(user_id, created_at DESC);

-- Find active sessions for a user
CREATE INDEX idx_sessions_user_active ON sessions(user_id, is_active)
    WHERE is_active = TRUE;
```

**Rationale:**
- Sessions are soft-closed (`is_active = FALSE`, `closed_at` set) rather than deleted. This enables end-of-session processing (structured extraction, community contribution) in async workers.
- `external_id` is caller-defined — the UNIQUE constraint prevents duplicate session IDs within a user's namespace.

---

### 2.7 `episodes`

```sql
CREATE TABLE episodes (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role              TEXT NOT NULL
                      CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content           TEXT NOT NULL
                      CHECK (char_length(content) <= 65536),
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    token_count       INT NOT NULL DEFAULT 0,
    sequence_number   INT NOT NULL,
    embedding         VECTOR(1536),       -- dimension parameterized from EMBEDDING_DIM
    graphiti_node_id  TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_episodes_session_sequence UNIQUE (session_id, sequence_number)
);

COMMENT ON TABLE episodes IS 'Individual conversation turns (messages). Core of episodic memory.';
COMMENT ON COLUMN episodes.role IS 'Message role: user, assistant, system, or tool. Matches OpenAI message format.';
COMMENT ON COLUMN episodes.content IS 'Message body. Max 64KB enforced by CHECK constraint (SEC-09).';
COMMENT ON COLUMN episodes.token_count IS 'Estimated token count of content. Set at ingestion or by worker. Used for context window budgeting.';
COMMENT ON COLUMN episodes.sequence_number IS 'Monotonically increasing sequence number within a session. Guarantees ordering.';
COMMENT ON COLUMN episodes.embedding IS 'pgvector embedding of content. NULL until embedding worker completes.';
COMMENT ON COLUMN episodes.graphiti_node_id IS 'UUID of the corresponding EpisodicNode in the graph DB (FalkorDB/Neo4j). NULL until enqueued.';
```

**Indexes:**

```sql
-- FK indexes (3 FKs = 3 indexes)
CREATE INDEX idx_episodes_session_id ON episodes(session_id);
CREATE INDEX idx_episodes_user_id ON episodes(user_id);

-- Retrieve messages for a session in order (SES-04)
CREATE INDEX idx_episodes_session_sequence ON episodes(session_id, sequence_number);

-- Filter episodes for a user within a time range (context assembly)
CREATE INDEX idx_episodes_user_created ON episodes(user_id, created_at DESC);

-- pgvector IVFFlat index for vector similarity search (RET-01)
-- Index name includes dimension for clarity
CREATE INDEX idx_episodes_embedding_ivfflat ON episodes
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- GIN full-text search index for BM25 (RET-02)
CREATE INDEX idx_episodes_content_gin ON episodes
    USING GIN (to_tsvector('english', content));

-- Cover the "recent episodes" query which filters on created_at > now() - interval
CREATE INDEX idx_episodes_user_created_recent ON episodes(user_id, created_at DESC)
    WHERE created_at > NOW() - INTERVAL '24 hours';
```

**Rationale:**
- `content` CHECK enforces the 64KB limit from SEC-09 at the database level — defense in depth beyond API validation.
- `token_count` enables context window budgeting without re-tokenizing. Default 0 — set by worker after tokenization.
- `sequence_number` provides deterministic ordering even if two messages arrive in the same millisecond. The UNIQUE constraint prevents duplicates.
- The partial index `idx_episodes_user_created_recent` optimizes the common "recent activity" query in context assembly.
- The GIN index uses `to_tsvector('english', content)` — the language config should be parameterized if multi-language support is needed.

---

### 2.8 `facts`

```sql
CREATE TABLE facts (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content           TEXT NOT NULL,
    subject           TEXT,
    predicate         TEXT,
    object            TEXT,
    subject_type      TEXT,
    object_type       TEXT,
    confidence        REAL NOT NULL DEFAULT 1.0
                      CHECK (confidence >= 0 AND confidence <= 1),
    source_episode_id UUID REFERENCES episodes(id) ON DELETE SET NULL,
    valid_from        TIMESTAMPTZ,
    valid_to          TIMESTAMPTZ,
    invalid_at        TIMESTAMPTZ,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    embedding         VECTOR(1536),       -- dimension parameterized from EMBEDDING_DIM
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_facts_valid_range CHECK (
        valid_from IS NULL OR
        valid_to IS NULL OR
        valid_from < valid_to
    )
);

COMMENT ON TABLE facts IS 'Extracted fact triples with bi-temporal validity. Core of semantic/temporal memory.';
COMMENT ON COLUMN facts.subject IS 'Entity subject of the triple (e.g., "user_123").';
COMMENT ON COLUMN facts.predicate IS 'Relationship predicate (e.g., "prefers", "purchased").';
COMMENT ON COLUMN facts.object IS 'Entity object of the triple (e.g., "Python").';
COMMENT ON COLUMN facts.subject_type IS 'Entity type of the subject (e.g., "person", "organization"). Added for graph resolution.';
COMMENT ON COLUMN facts.object_type IS 'Entity type of the object (e.g., "language", "product"). Added for graph resolution.';
COMMENT ON COLUMN facts.confidence IS 'LLM-assigned confidence score [0, 1]. Only facts above a threshold are surfaced in context.';
COMMENT ON COLUMN facts.valid_from IS 'Start of fact validity window (valid time). NULL = valid from recorded time.';
COMMENT ON COLUMN facts.valid_to IS 'End of fact validity window (valid time). NULL = still valid.';
COMMENT ON COLUMN facts.invalid_at IS 'System timestamp when fact was invalidated (transaction time). NULL = not invalidated.';
COMMENT ON COLUMN facts.is_active IS 'Query-level flag: TRUE for facts that should appear in results. Set to FALSE on contradiction or deletion.';
```

**Indexes:**

```sql
-- FK indexes
CREATE INDEX idx_facts_user_id ON facts(user_id);
CREATE INDEX idx_facts_source_episode_id ON facts(source_episode_id);

-- Core query: "get all active facts for this user" (NLP-06)
CREATE INDEX idx_facts_user_active ON facts(user_id, is_active)
    WHERE is_active = TRUE;

-- Temporal query: "get facts valid at time T for user" (KG-07, temporal layer)
CREATE INDEX idx_facts_user_temporal ON facts(user_id, valid_from, valid_to)
    WHERE is_active = TRUE;

-- Filter by predicate for graph queries (KG-11)
CREATE INDEX idx_facts_predicate ON facts(predicate)
    WHERE predicate IS NOT NULL AND is_active = TRUE;

-- pgvector IVFFlat index for semantic fact search (RET-01)
CREATE INDEX idx_facts_embedding_ivfflat ON facts
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- GIN full-text search index for BM25 fact search (RET-02) ⚠️ CRITICAL — was missing in SRS
CREATE INDEX idx_facts_content_gin ON facts
    USING GIN (to_tsvector('english', content));

-- Cover confidence-thresholded queries (NLP-06 filter)
CREATE INDEX idx_facts_user_confidence ON facts(user_id, confidence DESC)
    WHERE is_active = TRUE;
```

**Rationale:**
- `confidence` has a CHECK constraint enforcing [0, 1] range. The service layer uses a configurable threshold (default 0.7) to filter facts for context assembly.
- `subject_type` and `object_type` were missing from the SRS — added here for graph entity type resolution. These enable queries like "find all facts where the subject is a Person".
- The GIN index on `facts.content` was missing in the SRS — this is critical for RET-02 (full-text search). Without it, BM25 searching facts would be a sequential scan.
- The composite temporal index `idx_facts_user_temporal` is the workhorse for the temporal query layer (KG-07). It covers `WHERE user_id = $1 AND valid_from <= $2 AND (valid_to IS NULL OR valid_to >= $2)` in a single index scan.
- `valid_from < valid_to` CHECK constraint prevents nonsensical validity windows at the database level.

---

### 2.9 `structured_extractions`

```sql
CREATE TABLE structured_extractions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    schema_id   UUID REFERENCES extraction_schemas(id) ON DELETE SET NULL,
    data        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE structured_extractions IS 'JSON Schema-validated extractions from conversation sessions. Populated by NLP extract worker.';
COMMENT ON COLUMN structured_extractions.schema_id IS 'FK to extraction_schemas. NULL if the schema was deleted — data is preserved.';
COMMENT ON COLUMN structured_extractions.data IS 'Extracted JSON data conforming to the referenced schema. Validated by worker before insert.';
```

**Indexes:**

```sql
-- FK indexes
CREATE INDEX idx_structured_extractions_session_id ON structured_extractions(session_id);
CREATE INDEX idx_structured_extractions_schema_id ON structured_extractions(schema_id);

-- Query extractions by session (NLP-14)
CREATE INDEX idx_structured_extractions_session_created ON structured_extractions(session_id, created_at DESC);
```

**Rationale:**
- `schema_id` uses ON DELETE SET NULL — if an org deletes a schema, historical extractions remain accessible.
- `data` is raw JSONB with no schema-level validation at the DB layer. Validation happens in the worker against the stored schema definition.

---

### 2.10 `dialog_classifications`

```sql
CREATE TABLE dialog_classifications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    episode_id  UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    intent      TEXT,
    emotion     TEXT,
    valence     TEXT
                CHECK (valence IN ('positive', 'neutral', 'negative')),
    arousal     TEXT
                CHECK (arousal IN ('low', 'high')),
    confidence  REAL DEFAULT 1.0
                CHECK (confidence >= 0 AND confidence <= 1),
    raw         JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE dialog_classifications IS 'Per-episode intent and emotion classification. Populated by NLP classify worker.';
COMMENT ON COLUMN dialog_classifications.intent IS 'Classified intent (e.g., question, complaint, purchase_intent, chitchat). Org-configurable labels.';
COMMENT ON COLUMN dialog_classifications.emotion IS 'Classified emotion label (org-configurable).';
COMMENT ON COLUMN dialog_classifications.valence IS 'Emotional valence: positive, neutral, or negative.';
COMMENT ON COLUMN dialog_classifications.arousal IS 'Emotional arousal: low or high.';
COMMENT ON COLUMN dialog_classifications.raw IS 'Raw LLM response JSON for debugging and schema evolution.';
```

**Indexes:**

```sql
-- FK index
CREATE INDEX idx_dialog_classifications_episode_id ON dialog_classifications(episode_id);

-- Query classifications for a session via episode join (NLP-11)
CREATE INDEX idx_dialog_classifications_episode_created ON dialog_classifications(episode_id, created_at DESC);

-- Filter by intent for analytics
CREATE INDEX idx_dialog_classifications_intent ON dialog_classifications(intent)
    WHERE intent IS NOT NULL;
```

**Rationale:**
- `raw` stores the complete LLM response as JSONB — enables schema evolution without data migration. If the classification schema expands, old `raw` data can be re-parsed.

---

### 2.11 `extraction_schemas` (ADDED — missing from SRS §7.1)

This table was identified as missing during the schema audit. It supports requirement NLP-12 (org-defined JSON Schema for structured extraction).

```sql
CREATE TABLE extraction_schemas (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT,
    schema_def      JSONB NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_extraction_schemas_org_name UNIQUE (organization_id, name)
);

COMMENT ON TABLE extraction_schemas IS 'Org-defined JSON Schemas for structured data extraction from conversations (NLP-12).';
COMMENT ON COLUMN extraction_schemas.schema_def IS 'Valid JSON Schema (draft-07+) document. Used by LLM extraction prompt to structure output.';
COMMENT ON COLUMN extraction_schemas.is_active IS 'Soft-delete. Inactive schemas cannot be assigned to new sessions but existing extractions remain.';
```

**Indexes:**

```sql
CREATE INDEX idx_extraction_schemas_organization_id ON extraction_schemas(organization_id);
CREATE INDEX idx_extraction_schemas_org_active ON extraction_schemas(organization_id, is_active)
    WHERE is_active = TRUE;
```

**Rationale:**
- `schema_def` stores a complete JSON Schema document (e.g., `{"type": "object", "properties": {...}}`). The worker injects this schema into the LLM extraction prompt.
- The UNIQUE constraint prevents two schemas with the same name within an org.

---

### 2.12 `refresh_tokens` (ADDED — missing from SRS §7.1)

Supports requirement AUTH-05 (JWT auth for dashboard) and SEC-08 (refresh token rotation).

```sql
CREATE TABLE refresh_tokens (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT NOT NULL,                   -- dashboard user ID or email
    organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
    token_hash      TEXT NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    is_revoked      BOOLEAN NOT NULL DEFAULT FALSE,
    rotated_by      UUID,                           -- ID of the token that replaced this one
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_refresh_tokens_token_hash UNIQUE (token_hash)
);

COMMENT ON TABLE refresh_tokens IS 'Dashboard JWT refresh tokens with rotation tracking (SEC-08).';
COMMENT ON COLUMN refresh_tokens.token_hash IS 'SHA-256 hash of the refresh token. Raw token never stored.';
COMMENT ON COLUMN refresh_tokens.rotated_by IS 'ID of the replacement token. Enables rotation chain for audit.';
COMMENT ON COLUMN refresh_tokens.expires_at IS 'Hard expiry. Past this date, token is rejected even if not revoked.';
```

**Indexes:**

```sql
CREATE INDEX idx_refresh_tokens_organization_id ON refresh_tokens(organization_id);
CREATE INDEX idx_refresh_tokens_expires_at ON refresh_tokens(expires_at)
    WHERE is_revoked = FALSE;
```

---

### 2.13 `audit_log` (ADDED — missing from SRS §7.1)

Immutable audit trail for security-sensitive operations (API key creation, org changes, user deletion).

```sql
CREATE TABLE audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES organizations(id) ON DELETE SET NULL,
    actor_type      TEXT NOT NULL
                    CHECK (actor_type IN ('api_key', 'dashboard_user', 'system')),
    actor_id        TEXT NOT NULL,
    action          TEXT NOT NULL,
    resource_type   TEXT NOT NULL,
    resource_id     TEXT,
    metadata        JSONB DEFAULT '{}'::jsonb,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE audit_log IS 'Immutable audit trail. INSERT-only — no UPDATE or DELETE permitted by application roles.';
COMMENT ON COLUMN audit_log.actor_type IS 'Type of actor performing the action: api_key (API key), dashboard_user (JWT user), system (worker/cron).';
COMMENT ON COLUMN audit_log.action IS 'Action performed, e.g., api_key.created, user.deleted, org.quota_updated.';
COMMENT ON COLUMN audit_log.resource_type IS 'Type of resource affected, e.g., organization, user, api_key, session.';
```

**Indexes:**

```sql
CREATE INDEX idx_audit_log_organization_id ON audit_log(organization_id);
CREATE INDEX idx_audit_log_occurred_at ON audit_log(occurred_at DESC);
CREATE INDEX idx_audit_log_actor ON audit_log(actor_type, actor_id);
CREATE INDEX idx_audit_log_action ON audit_log(action, occurred_at DESC);
```

**Rationale:**
- This is an INSERT-only table. Application-level DB users must not have UPDATE or DELETE privileges on it.
- The table intentionally uses SET NULL on org deletion — audit records are preserved even if the org is removed.
- Index strategy is write-optimized: limited indexes, all B-tree (no GIN/GiST that slow inserts).

---

### 2.14 `llm_usage` (ADDED — missing from SRS §7.1)

Token usage accounting for LLM and embedding API calls. Drives cost analytics (dashboard panels) and per-org billing.

```sql
CREATE TABLE llm_usage (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id   UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    task_type         TEXT NOT NULL,
    model             TEXT NOT NULL,
    provider          TEXT NOT NULL DEFAULT 'openai',
    prompt_tokens     INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    total_tokens      INT NOT NULL DEFAULT 0,
    cost_usd          NUMERIC(12, 8) NOT NULL DEFAULT 0,
    duration_ms       INT NOT NULL DEFAULT 0,
    request_id        TEXT,                          -- trace ID for correlation
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE llm_usage IS 'Token and cost accounting for every LLM/embedding API call. Append-only.';
COMMENT ON COLUMN llm_usage.task_type IS 'Worker task or endpoint, e.g., entity_extraction, embed_episode, context_assembly.';
COMMENT ON COLUMN llm_usage.model IS 'Model identifier, e.g., gpt-4o, text-embedding-3-small.';
COMMENT ON COLUMN llm_usage.cost_usd IS 'Estimated cost in USD, computed from model pricing table.';
COMMENT ON COLUMN llm_usage.duration_ms IS 'API call latency in milliseconds.';
```

**Indexes:**

```sql
-- FK index
CREATE INDEX idx_llm_usage_organization_id ON llm_usage(organization_id);

-- Aggregate usage by org and time range (dashboard)
CREATE INDEX idx_llm_usage_org_created ON llm_usage(organization_id, created_at DESC);

-- Aggregate usage by task type (cost breakdown)
CREATE INDEX idx_llm_usage_task ON llm_usage(task_type, created_at DESC);
```

**Rationale:**
- Append-only for accurate accounting. No UPDATE, no DELETE.
- `cost_usd` is pre-computed at insert time using a pricing lookup — avoids expensive per-query cost calculation.
- `NUMERIC(12, 8)` gives 4 decimal places of sub-cent precision with 8 digits before the decimal (enough for millions of dollars).

---

## 3. Service Layer

### 3.1 Entity Relationship Summary

```
organizations 1───* api_keys
organizations 1───* users
organizations 1───* extraction_schemas
organizations 1───* refresh_tokens
organizations 1───* audit_log (SET NULL)
organizations 1───* llm_usage
users 1───* sessions
users 1───* episodes
users 1───* facts
sessions 1───* episodes
sessions 1───* structured_extractions
sessions 1───* dialog_classifications (via episodes)
episodes 1───* dialog_classifications
episodes 1───* facts (source_episode_id, SET NULL)
extraction_schemas 1───* structured_extractions (SET NULL)
```

### 3.2 Multi-Tenant Query Pattern (MT-01, MT-02)

Every query that accesses tenant-scoped data MUST include an `organization_id` filter. The canonical pattern:

```python
# repositories/base.py — abstract base repository with tenant enforcement
class TenantAwareRepository(ABC):
    """All tenant-scoped repositories inherit from this.

    Every query method requires organization_id as a parameter.
    Cross-tenant access is structurally impossible at the query layer.
    """

    @abstractmethod
    def _tenant_table(self) -> sa.Table:
        """Return the SQLAlchemy table for this repository."""

    async def _assert_org_access(
        self, db: AsyncSession, org_id: UUID, row_id: UUID
    ) -> None:
        """Verify that a row belongs to the given org. Raises 403 if not."""
        # Implemented by each repository that has organization_id directly,
        # or joins through user -> org for indirect tables.
        ...
```

**Rule:** The service layer is responsible for passing `organization_id` from the auth context. The repository layer is responsible for including it in every WHERE clause. The router layer is responsible for extracting it from the authenticated API key.

---

## 4. Implementation Notes

### 4.1 Alembic Migration Strategy

**File naming convention:**
```
alembic/versions/
├── 0001_initial_schema.py           # Phase 0: all 11 tables + extensions
├── 0002_add_episode_token_count.py  # Add token_count to episodes
├── 0003_add_fact_subject_type.py    # Add subject_type/object_type to facts
└── ...
```

**Parameterizing EMBEDDING_DIM:**

The `embedding` columns use `VECTOR(1536)` as a literal dimension in the DDL. This MUST be parameterized in Alembic migrations:

```python
# alembic/versions/0001_initial_schema.py
from alembic import op
import sqlalchemy as sa
from core.config import settings  # EMBEDDING_DIM = env var, default 1536

EMBEDDING_DIM = settings.EMBEDDING_DIM

def upgrade():
    op.create_table(
        "episodes",
        sa.Column("id", sa.UUID(), ...),
        sa.Column("embedding", sa.Vector(EMBEDDING_DIM), nullable=True),
        ...
    )
```

**Why not hardcode:** The dimension depends on the chosen embedding model (see `03-embedding-strategy.md`). Hardcoding `1536` means a model change requires editing migration files. Using `settings.EMBEDDING_DIM` ensures the migration always matches the configured model.

**Sequential migrations, not squashed:**
- Every migration file gets a unique sequential number (`0001_`, `0002_`, etc.)
- Never squash migrations — history matters for rollback
- Each migration must have `downgrade()` even if it's a no-op

**Two-step column drops:**
```python
# Step 1 (deploy 1): Mark column as deprecated, stop writing to it
def upgrade():
    # Add a comment, code stops reading this column
    pass

# Step 2 (deploy 2, after all code is updated): Drop column
def upgrade():
    op.drop_column("episodes", "deprecated_column")
```

### 4.2 FK Index Audit (Complete — 14 FK indexes)

Every foreign key column MUST have an index. PostgreSQL does NOT auto-index FKs. Missing FK indexes cause sequential scans on JOIN queries and lock contention on DELETE CASCADE.

| # | Table | FK Column | References | Index Name |
|---|-------|-----------|-----------|------------|
| 1 | api_keys | organization_id | organizations(id) | `idx_api_keys_organization_id` |
| 2 | users | organization_id | organizations(id) | `idx_users_organization_id` |
| 3 | sessions | user_id | users(id) | `idx_sessions_user_id` |
| 4 | episodes | session_id | sessions(id) | `idx_episodes_session_id` |
| 5 | episodes | user_id | users(id) | `idx_episodes_user_id` |
| 6 | facts | user_id | users(id) | `idx_facts_user_id` |
| 7 | facts | source_episode_id | episodes(id) | `idx_facts_source_episode_id` |
| 8 | structured_extractions | session_id | sessions(id) | `idx_structured_extractions_session_id` |
| 9 | structured_extractions | schema_id | extraction_schemas(id) | `idx_structured_extractions_schema_id` |
| 10 | dialog_classifications | episode_id | episodes(id) | `idx_dialog_classifications_episode_id` |
| 11 | extraction_schemas | organization_id | organizations(id) | `idx_extraction_schemas_organization_id` |
| 12 | refresh_tokens | organization_id | organizations(id) | `idx_refresh_tokens_organization_id` |
| 13 | audit_log | organization_id | organizations(id) | `idx_audit_log_organization_id` |
| 14 | llm_usage | organization_id | organizations(id) | `idx_llm_usage_organization_id` |

### 4.3 Environment Variables

| Variable | Default | Applies To | Description |
|----------|---------|-----------|-------------|
| `DATABASE_URL` | — | All tables | PostgreSQL DSN with async driver: `postgresql+asyncpg://user:pass@host:5432/OpenZep` |
| `EMBEDDING_DIM` | `1536` | episodes, facts | Vector dimension matching the embedding model |
| `PGVECTOR_LISTS` | `100` | IVFFlat indexes | Number of IVF centroids — see `03-embedding-strategy.md` |
| `FACT_CONFIDENCE_THRESHOLD` | `0.7` | facts | Minimum confidence score for facts to appear in context |
| `DB_POOL_SIZE` | `20` | Connection pool | asyncpg pool_size |
| `DB_MAX_OVERFLOW` | `10` | Connection pool | asyncpg max_overflow |
| `DB_POOL_PRE_PING` | `true` | Connection pool | Verify connection before use |

### 4.4 Performance Notes

**Write path optimization:**
- `episodes` and `facts` are write-heavy tables. The IVFFlat indexes on `embedding` columns incur write amplification — each insert updates the index. For bulk ingestion (>100 episodes/sec), consider:
  1. Setting `synchronous_commit = off` for the session (risk: 1 transaction loss on crash)
  2. Batching inserts in a single transaction
  3. Dropping and rebuilding the IVFFlat index after bulk load

**Query patterns to monitor:**
- Context assembly queries `facts` with temporal filters + embedding similarity → ensure `idx_facts_user_temporal` and `idx_facts_embedding_ivfflat` are both used (`EXPLAIN ANALYZE` to verify bitmap combine).
- Session message retrieval → `idx_episodes_session_sequence` covers ORDER BY sequence_number without a sort.
- Full-text search → verify GIN index is hit with `EXPLAIN ANALYZE SELECT ... WHERE to_tsvector('english', content) @@ to_tsquery('english', 'query')`.

**Dead tuple management:**
- `episodes` and `facts` have frequent UPDATE patterns (marking `is_active`, setting `invalid_at`). Monitor `n_dead_tup` in pg_stat_user_tables.
- Aggressive autovacuum settings for these tables:
  ```sql
  ALTER TABLE episodes SET (autovacuum_vacuum_scale_factor = 0.01);
  ALTER TABLE facts SET (autovacuum_vacuum_scale_factor = 0.01);
  ```

### 4.5 CHECK Constraint Summary

| Table | Constraint | Purpose |
|-------|-----------|---------|
| organizations | `plan IN ('free', 'pro', 'enterprise')` | Valid plan tiers |
| api_keys | `prefix IN ('mg_live_', 'mg_test_')` | Valid key prefix (AUTH-03) |
| episodes | `role IN ('user', 'assistant', 'system', 'tool')` | Valid message role |
| episodes | `char_length(content) <= 65536` | 64KB max content (SEC-09) |
| facts | `confidence >= 0 AND confidence <= 1` | Valid confidence range |
| facts | `valid_from < valid_to` | Temporal validity sanity |
| dialog_classifications | `valence IN ('positive', 'neutral', 'negative')` | Valid valence labels |
| dialog_classifications | `arousal IN ('low', 'high')` | Valid arousal labels |
| dialog_classifications | `confidence >= 0 AND confidence <= 1` | Valid confidence range |
| audit_log | `actor_type IN ('api_key', 'dashboard_user', 'system')` | Valid actor types |

---

## 5. Testing Guidance

### 5.1 Unit Tests (no DB)

| Test | What to Cover |
|------|--------------|
| TimestampMixin | Verify columns exist on model instances |
| CHECK constraint models | Verify Pydantic/constr models match DB CHECK constraints |
| Tenant enforcement | Mock repository and verify organization_id is added to every query |
| Migration parameterization | Verify `settings.EMBEDDING_DIM` is used in migration script |

### 5.2 Integration Tests (real PostgreSQL via testcontainers)

| Test | What to Cover |
|------|--------------|
| Schema creation | `alembic upgrade head` completes without errors |
| All 11 tables exist | `SELECT table_name FROM information_schema.tables` count = 11 |
| All CHECK constraints | INSERT violating each CHECK → expect constraint violation error |
| All UNIQUE constraints | INSERT duplicate → expect unique violation |
| FK cascade: org delete | DELETE organization → verify cascaded deletes on all children |
| FK SET NULL: source_episode_id | DELETE episode referenced by facts → verify source_episode_id IS NULL |
| FK SET NULL: schema_id | DELETE schema referenced by structured_extractions → verify schema_id IS NULL |
| IVFFlat index creation | CREATE INDEX ... IVFFLAT → verify index exists in pg_indexes |
| GIN index creation | CREATE INDEX ... GIN → verify index works with `@@ to_tsquery` |
| Temporal query | INSERT facts with valid_from/valid_to → query with timestamp → verify correct rows returned |
| Full-text search | INSERT episode → `to_tsvector('english', content) @@ to_tsquery('english', 'test')` → match |
| Trigger audit (if applicable) | INSERT into audit_log → verify no UPDATE/DELETE allowed by application user |

### 5.3 Edge Cases to Cover

1. **NULL vector columns:** Episodes and facts are inserted without embeddings initially. Verify query patterns that filter on `embedding IS NOT NULL` or `embedding IS NULL`.
2. **Zero-length content:** The `char_length(content) <= 65536` CHECK allows empty string (length 0). Decide if the service layer should reject empty content.
3. **Confidence = 0:** Facts with confidence 0 are allowed by the CHECK but should be filtered by the service layer. Test that they are excluded from context.
4. **valid_from == valid_to:** The CHECK `valid_from < valid_to` rejects this. Verify the error message is user-friendly.
5. **orgs with no users:** Verify list-users pagination handles empty results correctly.
6. **Concurrent session sequence:** Two messages arriving simultaneously for the same session. The `UNIQUE (session_id, sequence_number)` prevents duplicates. Test retry logic in the service layer.
7. **Large content (64KB boundary):** Insert content at exactly 65536 characters (edge), then 65537 (rejected by CHECK).
8. **JSONB metadata injection:** Verify that metadata JSONB cannot contain keys that shadow column names (handled by Pydantic schema validation, not DB layer).

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*

**SRS traceability:** This document implements all data model requirements from SRS §7.1 plus audit-identified gaps (`extraction_schemas`, `refresh_tokens`, `audit_log`, `llm_usage`), covering 12 tables, 14 FK indexes, 9 CHECK constraints, and the complete multi-tenant schema for OpenZep Phase 0.
