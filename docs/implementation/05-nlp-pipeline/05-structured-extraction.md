# Structured Data Extraction Worker

> **Phase:** 3 (NLP Enrichment) — P1 requirement
> **SRS References:** NLP-12, NLP-13, NLP-14
> **Index:** 5.5 — Read after 01-prompt-templates.md

---

## 1. Overview

Structured data extraction allows organizations to define arbitrary JSON Schemas and have the LLM extract matching data from conversations. This enables vertical-specific use cases like:

- **Support:** Extract `{issue_type, severity, customer_tier, resolution_time}`
- **E-commerce:** Extract `{product_name, quantity, price, abandoned_cart: bool}`
- **Healthcare:** Extract `{symptoms[], diagnosis, medications[], follow_up_date}`
- **HR:** Extract `{employee_id, request_type, urgent: bool, manager_approval_needed: bool}`

Unlike entity and fact extraction (which run per-message), structured extraction runs **after session close** — the entire conversation is available for analysis.

**Data flow:**

```
Session closed (closed_at set) → ARQ high queue → Structured Extraction Worker
                                                    │
                                             ┌──────▼──────┐
                                             │  Schema CRUD │
                                             │  API (admin) │
                                             └──────┬──────┘
                                                    │ schema definition
                                             ┌──────▼──────┐
                                             │  Load schema │
                                             │  + session   │
                                             │  messages    │
                                             └──────┬──────┘
                                             ┌──────▼──────┐
                                             │  LLM: schema-│
                                             │  guided      │
                                             │  extraction  │
                                             └──────┬──────┘
                                                     │ JSON matching schema
                                             ┌──────▼──────┐
                                             │  Validate    │
                                             │  against     │
                                             │  JSON Schema │
                                             └──────┬──────┘
                                                     │
                                             ┌──────▼──────┐
                                             │  Persist to  │
                                             │  structured_ │
                                             │  extractions │
                                             └─────────────┘
```

---

## 2. Schema CRUD API (Admin)

### 2.1 Data Model

```sql
CREATE TABLE extraction_schemas (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    json_schema     JSONB NOT NULL,
    prompt_template TEXT,               -- optional custom prompt override
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, name)
);

-- Each organization can have multiple extraction schemas
CREATE INDEX idx_extraction_schemas_org ON extraction_schemas (organization_id);
```

### 2.2 Pydantic Schemas

```python
# services/api/schemas/extraction_schema.py

from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime
from typing import Any


class ExtractionSchemaCreate(BaseModel):
    """Request body for POST /v1/admin/schemas."""
    name: str = Field(..., min_length=1, max_length=100, description="Unique schema name within org")
    description: str | None = Field(None, max_length=500)
    json_schema: dict[str, Any] = Field(
        ...,
        description="Valid JSON Schema (draft-07) describing the expected output",
    )
    prompt_template: str | None = Field(
        None,
        description="Optional custom prompt template. Uses default if omitted.",
    )


class ExtractionSchemaUpdate(BaseModel):
    """Request body for PUT /v1/admin/schemas/{schema_id}."""
    name: str | None = None
    description: str | None = None
    json_schema: dict[str, Any] | None = None
    prompt_template: str | None = None


class ExtractionSchemaResponse(BaseModel):
    """Response for extraction schema CRUD operations."""
    id: UUID
    name: str
    description: str
    json_schema: dict[str, Any]
    prompt_template: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
```

### 2.3 API Endpoints

