# Fact Extraction Worker

> **Phase:** 1 (Core Memory) — P0 requirement
> **SRS References:** NLP-05, NLP-06, NLP-07, WRK-01, WRK-02
> **Index:** 5.3 — Read after 01-prompt-templates.md, runs in parallel with 02-entity-extraction.md

---

## 1. Overview

The fact extraction worker extracts **zero-shot factual statements** from conversation turns. Unlike entity extraction which follows a defined ontology, fact extraction has NO pre-defined schema — the LLM decides what constitutes a fact in the conversation context.

**Key distinction from entity extraction:**

| Aspect | Entity Extraction | Fact Extraction |
|--------|------------------|-----------------|
| Schema | Pre-defined (Person, Org, etc.) | Zero-shot — no schema |
| Output | Entities + typed relationships | Fact triples `(subject, predicate, object)` |
| Storage | Graph DB nodes + edges | `facts` PostgreSQL table |
| Trigger | After each message | After each message (parallel) |
| Confidence threshold | 0.5 | 0.3 (more permissive) |

**Data flow:**

```
POST /memory → ARQ high queue (parallel fan-out)
                    │
           ┌────────┴────────┐
           ▼                  ▼
    Entity Extraction    Fact Extraction
    (02-entity-extraction)  (this doc)
                               │
                        ┌──────▼──────┐
                        │  LLM: zero- │
                        │  shot fact  │
                        │  extraction │
                        └──────┬──────┘
                               │ fact triples
                        ┌──────▼──────┐
                        │  Validate & │
                        │  filter by  │
                        │  confidence │
                        └──────┬──────┘
                               │
                    ┌──────────┴──────────┐
                    ▼                     ▼
             INSERT into facts     RELATES_TO edge
             table (PostgreSQL)    in GraphDB (if entity match)
```

---

## 2. Worker Task Definition

### 2.1 Task Input

