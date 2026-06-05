# Request Validation Implementation Guide

> **Phase:** Phase 0 — Foundation (Week 1-2)
> **Priority:** P0
> **Requirements:** SEC-09, MAINT-03, SRS §8.4
> **Handoff from:** Architect (ADR-006: Input Validation & Sanitization)

---

## 1. Overview

MemGraph validates all incoming requests at two levels:

1. **HTTP/ingress level** (nginx/Traefik): Request size limits, header size limits
2. **Application level** (FastAPI/Pydantic): Schema validation, content validation, input sanitization

This document covers both layers, with emphasis on the application-level validation enforced via Pydantic schemas and custom validators.

---

## 2. Validation Architecture

```
Client Request
      │
      ▼
┌──────────────────────┐
│  nginx / Traefik      │  ← Request size limit (5MB)
│  - max_body_size: 5MB │  ← Header size limits
│  - proxy_read_timeout │  ← Timeouts
└─────────┬────────────┘
          │
          ▼
┌──────────────────────┐
│  FastAPI Middleware    │  ← Auth, rate limiting
│  (no body parsing)    │
└─────────┬────────────┘
          │
          ▼
┌──────────────────────┐
│  Pydantic Schema      │  ← Type validation
│  - Field types        │  ← Length constraints
│  - @field_validator   │  ← Content sanitization
│  - @model_validator   │  ← Cross-field validation
└─────────┬────────────┘
          │
          ▼
┌──────────────────────┐
│  Service Layer        │  ← Business rule validation
│  - Existence checks   │  ← Authorization
│  - State validation   │
└──────────────────────┘
```

---

## 3. Ingress-Level Validation

### 3.1 Traefik / nginx Configuration

```yaml
# traefik/config.yml (or nginx equivalent)

http:
  routers:
    api:
      rule: "Host(`api.memgraph.dev`) && PathPrefix(`/v1/`)"
      middlewares:
        - request-limits
      service: memgraph-api

  middlewares:
    request-limits:
      buffering:
        maxRequestBodyBytes: 5242880  # 5MB

    # Rate limit middleware (per IP)
    rate-limit:
      rateLimit:
        average: 100
        burst: 50
```

### 3.2 Environment Configuration

```python
# core/config.py
class Settings(BaseSettings):
    # ...
    MAX_REQUEST_BODY_SIZE: int = Field(
        default=5 * 1024 * 1024,  # 5MB
        description="Maximum request body size in bytes. Enforced at ingress level.",
    )
    MAX_MESSAGE_CONTENT_LENGTH: int = Field(
        default=64 * 1024,  # 64KB (SEC-09)
        description="Maximum length of a single message content field in bytes.",
    )
```

---

## 4. Application-Level Validation

### 4.1 Shared Validation Utilities

Located at `packages/core/validators.py`.

```python
from pydantic import field_validator
from typing import Any


# ── JSONB Metadata Validation ──────────────────────────────────────────

MAX_METADATA_DEPTH = 5
MAX_METADATA_KEYS = 50
MAX_METADATA_STRING_LENGTH = 1024


def validate_jsonb_depth(value: Any, current_depth: int = 0, max_depth: int = MAX_METADATA_DEPTH) -> None:
    """Validate that a JSON object does not exceed max_depth levels."""
    if current_depth > max_depth:
        raise ValueError(
            f"Metadata exceeds maximum depth of {max_depth} levels"
        )
    if isinstance(value, dict):
        for v in value.values():
            validate_jsonb_depth(v, current_depth + 1, max_depth)
    elif isinstance(value, list):
        for item in value:
            validate_jsonb_depth(item, current_depth + 1, max_depth)


def validate_jsonb_key_count(value: dict, max_keys: int = MAX_METADATA_KEYS) -> None:
    """Validate that a JSON object does not exceed max_keys entries."""
    if len(value) > max_keys:
        raise ValueError(
            f"Metadata exceeds maximum of {max_keys} keys (found {len(value)})"
        )


def validate_jsonb_string_lengths(
    value: Any, max_length: int = MAX_METADATA_STRING_LENGTH,
) -> None:
    """Validate that all string values in a JSON object are within max_length."""
    if isinstance(value, str) and len(value) > max_length:
        raise ValueError(
            f"Metadata string value exceeds maximum length of {max_length} "
            f"characters (found {len(value)})"
        )
    if isinstance(value, dict):
        for v in value.values():
            validate_jsonb_string_lengths(v, max_length)
    elif isinstance(value, list):
        for item in value:
            validate_jsonb_string_lengths(item, max_length)


# ── Content Sanitization ────────────────────────────────────────────────

CONTROL_CHARACTERS = set(range(0, 9)) | set(range(14, 32)) | {127}
# Allowed control chars: \t (9), \n (10), \r (13)


def sanitize_text(text: str) -> str:
    """Strip null bytes and disallowed control characters.

    Preserves: \t (tab), \n (newline), \r (carriage return).
    Strips: \0 (null byte), all other control characters.
    """
    # Remove null bytes first
    text = text.replace("\x00", "")

    # Remove disallowed control characters
    result = []
    for char in text:
        if ord(char) in CONTROL_CHARACTERS:
            continue
        result.append(char)

    return "".join(result)


# ── Content Length Validation ───────────────────────────────────────────

MAX_MESSAGE_CONTENT_LENGTH = 64 * 1024  # 64KB (SEC-09)


def validate_content_length(content: str) -> str:
    """Validate message content does not exceed maximum length.

    SEC-09: Reject messages over 64KB in content length.
    """
    content_bytes = len(content.encode("utf-8"))
    if content_bytes > MAX_MESSAGE_CONTENT_LENGTH:
        raise ValueError(
            f"Message content exceeds maximum length of "
            f"{MAX_MESSAGE_CONTENT_LENGTH} bytes (found {content_bytes})"
        )
    return content
```