```python
# services/api/routers/admin_schemas.py

from fastapi import APIRouter, Depends, HTTPException, Query, status
from uuid import UUID

from services.api.dependencies import get_admin_user, get_db_session
from services.api.repositories.extraction_schema_repo import ExtractionSchemaRepository
from services.api.schemas.extraction_schema import (
    ExtractionSchemaCreate,
    ExtractionSchemaUpdate,
    ExtractionSchemaResponse,
)

router = APIRouter(prefix="/v1/admin", tags=["admin-schemas"], dependencies=[Depends(get_admin_user)])


@router.post("/schemas", response_model=ExtractionSchemaResponse, status_code=status.HTTP_201_CREATED)
async def create_schema(
    payload: ExtractionSchemaCreate,
    repo: ExtractionSchemaRepository = Depends(),
    admin=Depends(get_admin_user),
) -> ExtractionSchemaResponse:
    """Create a new extraction schema for the admin's organization.

    The json_schema field must be a valid JSON Schema (draft-07).
    Schemas are scoped to the admin's organization — name must be unique within org.
    """
    try:
        schema = await repo.create_schema(
            org_id=admin.org_id,
            name=payload.name,
            description=payload.description or "",
            json_schema=payload.json_schema,
            prompt_template=payload.prompt_template,
        )
        return ExtractionSchemaResponse.model_validate(schema)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))


@router.get("/schemas", response_model=list[ExtractionSchemaResponse])
async def list_schemas(
    repo: ExtractionSchemaRepository = Depends(),
    admin=Depends(get_admin_user),
) -> list[ExtractionSchemaResponse]:
    """List all extraction schemas for the admin's organization."""
    schemas = await repo.list_schemas(org_id=admin.org_id)
    return [ExtractionSchemaResponse.model_validate(s) for s in schemas]


@router.get("/schemas/{schema_id}", response_model=ExtractionSchemaResponse)
async def get_schema(
    schema_id: UUID,
    repo: ExtractionSchemaRepository = Depends(),
    admin=Depends(get_admin_user),
) -> ExtractionSchemaResponse:
    """Get a single extraction schema by ID."""
    schema = await repo.get_schema(org_id=admin.org_id, schema_id=schema_id)
    if schema is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema not found")
    return ExtractionSchemaResponse.model_validate(schema)


@router.put("/schemas/{schema_id}", response_model=ExtractionSchemaResponse)
async def update_schema(
    schema_id: UUID,
    payload: ExtractionSchemaUpdate,
    repo: ExtractionSchemaRepository = Depends(),
    admin=Depends(get_admin_user),
) -> ExtractionSchemaResponse:
    """Update an existing extraction schema."""
    schema = await repo.update_schema(
        org_id=admin.org_id,
        schema_id=schema_id,
        **payload.model_dump(exclude_none=True),
    )
    if schema is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema not found")
    return ExtractionSchemaResponse.model_validate(schema)


@router.delete("/schemas/{schema_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schema(
    schema_id: UUID,
    repo: ExtractionSchemaRepository = Depends(),
    admin=Depends(get_admin_user),
) -> None:
    """Delete an extraction schema. Existing extractions using this schema are preserved."""
    deleted = await repo.delete_schema(org_id=admin.org_id, schema_id=schema_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schema not found")
```

---

## 3. Worker Task Definition

### 3.1 Task Input

```python
# services/worker/tasks/structured_extraction.py

from dataclasses import dataclass
from uuid import UUID


@dataclass
class StructuredExtractionInput:
    """Input for the structured data extraction worker task.

    Triggered after session close. The entire conversation is passed
    for analysis against the org's JSON schema.
    """
    session_id: UUID
    org_id: UUID
    user_id: UUID
    schema_id: UUID
    messages: list[dict]  # [{role, content, created_at}]
```

### 3.2 ARQ Task Handler