```python
# services/worker/tasks/fact_extraction.py

from dataclasses import dataclass, field
from uuid import UUID
from typing import Any


@dataclass
class FactExtractionInput:
    """Input for the fact extraction worker task.

    Attributes:
        episode_id: The episode record that was just ingested.
        user_id: The user who sent the message.
        org_id: The organization (tenant) scope.
        content: The message text content.
        session_id: The conversation session.
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
# services/worker/tasks/fact_extraction.py (continued)

import structlog
import json
from arq import Retry

from services.worker.prompts.renderer import PromptRenderer
from services.llm.client import LLMClient
from services.worker.repositories.fact_repo import FactRepository
from services.worker.schemas.fact import FactExtractionResponse

logger = structlog.get_logger(__name__)


async def extract_facts(ctx: dict, task_input: dict) -> dict:
    """ARQ worker task: extract facts from a message.

    Triggered after message ingestion on the high priority queue.
    Runs in parallel with entity extraction.

    Args:
        ctx: ARQ worker context
        task_input: Serialized FactExtractionInput

    Returns:
        Summary dict with extracted fact count and status.

    Raises:
        Retry: On transient failures.
    """
    input_data = FactExtractionInput(**task_input)
    log = logger.bind(
        episode_id=str(input_data.episode_id),
        user_id=str(input_data.user_id),
        org_id=str(input_data.org_id),
    )

    # Idempotency guard
    repo: FactRepository = ctx["fact_repo"]
    if await repo.has_extraction(input_data.episode_id):
        log.info("fact_extraction.skipped.already_processed")
        return {"status": "skipped", "reason": "already_processed"}

    # Budget check
    cost_controller = ctx["cost_controller"]
    if not await cost_controller.check_budget(input_data.org_id, "fact_extraction"):
        log.warning("fact_extraction.skipped.budget_exceeded")
        return {"status": "skipped", "reason": "daily_budget_exceeded"}

    # Load config
    config = await repo.get_extraction_config(input_data.org_id, "fact_extraction")

    # Render prompt
    renderer: PromptRenderer = ctx["prompt_renderer"]
    prompt = renderer.render(
        "extract_facts",
        version=config.get("prompt_version", 1),
        conversation_text=input_data.content,
        confidence_threshold=config.get("confidence_threshold", 0.3),
    )

    # Call LLM with retry
    llm: LLMClient = ctx["llm_client"]
    max_retries = 3
    parsed = None
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            response = await llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=2000,
                temperature=0.0,
            )
            raw_json = response["choices"][0]["message"]["content"]

            # Parse and validate
            parsed = FactExtractionResponse.model_validate_json(raw_json)

            # Filter low-confidence facts
            threshold = config.get("confidence_threshold", 0.3)
            parsed.facts = [f for f in parsed.facts if f.confidence >= threshold]
            break

        except Exception as e:
            last_error = e
            log.warning("fact_extraction.retry", attempt=attempt, error=str(e))
            if attempt == max_retries:
                raise Retry(defer=ctx["job_retry_delay"] * (2 ** (attempt - 1))) from e

            # Recovery prompt on parse failure
            if "model_validate_json" in type(e).__name__:
                prompt = renderer.render(
                    "extract_facts",
                    version=config.get("prompt_version", 1),
                    conversation_text=input_data.content,
                    confidence_threshold=config.get("confidence_threshold", 0.3),
                    recovery=True,
                    parse_error=str(e),
                )

    if parsed is None:
        log.error("fact_extraction.failed_all_retries")
        return {"status": "failed", "reason": "all_retries_exhausted"}

    # Persist facts
    graph_client = ctx["graph_client"]
    saved_count = 0

    for fact in parsed.facts:
        # Insert into facts table
        fact_id = await repo.create_fact(
            user_id=input_data.user_id,
            content=fact.subject + " " + fact.predicate + " " + fact.object,
            subject=fact.subject,
            predicate=fact.predicate,
            object=fact.object,
            confidence=fact.confidence,
            source_episode_id=input_data.episode_id,
            valid_from=fact.valid_from,
            valid_to=fact.valid_to,
        )

        # If subject/object matches a known entity, create graph edge
        entity_repo = ctx["entity_repo"]
        subj_entity = await entity_repo.find_entity_by_name_and_org(
            name=fact.subject, org_id=input_data.org_id
        )
        obj_entity = await entity_repo.find_entity_by_name_and_org(
            name=fact.object, org_id=input_data.org_id
        )

        if subj_entity and obj_entity:
            await graph_client.create_relationship(
                org_id=input_data.org_id,
                subject_node_id=subj_entity["graphiti_node_id"],
                predicate=fact.predicate,
                object_node_id=obj_entity["graphiti_node_id"],
                fact=fact.subject + " " + fact.predicate + " " + fact.object,
                confidence=fact.confidence,
                episode_id=input_data.episode_id,
            )

        saved_count += 1

    # Mark as processed
    await repo.mark_extracted(input_data.episode_id)

    log.info(
        "fact_extraction.completed",
        facts_saved=saved_count,
        total_extracted=len(parsed.facts),
        tokens_used=response.get("usage", {}),
    )

    return {
        "status": "completed",
        "fact_count": saved_count,
    }
```

---

## 3. Schema Definitions

### 3.1 Pydantic Response Schema

```python
# services/worker/schemas/fact.py

from pydantic import BaseModel, Field
from datetime import datetime
from typing import Any


class FactTriple(BaseModel):
    """A single fact triple extracted from conversation.

    Zero-shot — no pre-defined predicates. The LLM determines what
    constitutes a meaningful factual statement.
    """
    subject: str = Field(
        ...,
        description="The entity or concept the fact is about",
    )
    predicate: str = Field(
        ...,
        description="The relationship or action connecting subject to object",
    )
    object: str = Field(
        ...,
        description="The entity, value, or concept that is the target of the fact",
    )
    confidence: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description="LLM's confidence in this fact (0.0 to 1.0)",
    )
    valid_from: str | None = Field(
        None,
        description="ISO-8601 timestamp when this fact started being true (if inferable)",
    )
    valid_to: str | None = Field(
        None,
        description="ISO-8601 timestamp when this fact stopped being true (if inferable)",
    )


class FactExtractionResponse(BaseModel):
    """Validated LLM response for fact extraction."""
    facts: list[FactTriple] = Field(default_factory=list)
```

### 3.2 PostgreSQL Facts Table

