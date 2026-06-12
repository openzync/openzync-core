# Dialog Classification Worker

> **Phase:** 3 (NLP Enrichment) — P1 requirement
> **SRS References:** NLP-08, NLP-09, NLP-10, NLP-11, WRK-01, WRK-02
> **Index:** 5.4 — Read after 01-prompt-templates.md

---

## 1. Overview

The dialog classification worker analyzes each conversation turn (episode) and assigns:

- **Intent:** What the user is trying to do (question, complaint, purchase_intent, chitchat, greeting, farewell, etc.)
- **Emotion:** The affective state, split into:
  - **Valence:** positive / neutral / negative
  - **Arousal:** low / high

Classification labels are **configurable per organization**, enabling vertical-specific taxonomies (e.g., a support platform might define `escalation_request` as an intent, while an e-commerce platform might define `cart_abandonment`).

**Data flow:**

```
POST /memory → ARQ high queue → Classify Dialog Worker
                                    │
                             ┌──────▼──────┐
                             │  Few-shot    │
                             │  LLM call    │
                             └──────┬──────┘
                                    │ intent + emotion
                             ┌──────▼──────┐
                             │  Validate    │
                             │  against     │
                             │  label set   │
                             └──────┬──────┘
                                    │
                             ┌──────▼──────┐
                             │  Persist to  │
                             │  dialog_     │
                             │  classifica- │
                             │  tions table │
                             └─────────────┘
```

---

## 2. Worker Task Definition

### 2.1 Task Input

```python
# services/worker/tasks/classification.py

from dataclasses import dataclass
from uuid import UUID


@dataclass
class ClassificationInput:
    """Input for the dialog classification worker task.

    Runs after each session turn (episode) to classify the user's message.
    """
    episode_id: UUID
    session_id: UUID | None
    user_id: UUID
    org_id: UUID
    content: str
```

### 2.2 ARQ Task Handler

```python
# services/worker/tasks/classification.py (continued)

import structlog
from arq import Retry

from services.worker.prompts.renderer import PromptRenderer
from services.llm.client import LLMClient
from services.worker.repositories.classification_repo import ClassificationRepository

logger = structlog.get_logger(__name__)


async def classify_dialog(ctx: dict, task_input: dict) -> dict:
    """ARQ worker task: classify a dialog turn by intent and emotion.

    Triggered after message ingestion on the high priority queue.
    Uses few-shot classification with org-configurable label sets.

    Args:
        ctx: ARQ worker context
        task_input: Serialized ClassificationInput

    Returns:
        Summary dict with classification result.

    Raises:
        Retry: On transient failures.
    """
    input_data = ClassificationInput(**task_input)
    log = logger.bind(
        episode_id=str(input_data.episode_id),
        org_id=str(input_data.org_id),
    )

    # Idempotency guard
    repo: ClassificationRepository = ctx["classification_repo"]
    if await repo.has_classification(input_data.episode_id):
        log.info("classification.skipped.already_processed")
        return {"status": "skipped", "reason": "already_processed"}

    # Budget check
    cost_controller = ctx["cost_controller"]
    if not await cost_controller.check_budget(input_data.org_id, "classification"):
        log.warning("classification.skipped.budget_exceeded")
        return {"status": "skipped", "reason": "daily_budget_exceeded"}

    # Load org-specific label configuration
    labels = await repo.get_classification_labels(input_data.org_id)
    if labels is None:
        # Use default labels
        labels = {
            "intents": [
                "question", "complaint", "purchase_intent", "chitchat",
                "greeting", "farewell", "request", "feedback",
                "escalation", "neutral",
            ],
            "emotions": {
                "valence": ["positive", "neutral", "negative"],
                "arousal": ["low", "high"],
            },
        }

    # Load few-shot examples from org config or defaults
    examples = await repo.get_classification_examples(input_data.org_id)

    # Render prompt
    renderer: PromptRenderer = ctx["prompt_renderer"]
    config = await repo.get_extraction_config(input_data.org_id, "classification")
    prompt = renderer.render(
        "classify_dialog",
        version=config.get("prompt_version", 1),
        conversation_text=input_data.content,
        labels=labels,
        examples=examples,
        confidence_threshold=config.get("confidence_threshold", 0.5),
    )

    # Call LLM
    llm: LLMClient = ctx["llm_client"]
    max_retries = 2  # lower retry budget — classification is non-critical
    parsed = None

    for attempt in range(1, max_retries + 1):
        try:
            response = await llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=500,
                temperature=0.1,  # slight temperature for label diversity
            )
            raw_json = response["choices"][0]["message"]["content"]

            # Parse against schema
            from services.worker.schemas.classification import ClassificationResponse
            parsed = ClassificationResponse.model_validate_json(raw_json)

            # Validate intent is in allowed set
            if parsed.intent not in labels["intents"]:
                log.warning(
                    "classification.invalid_intent",
                    intent=parsed.intent,
                    allowed=labels["intents"],
                )
                parsed.intent = "neutral"  # fallback

            break

        except Exception as e:
            log.warning("classification.retry", attempt=attempt, error=str(e))
            if attempt == max_retries:
                log.error("classification.failed_all_retries", error=str(e))
                return {
                    "status": "fallback",
                    "intent": "neutral",
                    "emotion": {"valence": "neutral", "arousal": "low"},
                }

    if parsed is None:
        return {
            "status": "fallback",
            "intent": "neutral",
            "emotion": {"valence": "neutral", "arousal": "low"},
        }

    # Persist
    await repo.save_classification(
        episode_id=input_data.episode_id,
        intent=parsed.intent,
        emotion=parsed.emotion.label if parsed.emotion else None,
        valence=parsed.emotion.valence if parsed.emotion else "neutral",
        arousal=parsed.emotion.arousal if parsed.emotion else "low",
        raw={
            "intent": parsed.intent,
            "emotion": parsed.emotion.model_dump() if parsed.emotion else None,
            "confidence": parsed.confidence,
        },
    )

    log.info(
        "classification.completed",
        intent=parsed.intent,
        valence=parsed.emotion.valence if parsed.emotion else None,
        arousal=parsed.emotion.arousal if parsed.emotion else None,
        confidence=parsed.confidence,
        tokens_prompt=response.get("usage", {}).get("prompt_tokens"),
        tokens_completion=response.get("usage", {}).get("completion_tokens"),
    )

    return {
        "status": "completed",
        "intent": parsed.intent,
        "emotion": {
            "valence": parsed.emotion.valence if parsed.emotion else "neutral",
            "arousal": parsed.emotion.arousal if parsed.emotion else "low",
        },
        "confidence": parsed.confidence,
    }
```