```python
# services/worker/tasks/structured_extraction.py (continued)

import structlog
import json
from arq import Retry

from services.worker.prompts.renderer import PromptRenderer
from services.llm.client import LLMClient
from services.worker.repositories.structured_extraction_repo import StructuredExtractionRepository

logger = structlog.get_logger(__name__)


async def extract_structured(ctx: dict, task_input: dict) -> dict:
    """ARQ worker task: extract structured data matching an org-defined JSON Schema.

    Triggered after session close on the high priority queue.
    Processes the entire conversation against the org's schema.

    Args:
        ctx: ARQ worker context
        task_input: Serialized StructuredExtractionInput

    Returns:
        Summary dict with extraction status.

    Raises:
        Retry: On transient failures.
    """
    input_data = StructuredExtractionInput(**task_input)
    log = logger.bind(
        session_id=str(input_data.session_id),
        org_id=str(input_data.org_id),
        schema_id=str(input_data.schema_id),
    )

    repo: StructuredExtractionRepository = ctx["structured_extraction_repo"]

    # Idempotency guard
    existing = await repo.get_extraction(input_data.session_id, input_data.schema_id)
    if existing:
        log.info("structured_extraction.skipped.already_processed")
        return {"status": "skipped", "reason": "already_processed"}

    # Budget check
    cost_controller = ctx["cost_controller"]
    if not await cost_controller.check_budget(input_data.org_id, "structured_extraction"):
        log.warning("structured_extraction.skipped.budget_exceeded")
        return {"status": "skipped", "reason": "daily_budget_exceeded"}

    # Load the schema definition
    schema = await repo.get_schema(input_data.org_id, input_data.schema_id)
    if schema is None:
        log.error("structured_extraction.schema_not_found")
        return {"status": "failed", "reason": "schema_not_found"}

    # Load per-org extraction config
    config = await repo.get_extraction_config(input_data.org_id, "structured_extraction")

    # Serialize the conversation
    conversation_text = "\n".join(
        f"{m['role']}: {m['content']}" for m in input_data.messages
    )

    # Render prompt
    renderer: PromptRenderer = ctx["prompt_renderer"]
    prompt = renderer.render(
        "extract_structured",
        version=config.get("prompt_version", 1),
        schema=schema["json_schema"],
        schema_description=schema.get("description", ""),
        conversation=conversation_text,
        custom_prompt=schema.get("prompt_template"),
    )

    # Call LLM with schema validation retry
    llm: LLMClient = ctx["llm_client"]
    max_retries = 3
    parsed_data = None
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            response = await llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=4000,
                temperature=0.0,
            )
            raw_json = response["choices"][0]["message"]["content"]
            extracted = json.loads(raw_json)

            # Validate against JSON Schema
            _validate_json_schema(extracted, schema["json_schema"])

            parsed_data = extracted
            break

        except json.JSONDecodeError as e:
            last_error = e
            log.warning("structured_extraction.json_parse_failed", attempt=attempt)

        except ValueError as e:
            last_error = e
            log.warning(
                "structured_extraction.schema_validation_failed",
                attempt=attempt,
                error=str(e),
            )

        if attempt < max_retries:
            # Append validation error to prompt for retry
            prompt += (
                f"\n\nPREVIOUS ATTEMPT FAILED:\n"
                f"The previous response did not match the expected schema.\n"
                f"Error: {last_error}\n"
                f"Please ensure the JSON response matches exactly the schema provided above."
            )

    if parsed_data is None:
        log.error("structured_extraction.failed_all_retries", error=str(last_error))
        return {"status": "failed", "reason": "schema_validation_failed"}

    # Persist
    await repo.save_extraction(
        session_id=input_data.session_id,
        schema_id=input_data.schema_id,
        data=parsed_data,
    )

    # Record usage
    await cost_controller.record_usage(
        org_id=input_data.org_id,
        model=response["model"],
        prompt_tokens=response["usage"]["prompt_tokens"],
        completion_tokens=response["usage"]["completion_tokens"],
        task_type="structured_extraction",
        session_id=input_data.session_id,
    )

    log.info(
        "structured_extraction.completed",
        keys=list(parsed_data.keys()),
        tokens_prompt=response.get("usage", {}).get("prompt_tokens"),
        tokens_completion=response.get("usage", {}).get("completion_tokens"),
    )

    return {"status": "completed", "extracted_keys": list(parsed_data.keys())}


def _validate_json_schema(data: dict, schema: dict) -> None:
    """Validate extracted data against the JSON Schema.

    Uses the `jsonschema` library for validation. Raises ValueError
    on validation failure with details about what didn't match.

    Args:
        data: The extracted JSON data.
        schema: JSON Schema (draft-07) to validate against.

    Raises:
        ValueError: If validation fails, with a description of the error.
    """
    import jsonschema

    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as e:
        raise ValueError(f"Schema validation failed: {e.message}") from e
```

