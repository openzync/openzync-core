# Entity Extraction Worker

> **Phase:** 1 (Core Memory) — P0 requirement
> **SRS References:** NLP-01, NLP-02, NLP-03, NLP-04, WRK-01, WRK-02, WRK-03
> **Index:** 5.2 — Read after 01-prompt-templates.md and 06-worker-system/01-arq-setup.md

---

## 1. Overview

The entity extraction worker is an ARQ background task that runs after each message ingestion. It calls an LLM with the `extract_entities_v1.jinja2` prompt, parses the structured JSON response, and persists entities and relationships to the graph database (Graphiti) and PostgreSQL.

**Data flow:**

```
POST /memory → ARQ enqueue → Entity Extraction Worker
                                 │
                          ┌──────▼──────┐
                          │  Prompt      │
                          │  Renderer    │
                          └──────┬──────┘
                                 │ rendered prompt
                          ┌──────▼──────┐
                          │  LLM Call    │
                          │  (JSON mode) │
                          └──────┬──────┘
                                 │ JSON response
                          ┌──────▼──────┐
                          │  Parse &     │
                          │  Validate    │
                          └──────┬──────┘
                                 │ validated entities
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
              EntityNode   RELATES_TO    episodes
              in GraphDB   edges         table update
```

---

## 2. Worker Task Definition

### 2.1 Task Registration

```python
# services/worker/tasks/entity_extraction.py

from dataclasses import dataclass, field
from uuid import UUID
from typing import Any


@dataclass
class EntityExtractionInput:
    """Input for the entity extraction worker task.

    Attributes:
        episode_id: The episode record that was just ingested.
        user_id: The user who sent the message.
        org_id: The organization (tenant) scope.
        content: The message text content.
        session_id: The conversation session (optional, may be None).
    """
    episode_id: UUID
    user_id: UUID
    org_id: UUID
    content: str
    session_id: UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

### 2.2 ARQ Task Handler

```python
# services/worker/tasks/entity_extraction.py (continued)

import structlog
from arq import Retry
from uuid import UUID

from services.worker.prompts.renderer import PromptRenderer
from services.llm.client import LLMClient
from services.worker.repositories.entity_repo import EntityRepository
from services.worker.schemas.entity import EntityExtractionResponse

logger = structlog.get_logger(__name__)