---

## 3. Schema Definitions

### 3.1 Pydantic Response Schema

```python
# services/worker/schemas/classification.py

from pydantic import BaseModel, Field
from enum import Enum


class Valence(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class Arousal(str, Enum):
    LOW = "low"
    HIGH = "high"


class Emotion(BaseModel):
    """Emotional state classification."""
    valence: Valence = Field(..., description="Emotional valence")
    arousal: Arousal = Field(..., description="Emotional arousal level")
    label: str | None = Field(None, description="Free-text emotion label (e.g., 'frustrated', 'excited')")


class ClassificationResponse(BaseModel):
    """Validated LLM response for dialog classification."""
    intent: str = Field(..., description="Classified intent label")
    emotion: Emotion | None = Field(None, description="Emotional state")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
```

### 3.2 PostgreSQL Dialog Classifications Table

```sql
-- From SRS Section 7.1
CREATE TABLE dialog_classifications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    episode_id  UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    intent      TEXT,
    emotion     TEXT,
    valence     TEXT CHECK (valence IN ('positive','neutral','negative')),
    arousal     TEXT CHECK (arousal IN ('low','high')),
    raw         JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_dialog_classifications_episode ON dialog_classifications (episode_id);
CREATE INDEX idx_dialog_classifications_intent ON dialog_classifications (intent);
```

### 3.3 API Response Schema

```python
# services/api/schemas/classification.py

from pydantic import BaseModel
from uuid import UUID
from datetime import datetime


class ClassificationResponse(BaseModel):
    """Response returned by GET /users/{user_id}/sessions/{session_id}/classifications."""
    id: UUID
    episode_id: UUID
    intent: str | None
    emotion: str | None
    valence: str | None
    arousal: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ClassificationListResponse(BaseModel):
    """Paginated list of classifications for a session."""
    data: list[ClassificationResponse]
    next_cursor: str | None
    has_more: bool
```

---

## 4. Repository Layer