---

## 4. Repository Layer

### 4.1 Schema CRUD Repository

```python
# services/api/repositories/extraction_schema_repo.py

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID


class ExtractionSchemaRepository:
    """Repository for extraction schema CRUD."""

    def __init__(self, db: AsyncSession = Depends(get_db_session)) -> None:
        self._db = db

    async def create_schema(
        self,
        org_id: UUID,
        name: str,
        description: str,
        json_schema: dict,
        prompt_template: str | None = None,
    ) -> dict:
        """Create a new extraction schema.

        Raises ValueError if a schema with the same name exists in the org.
        """
        # Check for duplicate name
        existing = await self._db.execute(
            text("SELECT 1 FROM extraction_schemas WHERE organization_id = :org_id AND name = :name"),
            {"org_id": org_id, "name": name},
        )
        if existing.scalar_one_or_none():
            raise ValueError(f"Schema '{name}' already exists in this organization")

        result = await self._db.execute(
            text("""
                INSERT INTO extraction_schemas
                    (organization_id, name, description, json_schema, prompt_template)
                VALUES
                    (:org_id, :name, :description, :json_schema::jsonb, :prompt_template)
                RETURNING id, name, description, json_schema, prompt_template,
                          created_at, updated_at
            """),
            {
                "org_id": org_id,
                "name": name,
                "description": description,
                "json_schema": json.dumps(json_schema),
                "prompt_template": prompt_template,
            },
        )
        await self._db.commit()
        return result.one()._asdict()

    async def list_schemas(self, org_id: UUID) -> list[dict]:
        """List all schemas for an organization."""
        result = await self._db.execute(
            text("""
                SELECT id, name, description, json_schema, prompt_template,
                       created_at, updated_at
                FROM extraction_schemas
                WHERE organization_id = :org_id
                ORDER BY created_at DESC
            """),
            {"org_id": org_id},
        )
        return [row._asdict() for row in result.all()]

    async def get_schema(self, org_id: UUID, schema_id: UUID) -> dict | None:
        """Get a single schema by ID."""
        result = await self._db.execute(
            text("""
                SELECT id, name, description, json_schema, prompt_template,
                       created_at, updated_at
                FROM extraction_schemas
                WHERE organization_id = :org_id AND id = :schema_id
            """),
            {"org_id": org_id, "schema_id": schema_id},
        )
        row = result.one_or_none()
        return row._asdict() if row else None

    async def update_schema(
        self, org_id: UUID, schema_id: UUID, **updates
    ) -> dict | None:
        """Update fields on an existing schema."""
        set_clauses = []
        params = {"org_id": org_id, "schema_id": schema_id}

        for key, value in updates.items():
            if value is not None:
                if key == "json_schema":
                    set_clauses.append(f"{key} = :{key}::jsonb")
                    params[key] = json.dumps(value)
                else:
                    set_clauses.append(f"{key} = :{key}")
                    params[key] = value

        if not set_clauses:
            return await self.get_schema(org_id, schema_id)

        set_clauses.append("updated_at = now()")

        result = await self._db.execute(
            text(f"""
                UPDATE extraction_schemas
                SET {', '.join(set_clauses)}
                WHERE organization_id = :org_id AND id = :schema_id
                RETURNING id, name, description, json_schema, prompt_template,
                          created_at, updated_at
            """),
            params,
        )
        await self._db.commit()
        row = result.one_or_none()
        return row._asdict() if row else None

    async def delete_schema(self, org_id: UUID, schema_id: UUID) -> bool:
        """Delete a schema. Returns True if deleted."""
        result = await self._db.execute(
            text("""
                DELETE FROM extraction_schemas
                WHERE organization_id = :org_id AND id = :schema_id
                RETURNING id
            """),
            {"org_id": org_id, "schema_id": schema_id},
        )
        await self._db.commit()
        return result.scalar_one_or_none() is not None
```