async def extract_entities(ctx: dict, task_input: dict) -> dict:
    """ARQ worker task: extract entities from a message.

    Triggered after message ingestion on the high priority queue.

    Args:
        ctx: ARQ worker context (contains Redis, DB session factory, etc.)
        task_input: Serialized EntityExtractionInput

    Returns:
        Summary dict with extracted entity count and status.

    Raises:
        Retry: On transient failures (LLM timeout, DB contention).
    """
    input_data = EntityExtractionInput(**task_input)
    log = logger.bind(
        episode_id=str(input_data.episode_id),
        user_id=str(input_data.user_id),
        org_id=str(input_data.org_id),
    )

    # ⚠️ Idempotency guard: skip if already processed
    repo: EntityRepository = ctx["entity_repo"]
    if await repo.has_extraction(input_data.episode_id):
        log.info("entity_extraction.skipped.already_processed")
        return {"status": "skipped", "reason": "already_processed"}

    # 1. Check budget before LLM call
    cost_controller = ctx["cost_controller"]
    if not await cost_controller.check_budget(input_data.org_id, "entity_extraction"):
        log.warning("entity_extraction.skipped.budget_exceeded")
        return {"status": "skipped", "reason": "daily_budget_exceeded"}

    # 2. Load config (check for per-org override)
    config = await repo.get_extraction_config(input_data.org_id, "entity_extraction")

    # 3. Render prompt
    renderer: PromptRenderer = ctx["prompt_renderer"]
    prompt = renderer.render(
        "extract_entities",
        version=config.get("prompt_version", 1),
        conversation_text=input_data.content,
        ontology=config.get("ontology"),
        language=config.get("language"),
        confidence_threshold=config.get("confidence_threshold", 0.5),
    )

    # 4. Call LLM with retry
    llm: LLMClient = ctx["llm_client"]
    max_retries = 3
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            response = await llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=2000,
                temperature=0.0,  # deterministic extraction
            )
            raw_json = response["choices"][0]["message"]["content"]

            # 5. Parse and validate
            parsed = EntityExtractionResponse.model_validate_json(raw_json)
            break  # success

        except Exception as e:
            last_error = e
            log.warning(
                "entity_extraction.retry",
                attempt=attempt,
                error=str(e),
            )
            if attempt == max_retries:
                raise Retry(defer=ctx["job_retry_delay"] * (2 ** (attempt - 1))) from e

            # On parse failure, use recovery prompt for retry
            if "model_validate_json" in type(e).__name__:
                recovery_prompt = renderer.render(
                    "extract_entities",
                    version=config.get("prompt_version", 1),
                    conversation_text=input_data.content,
                    ontology=config.get("ontology"),
                    language=config.get("language"),
                    confidence_threshold=config.get("confidence_threshold", 0.5),
                    recovery=True,
                    parse_error=str(e),
                )
                prompt = recovery_prompt

    # 6. Persist to graph DB and PostgreSQL
    db_session = ctx["db_session"]
    graph_client = ctx["graph_client"]

    entity_count = 0
    for entity in parsed.entities:
        # 6a. Dedup check: same name + org_id → append to existing
        existing = await repo.find_entity_by_name_and_org(
            name=entity.name, org_id=input_data.org_id
        )
        if existing:
            await repo.append_episode_to_entity(
                entity_id=existing.id,
                episode_id=input_data.episode_id,
            )
            node_id = existing.graphiti_node_id
        else:
            node_id = await graph_client.create_entity_node(
                org_id=input_data.org_id,
                user_id=input_data.user_id,
                name=entity.name,
                entity_type=entity.type,
                summary=entity.summary,
            )
            await repo.create_entity(
                org_id=input_data.org_id,
                user_id=input_data.user_id,
                name=entity.name,
                entity_type=entity.type,
                summary=entity.summary,
                graphiti_node_id=node_id,
                episode_id=input_data.episode_id,
                confidence=entity.confidence,
            )
            entity_count += 1

        # 6b. Update episodes table with graphiti_node_id reference
        await repo.update_episode_graphiti_ref(
            episode_id=input_data.episode_id,
            graphiti_node_id=node_id,
        )

    # 7. Create relationships
    for rel in parsed.relationships:
        subj_entity = await repo.find_entity_by_name_and_org(
            name=rel.subject, org_id=input_data.org_id
        )
        obj_entity = await repo.find_entity_by_name_and_org(
            name=rel.object, org_id=input_data.org_id
        )
        if subj_entity and obj_entity:
            await graph_client.create_relationship(
                org_id=input_data.org_id,
                subject_node_id=subj_entity.graphiti_node_id,
                predicate=rel.predicate,
                object_node_id=obj_entity.graphiti_node_id,
                fact=rel.fact,
                confidence=rel.confidence,
                episode_id=input_data.episode_id,
            )

    # 8. Mark extracted
    await repo.mark_extracted(input_data.episode_id)

    log.info(
        "entity_extraction.completed",
        entities_created=entity_count,
        relationships_created=len(parsed.relationships),
        tokens_used=response.get("usage", {}),
    )

    return {
        "status": "completed",
        "entity_count": entity_count,
        "relationship_count": len(parsed.relationships),
    }
```

### 2.3 Queue Assignment

```python
# services/worker/tasks/__init__.py

from arq.connections import RedisSettings

class WorkerSettings:
    functions = [
        extract_entities,  # high priority
        # ... other tasks
    ]
    redis_settings = RedisSettings.from_dsn("redis://localhost:6379")
    queue_name = "high"  # high priority queue
```

---

## 3. Schema Definitions

### 3.1 Pydantic Response Schema

```python
# services/worker/schemas/entity.py

from pydantic import BaseModel, Field
from typing import Any