```sql
-- From SRS Section 7.1 — facts table
CREATE TABLE facts (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content           TEXT NOT NULL,
    subject           TEXT,
    predicate         TEXT,
    object            TEXT,
    confidence        FLOAT4 DEFAULT 1.0,
    source_episode_id UUID REFERENCES episodes(id),
    valid_from        TIMESTAMPTZ,
    valid_to          TIMESTAMPTZ,
    invalid_at        TIMESTAMPTZ,
    embedding         VECTOR(1536),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_facts_user ON facts (user_id);
CREATE INDEX idx_facts_source_episode ON facts (source_episode_id);
CREATE INDEX idx_facts_confidence ON facts (confidence DESC);
CREATE INDEX idx_facts_subject ON facts (subject);
CREATE INDEX idx_facts_valid_from ON facts (valid_from);
CREATE INDEX ON facts USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

---

## 4. Repository Layer

```python
# services/worker/repositories/fact_repo.py

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID
from datetime import datetime


class FactRepository:
    """Repository for fact extraction data."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create_fact(
        self,
        user_id: UUID,
        content: str,
        subject: str | None,
        predicate: str | None,
        object: str | None,
        confidence: float,
        source_episode_id: UUID,
        valid_from: str | None = None,
        valid_to: str | None = None,
    ) -> UUID:
        """Insert a new fact into the facts table.

        Args:
            user_id: Owner of this fact.
            content: Natural language representation of the fact.
            subject: Subject entity name.
            predicate: Relationship predicate.
            object: Object entity name.
            confidence: Extraction confidence score.
            source_episode_id: Provenance — which episode produced this fact.
            valid_from: ISO-8601 timestamp when the fact became true.
            valid_to: ISO-8601 timestamp when the fact ceased to be true.

        Returns:
            UUID of the newly created fact.
        """
        result = await self._db.execute(
            text("""
                INSERT INTO facts
                    (user_id, content, subject, predicate, object,
                     confidence, source_episode_id,
                     valid_from, valid_to, invalid_at)
                VALUES
                    (:user_id, :content, :subject, :predicate, :object,
                     :confidence, :source_episode_id,
                     :valid_from::timestamptz, :valid_to::timestamptz, NULL)
                RETURNING id
            """),
            {
                "user_id": user_id,
                "content": content,
                "subject": subject,
                "predicate": predicate,
                "object": object,
                "confidence": confidence,
                "source_episode_id": source_episode_id,
                "valid_from": valid_from,
                "valid_to": valid_to,
            },
        )
        await self._db.flush()
        return result.scalar_one()

    async def has_extraction(self, episode_id: UUID) -> bool:
        """Check if facts have already been extracted for this episode."""
        result = await self._db.execute(
            text("""
                SELECT metadata->>'extracted_facts'
                FROM episodes
                WHERE id = :episode_id
            """),
            {"episode_id": episode_id},
        )
        row = result.scalar_one_or_none()
        return row == "true"

    async def mark_extracted(self, episode_id: UUID) -> None:
        """Mark an episode as having been fact-extracted."""
        await self._db.execute(
            text("""
                UPDATE episodes
                SET metadata = jsonb_set(
                    COALESCE(metadata, '{}'),
                    '{extracted_facts}',
                    'true'::jsonb
                )
                WHERE id = :episode_id
            """),
            {"episode_id": episode_id},
        )
        await self._db.flush()

    async def get_extraction_config(
        self, org_id: UUID, task: str
    ) -> dict:
        """Load per-org fact extraction config."""
        result = await self._db.execute(
            text("""
                SELECT prompt_template, language,
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
                "language": row[1],
                "confidence_threshold": row[2] or 0.3,
                "prompt_version": row[3] or 1,
            }
        return {
            "prompt_template": None,
            "language": None,
            "confidence_threshold": 0.3,
            "prompt_version": 1,
        }
```

---

## 5. Validation & Confidence Filtering

### 5.1 Confidence-Based Rejection

```
IF fact.confidence < org_config.confidence_threshold (default: 0.3):
    → REJECT the fact (log at DEBUG level)
    → DO NOT persist to DB or graph
    → Rationale: low-confidence extractions introduce noise into retrieval