### 4.2 Extraction Results Repository

```python
# services/worker/repositories/structured_extraction_repo.py

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID


class StructuredExtractionRepository:
    """Repository for structured extraction results."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def save_extraction(
        self,
        session_id: UUID,
        schema_id: UUID,
        data: dict,
    ) -> None:
        """Persist a structured extraction result."""
        await self._db.execute(
            text("""
                INSERT INTO structured_extractions
                    (session_id, schema_id, data)
                VALUES
                    (:session_id, :schema_id, :data::jsonb)
            """),
            {
                "session_id": session_id,
                "schema_id": schema_id,
                "data": json.dumps(data),
            },
        )
        await self._db.flush()

    async def get_extraction(
        self,
        session_id: UUID,
        schema_id: UUID,
    ) -> dict | None:
        """Check for existing extraction (idempotency guard)."""
        result = await self._db.execute(
            text("""
                SELECT id, data, created_at
                FROM structured_extractions
                WHERE session_id = :session_id AND schema_id = :schema_id
                LIMIT 1
            """),
            {"session_id": session_id, "schema_id": schema_id},
        )
        row = result.one_or_none()
        return row._asdict() if row else None

    async def get_schema(self, org_id: UUID, schema_id: UUID) -> dict | None:
        """Get the schema definition for extraction."""
        result = await self._db.execute(
            text("""
                SELECT id, name, json_schema, description, prompt_template
                FROM extraction_schemas
                WHERE organization_id = :org_id AND id = :schema_id
            """),
            {"org_id": org_id, "schema_id": schema_id},
        )
        row = result.one_or_none()
        if row is None:
            return None
        return {
            "id": row[0],
            "name": row[1],
            "json_schema": row[2],
            "description": row[3],
            "prompt_template": row[4],
        }

    async def get_extraction_config(
        self, org_id: UUID, task: str
    ) -> dict:
        """Get per-org config for structured extraction."""
        result = await self._db.execute(
            text("""
                SELECT prompt_version
                FROM entity_ontologies
                WHERE organization_id = :org_id AND task = :task
            """),
            {"org_id": org_id, "task": task},
        )
        row = result.one_or_none()
        if row:
            return {"prompt_version": row[0] or 1}
        return {"prompt_version": 1}
```

---

## 5. Query API Endpoint

```python
# services/api/routers/structured_extraction.py

from fastapi import APIRouter, Depends, HTTPException, status
from uuid import UUID

from services.api.dependencies import get_current_user, get_db_session
from services.api.repositories.structured_extraction_repo import StructuredExtractionAPIRepository

router = APIRouter(prefix="/v1/users/{user_id}", tags=["structured-extraction"])


@router.get("/sessions/{session_id}/extract")
async def get_structured_extraction(
    user_id: UUID,
    session_id: UUID,
    schema_name: str | None = None,
    repo: StructuredExtractionAPIRepository = Depends(),
    current_user=Depends(get_current_user),
) -> dict:
    """Get structured extraction results for a session.

    Args:
        user_id: The user owning the session.
        session_id: The session to retrieve extraction for.
        schema_name: Optional — filter by schema name. If omitted,
                     returns the most recent extraction.

    Returns:
        The extracted data dict (shape depends on the org's schema).

    Raises:
        404: If no extraction exists for this session.
    """
    extraction = await repo.get_session_extraction(
        user_id=user_id,
        session_id=session_id,
        schema_name=schema_name,
    )
    if extraction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No structured extraction found for this session",
        )
    return extraction["data"]
```

---

## 6. Session Close Trigger

The structured extraction worker is triggered when a session's `closed_at` is set. This happens either:

1. **Explicitly:** `POST /v1/users/{user_id}/sessions/{session_id}/close` endpoint
2. **Implicitly:** Session auto-close after a configurable inactivity timeout (default: 30 minutes)