class Entity(BaseModel):
    """A single extracted entity."""
    name: str = Field(..., description="Entity name")
    type: str = Field(..., description="Entity type: person, organization, location, product, date, custom")
    custom_type: str | None = Field(None, description="Custom type when type=custom")
    summary: str = Field("", description="Brief description of the entity in context")
    mentions: list[str] = Field(default_factory=list, description="Mentioned text fragments")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class Relationship(BaseModel):
    """A typed relationship between two entities."""
    subject: str = Field(..., description="Subject entity name")
    predicate: str = Field(..., description="Relationship type")
    object: str = Field(..., description="Object entity name")
    fact: str = Field("", description="Natural language description")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class EntityExtractionResponse(BaseModel):
    """Validated LLM response for entity extraction."""
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
```

### 3.2 PostgreSQL Entity Table

```sql
-- Extension of episodes table — entity tracking
CREATE TABLE extracted_entities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    summary         TEXT DEFAULT '',
    confidence      FLOAT4 DEFAULT 0.5,
    graphiti_node_id TEXT,                  -- reference to graph DB node
    episode_ids     UUID[] DEFAULT '{}',     -- all episodes mentioning this entity
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_extracted_entities_name ON extracted_entities (organization_id, name);
CREATE INDEX idx_extracted_entities_user ON extracted_entities (user_id);
CREATE UNIQUE INDEX idx_extracted_entities_org_name ON extracted_entities (organization_id, LOWER(name));
```

---

## 4. Entity Deduplication Logic

### 4.1 Algorithm

```
BEFORE creating a new EntityNode:

1. SELECT FROM extracted_entities
   WHERE organization_id = :org_id
     AND LOWER(name) = LOWER(:candidate_name)

2. IF existing row found:
   a. Append this episode_id to existing.episode_ids
   b. Update existing.last_seen_at
   c. Do NOT create new EntityNode in graph DB
   d. Return existing.graphiti_node_id

3. IF no existing row:
   a. Create new EntityNode in graph DB
   b. INSERT into extracted_entities
   c. Return new graphiti_node_id
```

### 4.2 Implementation

```python
# services/worker/repositories/entity_repo.py

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID


class EntityRepository:
    """Repository for entity extraction data."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def find_entity_by_name_and_org(
        self, name: str, org_id: UUID
    ) -> dict | None:
        """Check if an entity with this name already exists in the org."""
        result = await self._db.execute(
            text("""
                SELECT id, graphiti_node_id, episode_ids
                FROM extracted_entities
                WHERE organization_id = :org_id
                  AND LOWER(name) = LOWER(:name)
                LIMIT 1
            """),
            {"org_id": org_id, "name": name},
        )
        row = result.one_or_none()
        if row is None:
            return None
        return {
            "id": row[0],
            "graphiti_node_id": row[1],
            "episode_ids": row[2],
        }

    async def append_episode_to_entity(
        self, entity_id: UUID, episode_id: UUID
    ) -> None:
        """Append an episode ID to an existing entity's episode list."""
        await self._db.execute(
            text("""
                UPDATE extracted_entities
                SET episode_ids = array_append(episode_ids, :episode_id),
                    last_seen_at = now()
                WHERE id = :entity_id
            """),
            {"entity_id": entity_id, "episode_id": episode_id},
        )
        await self._db.flush()

    async def create_entity(
        self,
        org_id: UUID,
        user_id: UUID,
        name: str,
        entity_type: str,
        summary: str,
        graphiti_node_id: str,
        episode_id: UUID,
        confidence: float,
    ) -> UUID:
        """Create a new extracted entity record."""
        result = await self._db.execute(
            text("""
                INSERT INTO extracted_entities
                    (organization_id, user_id, name, entity_type,
                     summary, graphiti_node_id, episode_ids, confidence)
                VALUES
                    (:org_id, :user_id, :name, :entity_type,
                     :summary, :graphiti_node_id, ARRAY[:episode_id], :confidence)
                RETURNING id
            """),
            {
                "org_id": org_id,
                "user_id": user_id,
                "name": name,
                "entity_type": entity_type,
                "summary": summary,
                "graphiti_node_id": graphiti_node_id,
                "episode_id": episode_id,
                "confidence": confidence,
            },
        )
        await self._db.flush()
        return result.scalar_one()

    async def update_episode_graphiti_ref(
        self, episode_id: UUID, graphiti_node_id: str
    ) -> None:
        """Link an episode to its graph node."""
        await self._db.execute(
            text("""
                UPDATE episodes
                SET graphiti_node_id = :graphiti_node_id
                WHERE id = :episode_id
            """),
            {"episode_id": episode_id, "graphiti_node_id": graphiti_node_id},
        )
        await self._db.flush()

    async def mark_extracted(self, episode_id: UUID) -> None:
        """Mark an episode as having been processed."""
        await self._db.execute(
            text("""
                UPDATE episodes
                SET metadata = jsonb_set(
                    COALESCE(metadata, '{}'),
                    '{extracted_entities}',
                    'true'::jsonb
                )
                WHERE id = :episode_id
            """),
            {"episode_id": episode_id},
        )
        await self._db.flush()

    async def has_extraction(self, episode_id: UUID) -> bool:
        """Check if entities have already been extracted for this episode."""
        result = await self._db.execute(
            text("""
                SELECT metadata->>'extracted_entities'
                FROM episodes
                WHERE id = :episode_id
            """),
            {"episode_id": episode_id},
        )
        row = result.scalar_one_or_none()
        return row == "true"

    async def get_extraction_config(
        self, org_id: UUID, task: str
    ) -> dict:
        """Load per-org extraction config from entity_ontologies or return defaults."""
        result = await self._db.execute(
            text("""
                SELECT prompt_template, ontology, language,
                       confidence_threshold, prompt_version
                FROM entity_ontologies
                WHERE organization_id = :org_id AND task = :task
            """),
            {"org_id": org_id, "task": task},
        )
        row = result.one_or_none()
        if row:
            return {
                "prompt_template": row[0],
                "ontology": row[1],
                "language": row[2],
                "confidence_threshold": row[3] or 0.5,
                "prompt_version": row[4] or 1,
            }
        return {
            "prompt_template": None,
            "ontology": None,
            "language": None,
            "confidence_threshold": 0.5,
            "prompt_version": 1,
        }