```

### 5.2 Fact Quality Heuristics

Beyond confidence thresholds, apply these quality filters:

```python
def is_valid_fact(fact: FactTriple) -> bool:
    """Reject clearly invalid facts before persistence.

    Filters:
    1. Empty subject or object
    2. Predicate is too generic ('is', 'has', 'does')
    3. Fact is actually a conversational turn instruction
    4. Subject and object are identical
    """
    if not fact.subject or not fact.object:
        return False

    # Empty/whitespace-only fields
    if not fact.subject.strip() or not fact.object.strip():
        return False

    # Subject and object identical with generic predicate
    if fact.subject.lower() == fact.object.lower() and fact.predicate.lower() in {
        "is", "are", "has", "have", "does", "do", "was", "were"
    }:
        return False

    # Predicate suggests this is a meta-instruction, not a fact
    instruction_predicates = {
        "say", "tell", "output", "respond", "write", "repeat",
        "ignore", "forget", "remember", "act",
    }
    if fact.predicate.lower() in instruction_predicates:
        return False

    return True
```

### 5.3 Duplicate Detection

```python
async def is_duplicate_fact(
    db: AsyncSession,
    subject: str,
    predicate: str,
    object: str,
    org_id: UUID,
    window_minutes: int = 60,
) -> bool:
    """Check if an identical fact triple was extracted within the dedup window.

    Prevents the same fact from being extracted multiple times across
    different messages in the same conversation.
    """
    result = await db.execute(
        text("""
            SELECT COUNT(*)
            FROM facts f
            JOIN episodes e ON f.source_episode_id = e.id
            JOIN users u ON e.user_id = u.id
            WHERE u.organization_id = :org_id
              AND LOWER(f.subject) = LOWER(:subject)
              AND LOWER(f.predicate) = LOWER(:predicate)
              AND LOWER(f.object) = LOWER(:object)
              AND f.created_at > now() - INTERVAL ':window minutes'
        """),
        {
            "org_id": org_id,
            "subject": subject,
            "predicate": predicate,
            "object": object,
            "window": window_minutes,
        },
    )
    return result.scalar_one() > 0
```

---

## 6. Graph Edge Integration

When a fact's subject or object matches a known entity node (from entity extraction), a `RELATES_TO` edge is created in the graph. This connects the fact pipeline to the knowledge graph, enabling graph-traversal-based retrieval of facts.

### 6.1 Matching Logic

```
FOR EACH fact in parsed.facts:

    subj_entity = extracted_entities.find_by_name(fact.subject)
    obj_entity = extracted_entities.find_by_name(fact.object)

    IF subj_entity AND obj_entity:
        graph.create_relationship(
            subject=subj_entity.graphiti_node_id,
            predicate=fact.predicate,
            object=obj_entity.graphiti_node_id,
            properties={
                "fact": subject + " " + predicate + " " + object,
                "confidence": fact.confidence,
                "episode_id": episode_id,
                "valid_from": fact.valid_from,
                "valid_to": fact.valid_to,
            }
        )

    # ⚠️ If subject OR object does not match any known entity,
    # skip graph edge creation. The fact still exists in the
    # PostgreSQL facts table and is retrievable via vector/BFTS search.
```

---

## 7. Error Handling

| Failure Mode | Detection | Action |
|-------------|-----------|--------|
| LLM timeout | `asyncio.TimeoutError` | Retry 3x with exponential backoff (1s, 2s, 4s) |
| Invalid JSON | `pydantic.ValidationError` | Retry with recovery prompt showing the parse error |
| Empty facts list | Valid response with `facts: []` | Accept — no facts in this message. Do NOT retry. |
| DB insert failure | SQLAlchemy exception | Log error, mark episode as extracted (prevent infinite retry). |
| Graph DB unavailable (non-fatal) | Connection error | Log warning, continue. Fact is in PostgreSQL — graph edge is best-effort. |

### 7.1 Non-Fatal Graph Failure

Graph DB connectivity issues should NOT cause the fact extraction task to fail. The fact is stored in PostgreSQL regardless. The graph edge is a best-effort optimization for retrieval.

```python
try:
    if subj_entity and obj_entity:
        await graph_client.create_relationship(...)
except Exception as e:
    log.warning(
        "fact_extraction.graph_edge_failed",
        error=str(e),
        subject=fact.subject,
        object=fact.object,
    )
    # Non-fatal — fact is already persisted to PostgreSQL