```python
# services/worker/tasks/structured_extraction.py

async def on_session_close(ctx: dict, session_id: UUID, org_id: UUID, user_id: UUID) -> None:
    """Hook called when a session is closed.

    Enqueues structured extraction tasks for all active schemas
    associated with the organization.

    Args:
        ctx: ARQ worker context
        session_id: The closed session
        org_id: Organization scope
        user_id: Session owner
    """
    repo: StructuredExtractionRepository = ctx["structured_extraction_repo"]

    # Get all schemas for this org
    schemas = await repo.list_org_schemas(org_id)

    if not schemas:
        logger.info("structured_extraction.no_schemas_configured", org_id=str(org_id))
        return

    # Get all messages for this session
    messages = await repo.get_session_messages(session_id)

    if not messages:
        logger.info("structured_extraction.empty_session", session_id=str(session_id))
        return

    # Enqueue one task per schema
    redis = ctx["redis"]
    for schema in schemas:
        job = await redis.enqueue_job(
            "extract_structured",
            {
                "session_id": str(session_id),
                "org_id": str(org_id),
                "user_id": str(user_id),
                "schema_id": str(schema["id"]),
                "messages": messages,
            },
            _queue_name="high",
        )
        logger.info(
            "structured_extraction.enqueued",
            schema_name=schema["name"],
            job_id=job.id,
        )
```

---

## 7. Example: Schema Definition & Extraction

### 7.1 Defining a Schema

```json
POST /v1/admin/schemas
Authorization: Bearer mg_live_...

{
  "name": "product_feedback",
  "description": "Extract product feedback and feature requests from support conversations",
  "json_schema": {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["sentiment", "feedback_items", "action_items"],
    "properties": {
      "sentiment": {
        "type": "string",
        "enum": ["positive", "neutral", "negative"],
        "description": "Overall sentiment of the conversation"
      },
      "feedback_items": {
        "type": "array",
        "items": {
          "type": "object",
          "required": ["topic", "feedback_type", "description"],
          "properties": {
            "topic": {"type": "string"},
            "feedback_type": {"type": "string", "enum": ["praise", "complaint", "suggestion", "question"]},
            "description": {"type": "string"},
            "priority": {"type": "integer", "minimum": 1, "maximum": 5}
          }
        }
      },
      "action_items": {
        "type": "array",
        "items": {
          "type": "object",
          "required": ["action", "owner"],
          "properties": {
            "action": {"type": "string"},
            "owner": {"type": "string"},
            "due_date": {"type": "string", "format": "date"}
          }
        }
      },
      "customer_tier": {
        "type": "string",
        "enum": ["free", "pro", "enterprise"],
        "description": "Customer tier if mentioned"
      }
    }
  }
}
```

### 7.2 Extraction Result

After a session is closed:

```json
GET /v1/users/user_abc/sessions/session_xyz/extract
Authorization: Bearer mg_live_...

{
  "sentiment": "negative",
  "feedback_items": [
    {
      "topic": "billing",
      "feedback_type": "complaint",
      "description": "Customer was charged twice for the same month",
      "priority": 4
    },
    {
      "topic": "user interface",
      "feedback_type": "suggestion",
      "description": "Customer wants a dark mode option",
      "priority": 2
    }
  ],
  "action_items": [
    {
      "action": "Process refund for duplicate charge",
      "owner": "billing_team",
      "due_date": "2026-06-06"
    }
  ],
  "customer_tier": "pro"
}
```

---

## 8. Error Handling

| Failure Mode | Detection | Action |
|-------------|-----------|--------|
| Schema not found | DB query returns None | Log error, return `failed` — no retry |
| LLM timeout | `asyncio.TimeoutError` | Retry 3x with exponential backoff |
| JSON parse error | `json.JSONDecodeError` | Retry with parse error appended to prompt |
| Schema validation failure | `jsonschema.ValidationError` | Retry with validation error in prompt |
| Empty session (no messages) | Messages list is empty | Log info, return `skipped` — nothing to extract |
| DB insert failure | SQLAlchemy exception | Log error, mark for retry |