```

---

## 5. Graph Client Integration

```python
# packages/graphiti-client/client.py

from uuid import UUID


class GraphClient:
    """Thin wrapper around Graphiti for entity operations."""

    async def create_entity_node(
        self,
        org_id: UUID,
        user_id: UUID,
        name: str,
        entity_type: str,
        summary: str,
    ) -> str:
        """Create an EntityNode in the graph database.

        Returns:
            The graph node ID (used as graphiti_node_id in PostgreSQL).
        """
        # Delegate to Graphiti's entity creation
        # Implementation depends on Graphiti API version
        ...

    async def create_relationship(
        self,
        org_id: UUID,
        subject_node_id: str,
        predicate: str,
        object_node_id: str,
        fact: str,
        confidence: float,
        episode_id: UUID,
    ) -> str:
        """Create a RELATES_TO edge between two entity nodes.

        The edge carries the fact text as a property, the relationship type
        as the predicate, and the episode source for provenance.
        """
        ...
```

---

## 6. LLM Call Configuration

```python
# services/llm/client.py

from dataclasses import dataclass


@dataclass
class LLMConfig:
    """LLM client configuration for extraction tasks."""
    model: str = "gpt-4o-mini"  # default — fast & cheap for extraction
    temperature: float = 0.0    # deterministic output
    max_tokens: int = 2000
    timeout_seconds: int = 30
    max_retries: int = 3
    retry_base_delay: float = 1.0  # exponential backoff base


class LLMClient:
    """Async LLM client supporting OpenAI, Azure, and Ollama backends."""

    async def chat_completion(
        self,
        messages: list[dict],
        response_format: dict | None = None,
        max_tokens: int = 2000,
        temperature: float = 0.0,
    ) -> dict:
        """Send a chat completion request.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            response_format: JSON mode config (e.g., {"type": "json_object"}).
            max_tokens: Maximum completion tokens.
            temperature: Sampling temperature.

        Returns:
            Full OpenAI-compatible response dict.

        Raises:
            LLMTimeoutError: On timeout after retries.
            LLMStatusError: On non-200 response.
        """
        ...
```

---

## 7. Error Handling & Retry

| Failure Mode | Detection | Action |
|-------------|-----------|--------|
| LLM timeout (30s) | `asyncio.TimeoutError` | Retry with exponential backoff (1s → 2s → 4s). Max 3 attempts. |
| LLM returns invalid JSON | `pydantic.ValidationError` | Retry with recovery prompt that includes the parse error. |
| LLM returns empty entities | Empty list in response | Accept as valid — not all messages contain entities. Return 0 count. |
| Graph DB unavailable | Connection error | Retry up to 3 times with backoff. If still failing, log and mark for DLQ. |
| PostgreSQL constraint violation | Unique constraint error | Log warning — likely a race condition. Return success (dedup handled). |

### Retry Configuration

```python
# Worker settings for this specific task
ctx["job_retry_delay"] = 1  # seconds — doubles each retry (1, 2, 4)