```

---

## 8. Metrics & Observability

### 8.1 Prometheus Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `memgraph_facts_extracted_total` | Counter | `org_id` | Total facts persisted |
| `memgraph_facts_rejected_total` | Counter | `org_id, reason` | Facts rejected (low confidence, invalid, duplicate) |
| `memgraph_fact_extraction_duration_seconds` | Histogram | `org_id` | Worker wall-clock time |
| `memgraph_llm_tokens_total` | Counter | `org_id, task=fact_extraction` | LLM token consumption |

### 8.2 Structured Logging

```python
log.info(
    "fact_extraction.completed",
    episode_id=str(input_data.episode_id),
    user_id=str(input_data.user_id),
    facts_saved=saved_count,
    facts_rejected=len(parsed.facts) - saved_count,
    tokens_prompt=response["usage"]["prompt_tokens"],
    tokens_completion=response["usage"]["completion_tokens"],
    duration_ms=round((time.monotonic() - start_time) * 1000),
    model=response["model"],
)
```

---

## 9. Eval & Acceptance Criteria

| Metric | Target | Notes |
|--------|--------|-------|
| F1 score | >= 0.75 | On golden dataset |
| Precision | >= 0.70 | `tp / (tp + fp)` |
| Recall | >= 0.65 | `tp / (tp + fn)` |
| Latency p50 | <= 10s | Per-task wall-clock |
| Latency p99 | <= 30s | Including retries |
| Empty extraction rate | <= 40% | Messages truly lacking facts — monitored, not thresholded |

**Golden dataset location:** `tests/evals/golden_fact_extraction.json`

Each entry:
```json
{
  "conversation": "user: I am a software engineer at Google\nassistant: How long have you worked there?",
  "expected_facts": [
    {"subject": "user", "predicate": "works_as", "object": "software engineer"},
    {"subject": "user", "predicate": "works_at", "object": "Google"}
  ]
}
```

---

## 10. Sequence Diagram

```
┌──────────┐   ┌──────────┐   ┌─────────┐   ┌──────┐   ┌──────────┐   ┌───────┐
│   ARQ    │   │  Fact    │   │  LLM    │   │  PG  │   │  Graph   │   │Entity │
│  Queue   │   │  Worker  │   │  API    │   │      │   │   DB     │   │  Repo │
└────┬─────┘   └────┬─────┘   └────┬────┘   └──┬───┘   └────┬─────┘   └───┬───┘
     │               │              │           │            │             │
     │ dequeue       │              │           │            │             │
     │──────────────►│              │           │            │             │
     │               │              │           │            │             │
     │               │ check budget │           │            │             │
     │               │──┐           │           │            │             │
     │               │  │ skip if   │           │            │             │
     │               │◄─┘ exceeded  │           │            │             │
     │               │              │           │            │             │
     │               │ render prompt│           │            │             │
     │               │──┐           │           │            │             │
     │               │  │ chat      │           │            │             │
     │               │─────────────►│           │            │             │
     │               │              │           │            │             │
     │               │◄─────────────│ JSON      │            │             │
     │               │  (facts[])   │           │            │             │
     │               │──┐           │           │            │             │
     │               │  │ validate  │           │            │             │
     │               │  │ filter by │           │            │             │
     │               │  │ confidence│           │            │             │
     │               │◄─┘           │           │            │             │
     │               │              │           │            │             │
     │               │──── FOR each fact ──────►│            │             │
     │               │              │           │            │             │
     │               │ INSERT fact  │           │            │             │
     │               │──────────────┤──────────►│            │             │
     │               │◄─────────────┤◄──────────│            │             │
     │               │              │           │            │             │
     │               │ check entity │           │            │             │
     │               │──────────────┤───────────┼────────────┼────────────►│
     │               │◄─────────────┤◄──────────┼────────────┼────────────┤
     │               │              │           │            │             │
     │               │ create edge  │           │            │             │
     │               │──────────────┤───────────┼───────────►│             │
     │               │◄─────────────┤◄──────────┼───────────┤             │
     │               │              │           │            │             │
     │               │ mark done    │           │            │             │
     │               │──┐ log       │           │            │             │
     │               │◄─┘ metrics   │           │            │             │