---

## 9. Metrics & Observability

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `memgraph_structured_extractions_total` | Counter | `org_id, schema_name` | Total extractions by schema |
| `memgraph_structured_extraction_failures_total` | Counter | `org_id, schema_name, reason` | Failed extractions |
| `memgraph_structured_extraction_duration_seconds` | Histogram | `org_id` | Worker wall-clock time |
| `memgraph_llm_tokens_total` | Counter | `org_id, task=structured_extraction` | Token consumption |

---

## 10. Eval & Acceptance Criteria

| Metric | Target | Notes |
|--------|--------|-------|
| Schema validation pass rate | >= 0.95 | Extracted JSON matches schema |
| End-to-end accuracy | >= 0.85 | Manual review of extraction quality on test conversations |
| Latency p50 | <= 20s | Per-task (entire session extraction) |
| Latency p99 | <= 60s | Including retries |

**Golden dataset location:** `tests/evals/golden_structured_extraction.json`

Each entry contains a conversation, a JSON Schema, and the expected extracted output.

---

## 11. Testing Guide

### 11.1 Unit Tests

- `test_create_schema_valid` — valid JSON Schema creates successfully
- `test_create_schema_duplicate_name` — duplicate name returns 409
- `test_get_extraction_by_session` — returns stored extraction
- `test_get_extraction_not_found` — missing extraction returns 404
- `test_schema_validation_rejects_wrong_type` — LLM returns wrong type → retry
- `test_schema_validation_missing_required` — missing required field → retry
- `test_idempotent_rerun` — same session+schema processed twice skips
- `test_no_schemas_configured` — org with no schemas → no worker enqueued

### 11.2 Integration Tests

- Create schema → close session → verify extraction result
- Multiple schemas → verify all schemas produce extractions
- Schema update → verify new sessions use updated schema

---

## 12. Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `STRUCTURED_EXTRACTION_PROMPT` | `extract_structured_v1` | Default prompt version |
| `STRUCTURED_EXTRACTION_MAX_RETRIES` | `3` | Max LLM call retries |
| `STRUCTURED_EXTRACTION_TIMEOUT` | `60` | LLM call timeout (seconds) — longer due to full session |
| `STRUCTURED_EXTRACTION_MODEL` | `gpt-4o` | LLM model (needs stronger reasoning for complex schemas) |
| `STRUCTURED_EXTRACTION_TEMPERATURE` | `0.0` | LLM temperature |
| `STRUCTURED_EXTRACTION_MAX_TOKENS` | `4000` | Max completion tokens |

---

## 13. Open Questions

| ID | Question | Status |
|----|----------|--------|
| SE-01 | Should we support multiple extraction schemas running on the same session? | **Decision:** Yes — all org schemas run in parallel. Each schema creates one extraction record. |
| SE-02 | Should the extraction be incremental (streaming) or batch (entire session at once)? | **Decision:** Batch only — the LLM needs the full conversation context. |
| SE-03 | Should we cache schema definitions in Redis to avoid DB load on every worker run? | **Decision:** Phase 2 — add Redis caching for schema lookup. TTL: 5 minutes. |

---

## 14. Related Documents

| Document | Why |
|----------|-----|
| [01-prompt-templates.md](01-prompt-templates.md) | `extract_structured_v1.jinja2` template |
| [07-user-session-mgmt/02-session-crud.md](../07-user-session-mgmt/02-session-crud.md) | Session close triggers structured extraction |
| [07-llm-cost-control.md](07-llm-cost-control.md) | Budget checking before LLM call |
| [08-api-gateway/01-app-setup.md](../08-api-gateway/01-app-setup.md) | Admin router registration |
| [14-testing/03-golden-datasets.md](../14-testing/03-golden-datasets.md) | Golden dataset for structured extraction eval |