```python
# services/worker/repositories/classification_repo.py

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID


class ClassificationRepository:
    """Repository for dialog classification data."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def save_classification(
        self,
        episode_id: UUID,
        intent: str,
        emotion: str | None,
        valence: str,
        arousal: str,
        raw: dict,
    ) -> None:
        """Persist a classification result."""
        await self._db.execute(
            text("""
                INSERT INTO dialog_classifications
                    (episode_id, intent, emotion, valence, arousal, raw)
                VALUES
                    (:episode_id, :intent, :emotion, :valence, :arousal, :raw::jsonb)
            """),
            {
                "episode_id": episode_id,
                "intent": intent,
                "emotion": emotion,
                "valence": valence,
                "arousal": arousal,
                "raw": raw,
            },
        )
        await self._db.flush()

    async def has_classification(self, episode_id: UUID) -> bool:
        """Check if this episode was already classified."""
        result = await self._db.execute(
            text("""
                SELECT 1 FROM dialog_classifications
                WHERE episode_id = :episode_id
                LIMIT 1
            """),
            {"episode_id": episode_id},
        )
        return result.scalar_one_or_none() is not None

    async def get_classification_labels(self, org_id: UUID) -> dict | None:
        """Get org-specific classification labels.

        Labels are stored in entity_ontologies table for the
        'classification' task.
        """
        result = await self._db.execute(
            text("""
                SELECT labels FROM entity_ontologies
                WHERE organization_id = :org_id
                  AND task = 'classification'
            """),
            {"org_id": org_id},
        )
        row = result.scalar_one_or_none()
        return row if row else None

    async def get_classification_examples(self, org_id: UUID) -> list[dict]:
        """Get few-shot examples for classification.

        Returns:
            List of example dicts with 'text', 'intent', 'emotion' keys.
            Default examples are used when no org-specific ones exist.
        """
        result = await self._db.execute(
            text("""
                SELECT examples FROM entity_ontologies
                WHERE organization_id = :org_id
                  AND task = 'classification'
            """),
            {"org_id": org_id},
        )
        row = result.scalar_one_or_none()
        if row:
            return row  # examples stored as JSONB array
        return _DEFAULT_EXAMPLES  # defined below

    async def get_extraction_config(
        self, org_id: UUID, task: str
    ) -> dict:
        """Get per-org config for classification."""
        result = await self._db.execute(
            text("""
                SELECT prompt_template, confidence_threshold, prompt_version
                FROM entity_ontologies
                WHERE organization_id = :org_id AND task = :task
            """),
            {"org_id": org_id, "task": task},
        )
        row = result.one_or_none()
        if row:
            return {
                "prompt_template": row[0],
                "confidence_threshold": row[1] or 0.5,
                "prompt_version": row[2] or 1,
            }
        return {
            "prompt_template": None,
            "confidence_threshold": 0.5,
            "prompt_version": 1,
        }

    async def get_classifications_by_session(
        self, session_id: UUID, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[dict], str | None]:
        """Get classifications for all episodes in a session (paginated).

        Used by the GET /users/{user_id}/sessions/{session_id}/classifications endpoint.

        Returns:
            Tuple of (results list, next_cursor string or None).
        """
        # Implementation uses cursor-based pagination
        # See api-gateway/03-pagination.md for pattern
        ...


# Default few-shot examples
_DEFAULT_EXAMPLES = [
    {
        "text": "Hi there!",
        "intent": "greeting",
        "emotion": {"valence": "positive", "arousal": "low"},
    },
    {
        "text": "I need help with my billing",
        "intent": "complaint",
        "emotion": {"valence": "negative", "arousal": "high"},
    },
    {
        "text": "What is the weather like today?",
        "intent": "question",
        "emotion": {"valence": "neutral", "arousal": "low"},
    },
    {
        "text": "I'd like to buy the Pro plan",
        "intent": "purchase_intent",
        "emotion": {"valence": "positive", "arousal": "high"},
    },
    {
        "text": "Goodbye, thanks for your help!",
        "intent": "farewell",
        "emotion": {"valence": "positive", "arousal": "low"},
    },
    {
        "text": "This is ridiculous, I want a refund",
        "intent": "complaint",
        "emotion": {"valence": "negative", "arousal": "high"},
    },
    {
        "text": "My order still hasn't arrived",
        "intent": "question",
        "emotion": {"valence": "negative", "arousal": "high"},
    },
    {
        "text": "I was just thinking about something else",
        "intent": "chitchat",
        "emotion": {"valence": "neutral", "arousal": "low"},
    },
]
```

---

## 5. Router Layer (API Endpoint)

```python
# services/api/routers/classification.py

from fastapi import APIRouter, Depends, Query
from uuid import UUID

from services.api.dependencies import get_current_user
from services.api.schemas.classification import (
    ClassificationListResponse,
    ClassificationResponse,
)
from services.api.repositories.classification_repo import APIClassificationRepository

router = APIRouter(prefix="/v1/users/{user_id}", tags=["classifications"])


@router.get(
    "/sessions/{session_id}/classifications",
    response_model=ClassificationListResponse,
)
async def get_classifications(
    user_id: UUID,
    session_id: UUID,
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    repo: APIClassificationRepository = Depends(),
    current_user=Depends(get_current_user),
) -> ClassificationListResponse:
    """Get dialog classifications for a session.

    Returns a paginated list of per-episode classifications including
    intent, emotion valence, and arousal for each message in the session.
    """
    results, next_cursor = await repo.get_classifications_by_session(
        session_id=session_id,
        cursor=cursor,
        limit=limit,
    )
    return ClassificationListResponse(
        data=[ClassificationResponse(**r) for r in results],
        next_cursor=next_cursor,
        has_more=next_cursor is not None,
    )
```