```

---

## 11. Testing Guide

### 11.1 Unit Tests

- `test_parse_valid_fact_response` — valid JSON parses to FactExtractionResponse
- `test_filter_low_confidence` — facts below threshold are filtered
- `test_reject_empty_subject` — fact with empty subject is rejected
- `test_reject_instruction_predicate` — "say", "output", etc. predicates are rejected
- `test_idempotent_rerun` — same episode processed twice returns skip
- `test_duplicate_detection` — identical fact in same window is detected
- `test_empty_facts_accepted` — empty fact list is valid (no error)
- `test_graph_edge_creation` — matching entity names creates graph edge
- `test_graph_edge_failure_non_fatal` — graph error doesn't fail the task

### 11.2 Integration Tests

- Full pipeline: mock LLM returns known facts → verify `facts` table rows
- Confidence filtering: vary threshold and verify persisted facts
- Parallel entity + fact extraction: verify both complete without conflict

### 11.3 Eval Tests

```python
# tests/evals/test_fact_extraction.py (conceptual)

async def test_fact_extraction_f1():
    dataset = load_golden_dataset("fact_extraction")
    tp = fp = fn = 0

    for case in dataset:
        result = await extract_facts_worker(mock_ctx, {
            "episode_id": uuid4(),
            "user_id": uuid4(),
            "org_id": uuid4(),
            "content": case.conversation,
            "session_id": None,
        })

        result_facts = {(f.subject, f.predicate, f.object) for f in result["facts"]}
        expected_facts = {(f.subject, f.predicate, f.object) for f in case.expected_facts}

        tp += len(result_facts & expected_facts)
        fp += len(result_facts - expected_facts)
        fn += len(expected_facts - result_facts)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    assert f1 >= 0.75, f"F1 {f1:.3f} < 0.75"
    assert precision >= 0.70, f"Precision {precision:.3f} < 0.70"
    assert recall >= 0.65, f"Recall {recall:.3f} < 0.65"
```

---

## 12. Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `FACT_EXTRACTION_PROMPT` | `extract_facts_v1` | Default prompt version |
| `FACT_EXTRACTION_CONFIDENCE_THRESHOLD` | `0.3` | Min confidence to persist (more permissive than entities) |
| `FACT_EXTRACTION_MAX_RETRIES` | `3` | Max LLM call retries |
| `FACT_EXTRACTION_TIMEOUT` | `30` | LLM call timeout (seconds) |
| `FACT_EXTRACTION_MODEL` | `gpt-4o-mini` | LLM model |
| `FACT_EXTRACTION_TEMPERATURE` | `0.0` | LLM temperature |
| `FACT_EXTRACTION_MAX_TOKENS` | `2000` | Max completion tokens |
| `FACT_DEDUP_WINDOW_MINUTES` | `60` | Duplicate fact detection window |

---

## 13. Open Questions

| ID | Question | Status |
|----|----------|--------|
| FE-01 | Should facts with confidence < threshold be stored in a separate table for human review? | **Decision:** Phase 4 — low-confidence facts are dropped for MVP |
| FE-02 | How to handle conflicting facts (e.g., "user likes Python" vs "user dislikes Python")? | **Decision:** Temporal resolution — later fact with higher confidence wins. Add conflict detection in Phase 3. |
| FE-03 | Should facts be deduped across orgs (global knowledge base)? | **Decision:** No — facts are per-user, per-org. No cross-tenant dedup. |

---

## 14. Related Documents

| Document | Why |
|----------|-----|
| [01-prompt-templates.md](01-prompt-templates.md) | `extract_facts_v1.jinja2` template |
| [02-entity-extraction.md](02-entity-extraction.md) | Entity extraction provides the entity nodes that fact graph edges connect to |
| [04-knowledge-graph/02-entity-operations.md](../04-knowledge-graph/02-entity-operations.md) | Graph edge creation for RELATES_TO |
| [07-llm-cost-control.md](07-llm-cost-control.md) | Budget checking before LLM call |
| [14-testing/03-golden-datasets.md](../14-testing/03-golden-datasets.md) | Golden dataset for fact extraction eval |