# Max retries before DLQ
ctx["job_max_retries"] = 3
```

---

## 8. Metrics & Observability

The entity extraction worker emits the following metrics:

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `openzep_llm_tokens_total` | Counter | `org_id, task=entity_extraction` | Total LLM tokens consumed |
| `openzep_entities_extracted_total` | Counter | `org_id` | Entities created (not dedup) |
| `openzep_entity_extraction_duration_seconds` | Histogram | `org_id` | Wall-clock time per extraction task |
| `openzep_worker_tasks_total` | Counter | `task=extract_entities, status=success/failure/skip` | Task outcomes |

### Structured Logging

```python
log.info(
    "entity_extraction.completed",
    entity_count=entity_count,
    relationship_count=len(parsed.relationships),
    tokens_prompt=response["usage"]["prompt_tokens"],
    tokens_completion=response["usage"]["completion_tokens"],
    duration_ms=round((time.monotonic() - start_time) * 1000),
    model=response["model"],
    dedup_skipped=dedup_count,
)
```

### Token Accounting

Every LLM call logs to the `llm_usage` table for cost tracking:

```python
await cost_controller.record_usage(
    org_id=input_data.org_id,
    model=response["model"],
    prompt_tokens=response["usage"]["prompt_tokens"],
    completion_tokens=response["usage"]["completion_tokens"],
    task_type="entity_extraction",
    episode_id=input_data.episode_id,
)
```

---

## 9. Eval & Acceptance Criteria

| Metric | Target | Measurement |
|--------|--------|-------------|
| Precision | >= 0.80 | `true_positives / (true_positives + false_positives)` on golden dataset |
| Recall | >= 0.70 | `true_positives / (true_positives + false_negatives)` on golden dataset |
| Latency p50 | <= 10s | Worker wall-clock from enqueue to completion |
| Latency p99 | <= 30s | Worker wall-clock from enqueue to completion (includes retries) |
| Failure rate | <= 1% | Failed tasks / total tasks |

The golden dataset is defined in `tests/evals/golden_entity_extraction.json` with annotated entity and relationship ground truths.

```python
# tests/evals/test_entity_extraction.py (conceptual)

async def test_entity_extraction_precision_recall():
    dataset = load_golden_dataset("entity_extraction")
    tp = fp = fn = 0

    for case in dataset:
        result = await extract_entities_worker(mock_ctx, case.input)
        expected = case.expected_entities

        for e in result.entities:
            if e.name in [exp.name for exp in expected]:
                tp += 1
            else:
                fp += 1
        for exp in expected:
            if exp.name not in [e.name for e in result.entities]:
                fn += 1

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0

    assert precision >= 0.80, f"Precision {precision:.3f} < 0.80"
    assert recall >= 0.70, f"Recall {recall:.3f} < 0.70"
```

---

## 10. Sequence Diagram

```
┌─────────┐   ┌──────────┐   ┌───────────┐   ┌──────┐   ┌─────────┐   ┌─────────┐
│  Router  │   │   ARQ    │   │ Extraction│   │ LLM  │   │  Graph  │   │   PG    │
│ (Ingest) │   │  Queue   │   │  Worker   │   │ API  │   │   DB    │   │         │
└────┬─────┘   └────┬─────┘   └─────┬─────┘   └──┬───┘   └────┬────┘   └────┬────┘
     │               │               │             │            │             │
     │  HTTP 202     │               │             │            │             │
     │◄──────────────│               │             │            │             │
     │               │ enqueue       │             │            │             │
     │──────────────►│──────────────►│             │            │             │
     │               │               │             │            │             │
     │               │               │ check budget│            │             │
     │               │               │──┐          │            │             │
     │               │               │  │ skip if  │            │             │
     │               │               │◄─┘ exceeded │            │             │
     │               │               │             │            │             │
     │               │               │ render      │            │             │
     │               │               │ prompt      │            │             │
     │               │               │──┐          │            │             │
     │               │               │  │          │            │             │
     │               │               │  │ chat     │            │             │
     │               │               │────────────►│            │             │
     │               │               │             │            │             │
     │               │               │◄────────────│ JSON       │             │
     │               │               │  (entities) │            │             │
     │               │               │──┐          │            │             │
     │               │               │  │ validate │            │             │
     │               │               │◄─┘          │            │             │
     │               │               │             │            │             │
     │               │               │ dedup check │            │             │
     │               │               │─────────────┤            ├────────────►│
     │               │               │◄────────────┤            │◄────────────┤
     │               │               │             │            │             │
     │               │               │ create node │            │             │
     │               │               │─────────────┤───────────►│             │
     │               │               │◄────────────┤◄───────────┤             │
     │               │               │             │            │             │
     │               │               │ insert row  │            │             │
     │               │               │─────────────┤            ├────────────►│
     │               │               │◄────────────┤            │◄────────────┤
     │               │               │             │            │             │
     │               │               │ mark done   │            │             │
     │               │               │──┐          │            │             │
     │               │               │  │ log      │            │             │
     │               │               │◄─┘ metrics  │            │             │