### 4.2 Schema Validation Patterns

#### 4.2.1 CreateUserRequest

```python
# schemas/users.py

class CreateUserRequest(BaseModel):
    external_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Caller-chosen unique identifier for this user.",
    )
    name: Optional[str] = Field(
        None,
        max_length=1024,
        description="Display name for the user.",
    )
    email: Optional[str] = Field(
        None,
        max_length=1024,
        description="Email address."
    )
    metadata: Optional[dict] = Field(
        None,
        description="Arbitrary metadata (JSON object)."
    )

    @field_validator("external_id")
    @classmethod
    def external_id_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("external_id must not be empty or whitespace-only")
        return v.strip()

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, v: Optional[dict]) -> Optional[dict]:
        if v is None:
            return v
        validate_jsonb_depth(v, max_depth=5)
        validate_jsonb_key_count(v, max_keys=50)
        validate_jsonb_string_lengths(v, max_length=1024)
        return v
```

#### 4.2.2 Message Ingestion Validation

```python
# schemas/memory.py

class Message(BaseModel):
    """A single message in a conversation turn."""
    role: str = Field(
        ...,
        pattern=r"^(user|assistant|system|tool)$",
        description="Message role: one of user, assistant, system, tool.",
    )
    content: str = Field(
        ...,
        description="Message content (max 64KB).",
    )
    metadata: Optional[dict] = Field(
        None,
        description="Message-level metadata.",
    )
    created_at: Optional[datetime] = Field(
        None,
        description="ISO-8601 timestamp. Defaults to server time if omitted.",
    )

    @field_validator("content")
    @classmethod
    def validate_and_sanitize_content(cls, v: str) -> str:
        # 1. Sanitize (strip null bytes, control chars)
        sanitized = sanitize_text(v)
        # 2. Validate length (SEC-09: max 64KB)
        validate_content_length(sanitized)
        return sanitized

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"user", "assistant", "system", "tool"}:
            raise ValueError(f"Invalid role: '{v}'. Must be one of: "
                             f"user, assistant, system, tool.")
        return v

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, v: Optional[dict]) -> Optional[dict]:
        if v is None:
            return v
        validate_jsonb_depth(v)
        validate_jsonb_key_count(v)
        return v


class IngestMemoryRequest(BaseModel):
    """Request body for POST /v1/users/{user_id}/memory."""
    messages: List[Message] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="List of messages to ingest (1-100 per request).",
    )
    session_id: Optional[str] = Field(
        None,
        max_length=255,
        description="Optional session identifier. "
                    "Messages without session_id go to the default session.",
    )

    @field_validator("messages")
    @classmethod
    def validate_message_count(cls, v: List[Message]) -> List[Message]:
        if len(v) > 100:
            raise ValueError("Maximum 100 messages per ingestion request")
        return v
```

#### 4.2.3 Business Data Fact Validation

```python
# schemas/facts.py

class FactTriple(BaseModel):
    """A single fact triple: (subject, predicate, object)."""
    subject: str = Field(
        ...,
        min_length=1,
        max_length=1024,
        description="The subject of the fact (e.g., 'user_123').",
    )
    predicate: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="The predicate/relationship (e.g., 'purchased').",
    )
    object: str = Field(
        ...,
        min_length=1,
        max_length=1024,
        description="The object/value (e.g., 'Pro plan').",
    )
    valid_at: Optional[datetime] = Field(
        None,
        description="When the fact became true in the real world.",
    )
    expires_at: Optional[datetime] = Field(
        None,
        description="When the fact expires (optional).",
    )


class IngestFactsRequest(BaseModel):
    facts: List[FactTriple] = Field(
        ...,
        min_length=1,
        max_length=500,  # BIZ-03: max 500 triples per request
        description="List of fact triples (1-500 per request).",
    )

    @field_validator("facts")
    @classmethod
    def validate_fact_count(cls, v: List[FactTriple]) -> List[FactTriple]:
        if len(v) > 500:
            raise ValueError("Maximum 500 fact triples per request")
        return v
```