---

## 6. Org-Configurable Label Set

### 6.1 Storage

Classification labels are stored in the `entity_ontologies` table:

```sql
-- Example: org_abc configures custom labels for a support platform
INSERT INTO entity_ontologies
    (organization_id, task, labels, examples)
VALUES
    (
        'org_abc_uuid',
        'classification',
        '{
            "intents": [
                "greeting", "question", "bug_report", "feature_request",
                "escalation", "complaint", "chitchat", "farewell",
                "onboarding_help", "account_issue", "pricing_inquiry",
                "cancellation", "neutral"
            ],
            "emotions": {
                "valence": ["positive", "neutral", "negative"],
                "arousal": ["low", "high"]
            }
        }'::jsonb,
        '[... examples ...]'::jsonb
    );
```

### 6.2 Default Label Set (Fallback)

When no org-specific labels are configured, the system uses:

```python
DEFAULT_LABELS = {
    "intents": [
        "question", "complaint", "purchase_intent", "chitchat",
        "greeting", "farewell", "request", "feedback",
        "escalation", "neutral",
    ],
    "emotions": {
        "valence": ["positive", "neutral", "negative"],
        "arousal": ["low", "high"],
    },
}
```

---

## 7. Error Handling & Fallback

| Failure Mode | Detection | Action |
|-------------|-----------|--------|
| LLM timeout | `asyncio.TimeoutError` | Retry 2x (lower budget — classification is non-critical). On failure, return fallback neutral. |
| Invalid intent label | Label not in allowed set | Log warning, coerce to `"neutral"` |
| Invalid JSON | `pydantic.ValidationError` | Retry with recovery prompt. On failure, return fallback. |
| DB insert failure | SQLAlchemy exception | Log error — classification is non-critical, don't retry. |

**Fallback classification** (used when LLM is unavailable):
```python
FALLBACK = {
    "intent": "neutral",
    "emotion": {"valence": "neutral", "arousal": "low"},
    "confidence": 0.0,
}
```

---

## 8. Metrics & Observability

### 8.1 Prometheus Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `openzep_classifications_total` | Counter | `org_id, intent` | Total classifications by intent |
| `openzep_classification_fallbacks_total` | Counter | `org_id` | Times fallback neutral was used |
| `openzep_classification_duration_seconds` | Histogram | — | Worker wall-clock time |
| `openzep_llm_tokens_total` | Counter | `org_id, task=classification` | LLM token consumption |

### 8.2 Structured Logging

```python
log.info(
    "classification.completed",
    episode_id=str(input_data.episode_id),
    intent=parsed.intent,
    valence=parsed.emotion.valence if parsed.emotion else None,
    arousal=parsed.emotion.arousal if parsed.emotion else None,
    confidence=parsed.confidence,
    duration_ms=round((time.monotonic() - start_time) * 1000),
)
```

---

## 9. Eval & Acceptance Criteria

| Metric | Target | Notes |
|--------|--------|-------|
| Intent classification accuracy | >= 0.85 | Correct label out of allowed set |
| Emotion F1 | >= 0.80 | Macro F1 across valence classes |
| Arousal F1 | >= 0.70 | Binary (low/high) |
| Fallback rate | <= 5% | Percentage of classifications using neutral fallback |

**Golden dataset location:** `tests/evals/golden_dialog_classification.json`

Each entry:
```json
{
  "text": "I want to speak to a manager right now!",
  "expected": {
    "intent": "complaint",
    "emotion": {"valence": "negative", "arousal": "high"}
  }
}
```

---

## 10. Sequence Diagram