```

---

## 11. Testing Guide

### 11.1 Unit Tests

- `test_parse_valid_response` — valid JSON from LLM parses correctly
- `test_parse_missing_fields` — missing optional fields get defaults
- `test_parse_invalid_json` — invalid JSON raises ValidationError
- `test_dedup_same_org_same_name` — entity with same name → no new row
- `test_dedup_different_orgs` — same name, different org → two rows
- `test_idempotent_rerun` — processing same episode twice skips
- `test_empty_entities_accepted` — empty entity list is a valid response
- `test_budget_exceeded_skips` — returns early if budget check fails

### 11.2 Integration Tests

- Full pipeline: mock LLM returns known response → verify DB rows + graph calls
- LLM timeout: raises Retry after max attempts
- LLM returns invalid JSON on first attempt, valid on retry with recovery prompt
- Concurrent extraction on same entity: verify no duplicate rows

### 11.3 Eval Tests

- Run against golden dataset (see `tests/evals/golden_entity_extraction.json`)
- Measure precision, recall, F1
- Report per entity type (person, organization, etc.)

---

## 12. Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ENTITY_EXTRACTION_PROMPT` | `extract_entities_v1` | Default prompt version |
| `ENTITY_EXTRACTION_CONFIDENCE_THRESHOLD` | `0.5` | Min confidence to persist |
| `ENTITY_EXTRACTION_MAX_RETRIES` | `3` | Max LLM call retries |
| `ENTITY_EXTRACTION_TIMEOUT` | `30` | LLM call timeout (seconds) |
| `ENTITY_EXTRACTION_MODEL` | `gpt-4o-mini` | LLM model for extraction |
| `ENTITY_EXTRACTION_TEMPERATURE` | `0.0` | LLM temperature |
| `ENTITY_EXTRACTION_MAX_TOKENS` | `2000` | Max completion tokens |

---

## 13. Open Questions

| ID | Question | Status |
|----|----------|--------|
| EE-01 | Should we batch multiple episodes from the same session into one extraction call? | **Decision:** Phase 2 — single-episode for MVP, session-batched when community detection is added |
| EE-02 | Co-reference resolution across turns — current design extracts per-message. Should we pass the last N turns as context? | **Decision:** Phase 2 — pass last 5 episodes as conversation context |
| EE-03 | What happens when entity exceeds 50 associated episodes? | **Decision:** Trim episode_ids array to last 100, but keep graph edges intact |

---

## 14. Related Documents

| Document | Why |
|----------|-----|
| [01-prompt-templates.md](01-prompt-templates.md) | `extract_entities_v1.jinja2` template definition |
| [03-fact-extraction.md](03-fact-extraction.md) | Fact extraction runs in parallel with entity extraction |
| [04-knowledge-graph/01-graphiti-setup.md](../04-knowledge-graph/01-graphiti-setup.md) | Graph DB setup for EntityNode creation |
| [06-worker-system/01-arq-setup.md](../06-worker-system/01-arq-setup.md) | ARQ queue configuration |
| [07-llm-cost-control.md](07-llm-cost-control.md) | Budget checking before LLM call |
| [14-testing/03-golden-datasets.md](../14-testing/03-golden-datasets.md) | Golden dataset format and eval harness |