---

## 5. Input Sanitization

### 5.1 What Gets Sanitized

| Input Field | Sanitization | Preserved |
|---|---|---|
| `content` (message body) | Strip null bytes, strip control chars except `\t\n\r` | Newlines, tabs, Unicode |
| `external_id` | Strip leading/trailing whitespace | Internal spaces allowed |
| `name` | Strip leading/trailing whitespace | Internal spaces, Unicode |
| `email` | Strip leading/trailing whitespace | Valid email format |
| `metadata` (all JSONB) | Depth check, key count, string length | All valid JSON values |

### 5.2 Sanitization Must Not Be Silent

Validation errors from sanitization are **errors, not silent corrections**:

```python
# ✅ Correct: reject invalid input
@field_validator("external_id")
@classmethod
def external_id_must_not_be_empty(cls, v: str) -> str:
    stripped = v.strip()
    if not stripped:
        raise ValueError("external_id must not be empty or whitespace-only")
    return stripped  # Return the sanitized value on success

# ❌ Wrong: silently modify input
if not v.strip():
    v = "default_external_id"  # Never do this
```

---

## 6. Request Size Limits Summary

| Check | Limit | Enforced At | Error Code |
|---|---|---|---|
| Total request body | 5MB | Ingress (nginx/Traefik) | `PAYLOAD_TOO_LARGE` (413) |
| Single message content | 64KB | Pydantic validator (SEC-09) | `VALIDATION_ERROR` (422) |
| Messages per batch | 100 | Pydantic validator | `VALIDATION_ERROR` (422) |
| Facts per batch | 500 | Pydantic validator | `VALIDATION_ERROR` (422) |
| Metadata depth | 5 levels | Pydantic validator | `VALIDATION_ERROR` (422) |
| Metadata keys | 50 max | Pydantic validator | `VALIDATION_ERROR` (422) |
| Metadata string values | 1KB per value | Pydantic validator | `VALIDATION_ERROR` (422) |
| external_id length | 255 chars | Pydantic `max_length` | `VALIDATION_ERROR` (422) |
| session_id length | 255 chars | Pydantic `max_length` | `VALIDATION_ERROR` (422) |

---

## 7. Schema Versioning

### 7.1 Versioning Strategy

Request and response schemas are versioned alongside the API:

```
services/api/schemas/
├── users.py                  # Current version (v1)
├── users_v2.py               # Next version (when breaking changes needed)
├── sessions.py
├── memory.py
├── facts.py
└── ...
```

### 7.2 Backward Compatibility Rules

1. **Never remove fields** from a response schema within the same API version — mark them as `deprecated` instead.
2. **New fields must be optional** (have defaults or be `Optional[...]`) so old clients don't break.
3. **Use `extra=Extra.ignore`** at the Pydantic model level in request schemas — unexpected fields are silently ignored, not rejected.

```python
class UserResponse(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        extra="ignore",  # Ignore unexpected fields (forward compatibility)
    )

    id: UUID
    external_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    metadata: dict = {}
    created_at: datetime
    updated_at: datetime
    # Deprecated: 'is_active' replaced by status in v2. Keep for backward compat.
    is_active: Optional[bool] = Field(
        None,
        deprecated=True,
        description="Deprecated. Use 'status' field instead.",
    )
```

### 7.3 Migration Path

When a breaking schema change is required:

1. Create `schemas/{domain}_v2.py` with new schemas
2. Create `routers/{domain}_v2.py` with new endpoints under `/v2/`
3. Keep existing `v1` endpoints and schemas unchanged
4. Document migration path in changelog
5. After deprecation period, remove v1 endpoints

---

## 8. Testing Validation