```
┌──────────┐   ┌────────────┐   ┌──────────┐   ┌──────┐
│   ARQ    │   │  Classify  │   │   LLM    │   │  PG  │
│  Queue   │   │  Worker    │   │   API    │   │      │
└────┬─────┘   └─────┬──────┘   └────┬─────┘   └──┬───┘
     │                │               │            │
     │ dequeue        │               │            │
     │───────────────►│               │            │
     │                │               │            │
     │                │ check budget  │            │
     │                │──┐            │            │
     │                │  │ skip if    │            │
     │                │◄─┘ exceeded   │            │
     │                │               │            │
     │                │ load org      │            │
     │                │ labels +      │            │
     │                │ examples      │            │
     │                │───────┐       │            │
     │                │       │       │            │
     │                │ render prompt│            │
     │                │───────┘       │            │
     │                │               │            │
     │                │ chat          │            │
     │                │──────────────►│            │
     │                │               │            │
     │                │◄──────────────│ JSON       │
     │                │  (intent,     │            │
     │                │   emotion)    │            │
     │                │               │            │
     │                │ validate      │            │
     │                │ intent in     │            │
     │                │ allowed set   │            │
     │                │──┐            │            │
     │                │  │ if invalid │            │
     │                │  │ → fallback │            │
     │                │  │ to neutral │            │
     │                │◄─┘            │            │
     │                │               │            │
     │                │ INSERT into   │            │
     │                │ dialog_       │            │
     │                │ classifications│           │
     │                │───────────────┤──────────►│
     │                │◄──────────────┤◄──────────│
     │                │               │            │
     │                │ log + respond │            │
```

---

## 11. Testing Guide

### 11.1 Unit Tests

- `test_parse_valid_classification` — valid JSON parses correctly
- `test_invalid_intent_fallback` — unrecognized intent coereced to neutral
- `test_missing_emotion_fallback` — null emotion returns neutral/low
- `test_org_label_override` — org-specific labels override defaults
- `test_idempotent_rerun` — same episode processed twice returns skip
- `test_fallback_on_llm_failure` — LLM failure returns neutral fallback

### 11.2 Integration Tests

- Full pipeline: mock LLM → verify classification in `dialog_classifications` table
- Multiple classifications for a session → verify paginated listing
- Org with custom label set → verify labels are injected into prompt

### 11.3 Eval Tests

```python
async def test_classification_accuracy():
    dataset = load_golden_dataset("classification")
    correct = total = 0

    for case in dataset:
        result = await classify_dialog(mock_ctx, {
            "episode_id": uuid4(),
            "session_id": uuid4(),
            "user_id": uuid4(),
            "org_id": uuid4(),
            "content": case.text,
        })
        if result.get("intent") == case.expected.intent:
            correct += 1
        total += 1

    accuracy = correct / total if total > 0 else 0
    assert accuracy >= 0.85, f"Accuracy {accuracy:.3f} < 0.85"
```

---

## 12. Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CLASSIFICATION_PROMPT` | `classify_dialog_v1` | Default prompt version |
| `CLASSIFICATION_CONFIDENCE_THRESHOLD` | `0.5` | Min confidence |
| `CLASSIFICATION_MAX_RETRIES` | `2` | Max LLM call retries (lower — non-critical) |
| `CLASSIFICATION_TIMEOUT` | `15` | LLM call timeout (seconds) — shorter than extraction |
| `CLASSIFICATION_MODEL` | `gpt-4o-mini` | LLM model |
| `CLASSIFICATION_TEMPERATURE` | `0.1` | Slight temperature for label diversity |
| `CLASSIFICATION_MAX_TOKENS` | `500` | Max completion tokens (classification is short) |
| `CLASSIFICATION_DEFAULT_INTENTS` | see §6.2 | Default intent label set |
| `CLASSIFICATION_ENABLE` | `true` | Enable/disable classification per-org override |

---

## 13. Open Questions

| ID | Question | Status |
|----|----------|--------|
| DC-01 | Should we support multi-label intent (a message can have multiple intents)? | **Decision:** Phase 3 — single label for MVP. Multi-label adds eval complexity. |
| DC-02 | Should we run classification on assistant messages too, or only user messages? | **Decision:** User messages only. Assistant responses are not emotionally meaningful. |
| DC-03 | How to handle streaming classification (real-time emotion detection)? | Deferred to Phase 4 — requires WebSocket support. |

---

## 14. Related Documents

| Document | Why |
|----------|-----|
| [01-prompt-templates.md](01-prompt-templates.md) | `classify_dialog_v1.jinja2` template definition |
| [03-core-memory/01-message-ingestion.md](../03-core-memory/01-message-ingestion.md) | Ingestion triggers the classification worker |
| [07-user-session-mgmt/02-session-crud.md](../07-user-session-mgmt/02-session-crud.md) | Session CRUD that the classification API depends on |
| [07-llm-cost-control.md](07-llm-cost-control.md) | Budget checking before LLM call |
| [14-testing/03-golden-datasets.md](../14-testing/03-golden-datasets.md) | Golden dataset for classification eval |