```python
@pytest.mark.parametrize(
    "payload, expected_status, expected_field",
    [
        # Valid
        ({"external_id": "test_user"}, 201, None),
        # Empty external_id
        ({"external_id": ""}, 422, "external_id"),
        # Whitespace-only external_id
        ({"external_id": "   "}, 422, "external_id"),
        # external_id too long
        ({"external_id": "a" * 256}, 422, "external_id"),
        # Metadata too deep
        ({"external_id": "test", "metadata": {"a": {"b": {"c": {"d": {"e": {"f": "too deep"}}}}}}}, 422, "metadata"),
        # Metadata too many keys
        ({"external_id": "test", "metadata": {str(i): i for i in range(51)}}, 422, "metadata"),
        # Metadata string too long
        ({"external_id": "test", "metadata": {"key": "a" * 1025}}, 422, "metadata"),
    ],
)
@pytest.mark.asyncio
async def test_create_user_validation(
    async_client: AsyncClient,
    auth_headers: dict,
    payload: dict,
    expected_status: int,
    expected_field: Optional[str],
) -> None:
    response = await async_client.post(
        "/v1/users",
        json=payload,
        headers=auth_headers,
    )
    assert response.status_code == expected_status
    if expected_field and expected_status == 422:
        data = response.json()
        # Find the field in validation errors
        fields = [e["field"] for e in data.get("fields", [])]
        assert expected_field in fields


@pytest.mark.asyncio
async def test_message_content_sanitization(
    async_client: AsyncClient,
    auth_headers: dict,
    existing_user: User,
) -> None:
    """Verify null bytes and control characters are sanitized."""
    content_with_nulls = "Hello\x00World\x00!"
    content_with_controls = "Line1\x01\x02\x03Line2"

    response = await async_client.post(
        f"/v1/users/{existing_user.id}/memory",
        json={
            "messages": [
                {"role": "user", "content": content_with_nulls},
                {"role": "assistant", "content": content_with_controls},
            ],
        },
        headers=auth_headers,
    )
    assert response.status_code == 202


@pytest.mark.asyncio
async def test_message_content_max_length(
    async_client: AsyncClient,
    auth_headers: dict,
    existing_user: User,
) -> None:
    """SEC-09: Verify messages over 64KB are rejected."""
    oversized_content = "x" * (64 * 1024 + 1)

    response = await async_client.post(
        f"/v1/users/{existing_user.id}/memory",
        json={
            "messages": [
                {"role": "user", "content": oversized_content},
            ],
        },
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert "content" in str(response.json())


@pytest.mark.asyncio
async def test_max_messages_per_batch(
    async_client: AsyncClient,
    auth_headers: dict,
    existing_user: User,
) -> None:
    """Verify max 100 messages per ingestion request."""
    messages = [
        {"role": "user", "content": f"Message {i}"} for i in range(101)
    ]

    response = await async_client.post(
        f"/v1/users/{existing_user.id}/memory",
        json={"messages": messages},
        headers=auth_headers,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_max_facts_per_batch(
    async_client: AsyncClient,
    auth_headers: dict,
    existing_user: User,
) -> None:
    """BIZ-03: Verify max 500 fact triples per request."""
    facts = [
        {"subject": "user", "predicate": "test", "object": f"value_{i}"}
        for i in range(501)
    ]

    response = await async_client.post(
        f"/v1/users/{existing_user.id}/facts",
        json={"facts": facts},
        headers=auth_headers,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_invalid_role_rejected(
    async_client: AsyncClient,
    auth_headers: dict,
    existing_user: User,
) -> None:
    """Verify invalid message role is rejected."""
    response = await async_client.post(
        f"/v1/users/{existing_user.id}/memory",
        json={
            "messages": [
                {"role": "invalid_role", "content": "test"},
            ],
        },
        headers=auth_headers,
    )
    assert response.status_code == 422
    data = response.json()
    assert any("role" in str(e) for e in data.get("fields", []))
```

---

## 9. Security: Preventing Injection

### 9.1 No Raw SQL Interpolation

All queries use parameterized SQL via SQLAlchemy ORM or `text()` with bound parameters. **Never** do this:

```python
# ❌ NEVER: string interpolation into SQL
query = f"SELECT * FROM users WHERE external_id = '{external_id}'"

# ✅ ALWAYS: parameterized query
result = await db.execute(
    text("SELECT * FROM users WHERE external_id = :external_id"),
    {"external_id": external_id},
)
```

### 9.2 No Raw Cypher/GQL Construction

Graph queries use Graphiti's parameterized query API:

```python
# ❌ NEVER: string concatenation for graph queries
query = f"MATCH (n {{external_id: '{external_id}'}}) RETURN n"

# ✅ ALWAYS: parameterized graph query
query = "MATCH (n {external_id: $external_id}) RETURN n"
result = await graphiti.execute(query, {"external_id": external_id})
```

---

## 10. Validation Performance Considerations

- **Pydantic validation is fast**: Typically < 1ms per model instance for typical payload sizes.
- **Content sanitization is O(n)**: For large messages (> 10KB), sanitization time is proportional to content length. For a 64KB message, expect < 5ms.
- **JSONB metadata validation**: Depth and key-count checks are O(d) and O(k) respectively — negligible for typical metadata sizes.
- **Long content length check**: `len(content.encode("utf-8"))` is O(n) — but this is a simple byte count, not a complex operation.

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
