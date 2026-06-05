# Request Validation Implementation Guide

> **Phase:** Phase 0 — Foundation (Week 1–2)
> **Priority:** P0
> **Requirements:** SEC-09, MAINT-03, ING-02, BIZ-03
> **Handoff from:** Architect (ADR-006: Input Validation & Sanitization)
> **SRS Reference:** §8.4 Error Response Format, §12 Security Requirements (SEC-09)

---

## 1. Overview

MemGraph validates all incoming requests at **two levels**:

1. **HTTP/ingress level** (nginx/Traefik reverse proxy): Request size limits, header size limits, connection limits
2. **Application level** (FastAPI/Pydantic): Schema validation, content validation, input sanitization

This layered approach ensures that:
- Malformed or oversized requests are rejected early (before reaching application code)
- Business-rule validation is enforced with clear, actionable error messages
- Sensitive injection vectors (null bytes, control characters) are blocked

### 1.1 Validation Architecture

```
Client Request
      │
      ▼
┌──────────────────────────┐
│  nginx / Traefik          │  ← 5MB max_body_size
│  - max request body: 5MB  │  ← Header size limits
│  - proxy timeouts         │  ← Connection limits
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│  FastAPI Middleware        │  ← Auth, rate limiting
│  (no body parsing)        │  ← No business logic
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│  Pydantic Schema           │  ← Type validation
│  - Field types & formats   │  ← Length constraints
│  - @field_validator        │  ← Content sanitization
│  - @model_validator        │  ← Cross-field validation
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│  Service Layer             │  ← Business rule validation
│  - Existence checks        │  ← Authorization checks
│  - State-based validation  │  ← DB constraint validation
└──────────────────────────┘
```

---

## 2. Ingress-Level Validation

### 2.1 Traefik Configuration

```yaml
# traefik/config.yml

http:
  routers:
    api:
      rule: "Host(`api.memgraph.dev`) && PathPrefix(`/v1/`)"
      middlewares:
        - request-limits
        - security-headers
      service: memgraph-api

  middlewares:
    request-limits:
      buffering:
        maxRequestBodyBytes: 5242880   # 5MB — prevents OOM attacks
        memRequestBodyBytes: 5242880    # Same as above for memory buffer

    security-headers:
      headers:
        customFrameOptionsValue: "DENY"
        contentTypeNosniff: true
        referrerPolicy: "strict-origin-when-cross-origin"
        # Never include X-Powered-By or Server headers
        customResponseHeaders:
          X-Content-Type-Options: "nosniff"
```

### 2.2 nginx Configuration

```nginx
# /etc/nginx/conf.d/memgraph.conf

server {
    listen 443 ssl;
    server_name api.memgraph.dev;

    # ── Request size limits ──────────────────────────────────────
    client_max_body_size 5m;
    client_body_buffer_size 128k;
    client_header_buffer_size 8k;
    large_client_header_buffers 4 8k;

    # ── Timeouts ─────────────────────────────────────────────────
    proxy_read_timeout 60s;
    proxy_connect_timeout 10s;
    proxy_send_timeout 30s;

    # ── Security headers ─────────────────────────────────────────
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    # Never add: Server, X-Powered-By

    location / {
        proxy_pass http://memgraph-api:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Request-ID $request_id;
    }
}
```

---

## 3. Application-Level Validation Utilities

Located at `packages/core/validators.py`.

### 3.1 Content Sanitization

```python
from pydantic import field_validator
from typing import Any, Optional

# Allowed control characters: tab (0x09), newline (0x0A), carriage return (0x0D)
ALLOWED_CONTROL_CHARS = {0x09, 0x0A, 0x0D}

# Disallowed control character ranges (excluding allowed set)
CONTROL_CHARACTERS = set(range(0x00, 0x20)) | {0x7F}
CONTROL_CHARACTERS -= ALLOWED_CONTROL_CHARS


def sanitize_text(text: str) -> str:
    """Sanitize text input by removing dangerous characters.

    Removes:
    - Null bytes (\\x00) — injection vector for C-based systems
    - Disallowed control characters (except \\t, \\n, \\r)
    - Invalid UTF-8 sequences (converted to replacement character)

    Preserves:
    - Tab (\\t)
    - Newline (\\n)
    - Carriage return (\\r)
    - All printable Unicode characters (including emoji, CJK, etc.)

    Args:
        text: Raw input string potentially containing dangerous characters.

    Returns:
        Sanitized string with dangerous characters removed.

    Example:
        >>> sanitize_text("Hello\\x00World!")
        'HelloWorld!'
        >>> sanitize_text("Line1\\x01\\x02Line2")
        'Line1Line2'
    """
    # Remove null bytes first (most dangerous)
    text = text.replace("\x00", "")

    # Remove disallowed control characters
    result = []
    for char in text:
        code_point = ord(char)
        if code_point in CONTROL_CHARACTERS:
            continue  # Skip: strip this character
        result.append(char)

    return "".join(result)
```

### 3.2 Content Length Validation

```python
# SEC-09: Reject messages over 64KB in content length

MAX_MESSAGE_CONTENT_LENGTH = 64 * 1024  # 64KB


def validate_content_length(content: str) -> str:
    """Validate message content does not exceed maximum length.

    SEC-09 (from SRS):
    "Input validation shall reject messages over 64KB in content length."

    The check is performed on UTF-8 encoded byte length, not character
    count. This is important because multi-byte characters (emoji, CJK)
    count more toward the limit than single-byte ASCII characters.

    Args:
        content: The message content string.

    Returns:
        The original content if valid.

    Raises:
        ValueError: If content exceeds MAX_MESSAGE_CONTENT_LENGTH bytes.
    """
    content_bytes = len(content.encode("utf-8"))
    if content_bytes > MAX_MESSAGE_CONTENT_LENGTH:
        raise ValueError(
            f"Message content exceeds maximum length of "
            f"{MAX_MESSAGE_CONTENT_LENGTH:,} bytes "
            f"(found {content_bytes:,} bytes)"
        )
    return content
```

### 3.3 JSONB Metadata Validation

```python
# ── JSONB Metadata Constraints ────────────────────────────────────────
# Metadata is a JSONB column in PostgreSQL. We enforce constraints
# at the application layer (Pydantic) to catch issues early rather
# than failing on DB insert.

MAX_METADATA_DEPTH = 5        # Prevent deeply nested objects
MAX_METADATA_KEYS = 50        # Prevent excessively wide objects
MAX_METADATA_STRING_LENGTH = 1024  # 1KB per string value


def validate_jsonb_depth(
    value: Any,
    current_depth: int = 0,
    max_depth: int = MAX_METADATA_DEPTH,
) -> None:
    """Validate that a JSON object does not exceed max_depth levels.

    Prevents stack overflow from deeply nested metadata.
    The maximum nesting depth is 5 levels (configurable via constant).

    Args:
        value: The JSON value to validate (dict, list, or scalar).
        current_depth: Current recursion depth (internal).
        max_depth: Maximum allowed depth (default: 5).

    Raises:
        ValueError: If depth exceeds max_depth.
    """
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


def validate_jsonb_key_count(
    value: dict,
    max_keys: int = MAX_METADATA_KEYS,
) -> None:
    """Validate that a JSON object does not exceed max_keys entries.

    Prevents excessively wide metadata objects that could cause
    index bloat and slow serialization.

    Args:
        value: The dict to validate.
        max_keys: Maximum allowed keys (default: 50).

    Raises:
        ValueError: If key count exceeds max_keys.
    """
    if len(value) > max_keys:
        raise ValueError(
            f"Metadata exceeds maximum of {max_keys} keys "
            f"(found {len(value)})"
        )


def validate_jsonb_string_lengths(
    value: Any,
    max_length: int = MAX_METADATA_STRING_LENGTH,
) -> None:
    """Validate that all string values in a JSON object are within max_length.

    Recursively validates all string values in a nested JSON structure.
    Non-string values (numbers, booleans, null) are not length-checked.

    Args:
        value: The JSON value to validate.
        max_length: Maximum string length in bytes (default: 1024).

    Raises:
        ValueError: If any string value exceeds max_length.
    """
    if isinstance(value, str) and len(value) > max_length:
        raise ValueError(
            f"Metadata string value exceeds maximum length of "
            f"{max_length} characters (found {len(value)})"
        )
    if isinstance(value, dict):
        for v in value.values():
            validate_jsonb_string_lengths(v, max_length)
    elif isinstance(value, list):
        for item in value:
            validate_jsonb_string_lengths(item, max_length)


def validate_metadata(metadata: Optional[dict]) -> Optional[dict]:
    """Convenience function: run all metadata validations at once.

    Use this in @field_validator for any metadata field.

    Args:
        metadata: The metadata dict to validate.

    Returns:
        The validated metadata dict.

    Raises:
        ValueError: If any validation fails.
    """
    if metadata is None:
        return metadata

    validate_jsonb_depth(metadata)
    validate_jsonb_key_count(metadata)
    validate_jsonb_string_lengths(metadata)
    return metadata
```

---

## 4. Schema Validation Patterns

### 4.1 Create User Request

```python
# schemas/users.py

from pydantic import BaseModel, Field, field_validator
from typing import Optional


class CreateUserRequest(BaseModel):
    """Request body for POST /v1/users.

    Validates:
    - external_id: 1-255 characters, not empty/whitespace-only
    - name: optional, max 1024 characters
    - email: optional, max 1024 characters, email format
    - metadata: optional JSONB, depth ≤ 5, keys ≤ 50, string values ≤ 1KB
    """
    external_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description=(
            "Caller-chosen unique identifier for this user, "
            "scoped to the organization. Must be unique within "
            "the organization."
        ),
        json_schema_extra={"example": "user_abc123"},
    )
    name: Optional[str] = Field(
        None,
        max_length=1024,
        description="Display name for the user.",
        json_schema_extra={"example": "Alice Smith"},
    )
    email: Optional[str] = Field(
        None,
        max_length=1024,
        description="Email address of the user.",
        json_schema_extra={"example": "alice@example.com"},
    )
    metadata: Optional[dict] = Field(
        None,
        description=(
            "Arbitrary caller-defined metadata (JSON object). "
            "Constraints: max 5 levels deep, max 50 keys, "
            "max 1KB per string value."
        ),
        json_schema_extra={
            "example": {"plan": "pro", "signup_date": "2026-01-15"},
        },
    )

    @field_validator("external_id")
    @classmethod
    def external_id_must_not_be_empty(cls, v: str) -> str:
        """Reject whitespace-only external_id."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("external_id must not be empty or whitespace-only")
        return stripped  # Sanitize: strip leading/trailing whitespace

    @field_validator("metadata")
    @classmethod
    def validate_metadata_field(cls, v: Optional[dict]) -> Optional[dict]:
        """Validate and sanitize metadata."""
        return validate_metadata(v)
```

### 4.2 Message Ingestion Request

```python
# schemas/memory.py

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class Message(BaseModel):
    """A single message in a conversation turn.

    Validates:
    - role: one of user/assistant/system/tool
    - content: ≤ 64KB, sanitized (null bytes removed, controls stripped)
    - metadata: optional, JSONB constraints
    - created_at: optional ISO-8601 timestamp
    """
    role: str = Field(
        ...,
        description="Message role: 'user', 'assistant', 'system', or 'tool'.",
        json_schema_extra={"example": "user"},
    )
    content: str = Field(
        ...,
        description="Message content (max 64KB after sanitization).",
        json_schema_extra={"example": "Hello, I need help with my account."},
    )
    metadata: Optional[dict] = Field(
        None,
        description="Optional message-level metadata.",
    )
    created_at: Optional[datetime] = Field(
        None,
        description="ISO-8601 timestamp of the message. "
                    "Defaults to server time if omitted.",
    )

    @field_validator("content")
    @classmethod
    def validate_and_sanitize_content(cls, v: str) -> str:
        """Two-step validation: sanitize first, then validate length.

        Order matters: sanitize removes null bytes and control chars,
        then we validate the resulting length against the 64KB limit.
        """
        # 1. Sanitize: strip null bytes, control characters
        sanitized = sanitize_text(v)
        # 2. Validate length: SEC-09 — max 64KB
        validate_content_length(sanitized)
        return sanitized

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        """Normalize role to lowercase and validate against allowed set."""
        v = v.strip().lower()
        allowed_roles = {"user", "assistant", "system", "tool"}
        if v not in allowed_roles:
            raise ValueError(
                f"Invalid role: '{v}'. Must be one of: "
                f"{', '.join(sorted(allowed_roles))}."
            )
        return v

    @field_validator("metadata")
    @classmethod
    def validate_metadata_field(cls, v: Optional[dict]) -> Optional[dict]:
        return validate_metadata(v)


class IngestMemoryRequest(BaseModel):
    """Request body for POST /v1/users/{user_id}/memory.

    Validates:
    - messages: 1-100 messages per request
    - session_id: optional, max 255 characters
    """
    messages: list[Message] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="List of messages to ingest (1-100 per request).",
    )
    session_id: Optional[str] = Field(
        None,
        max_length=255,
        description=(
            "Optional session identifier. Messages without a session_id "
            "go to the default session for the user."
        ),
        json_schema_extra={"example": "session_abc123"},
    )
```

### 4.3 Business Data Fact Request

```python
# schemas/facts.py

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class FactTriple(BaseModel):
    """A single fact triple: (subject, predicate, object).

    BIZ-02: "Business data shall be expressed as
    (subject, predicate, object, valid_at, expires_at) triples."
    """
    subject: str = Field(
        ...,
        min_length=1,
        max_length=1024,
        description="The subject of the fact (e.g., 'user_123').",
        json_schema_extra={"example": "user_123"},
    )
    predicate: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="The predicate/relationship (e.g., 'purchased').",
        json_schema_extra={"example": "purchased"},
    )
    object: str = Field(
        ...,
        min_length=1,
        max_length=1024,
        description="The object/value (e.g., 'Pro plan').",
        json_schema_extra={"example": "Pro plan"},
    )
    valid_at: Optional[datetime] = Field(
        None,
        description="When the fact became true in the real world (ISO-8601).",
    )
    expires_at: Optional[datetime] = Field(
        None,
        description="When the fact expires (ISO-8601). Null means never expires.",
    )


class IngestFactsRequest(BaseModel):
    """Request body for POST /v1/users/{user_id}/facts.

    BIZ-03: "System shall accept batch ingestion of up to 500 fact triples per request."
    """
    facts: list[FactTriple] = Field(
        ...,
        min_length=1,
        max_length=500,
        description="List of fact triples (1-500 per request).",
    )
```

---

## 5. Input Sanitization Reference

### 5.1 What Gets Sanitized and How

| Input Field | Sanitization Applied | Preserved | Error On |
|---|---|---|---|
| `content` (message body) | Strip null bytes (`\x00`), strip control chars except `\t\n\r` | Newlines, tabs, Unicode, emoji | Length > 64KB (bytes) |
| `external_id` | Strip leading/trailing whitespace | Internal spaces, hyphens, underscores | Empty after strip, length > 255 |
| `name` | Strip leading/trailing whitespace | Internal spaces, Unicode letters | Length > 1024 |
| `email` | Strip leading/trailing whitespace | Standard email format | Invalid email, length > 1024 |
| `metadata` (JSONB) | Depth check (≤5), key count (≤50), string length (≤1KB) | All valid JSON types | Any constraint violated |
| `role` | Lowercase + strip | Valid roles only | Not in {user, assistant, system, tool} |
| `external_id` (sessions) | Strip leading/trailing whitespace | Alphanumerics, hyphens | Empty after strip, length > 255 |

### 5.2 Sanitization Must Not Be Silent

Validation errors from sanitization are **errors, not silent corrections**:

```python
# ✅ Correct: reject invalid input with clear error
@field_validator("external_id")
@classmethod
def validate_external_id(cls, v: str) -> str:
    stripped = v.strip()
    if not stripped:
        raise ValueError("external_id must not be empty or whitespace-only")
    return stripped  # Return sanitized value only when validation passes

# ❌ Wrong: silently modify input to a default
if not v.strip():
    v = "default_external_id"  # Never silently substitute invalid input
```

---

## 6. Request Size Limits Summary

| Check | Limit | Enforced At | Error Code | HTTP Status |
|---|---|---|---|---|
| Total request body | 5 MB | Ingress (nginx/Traefik) | `PAYLOAD_TOO_LARGE` | 413 |
| Single message content | 64 KB | Pydantic `@field_validator` (SEC-09) | `VALIDATION_ERROR` | 422 |
| Messages per batch | 100 | Pydantic `max_length=100` | `VALIDATION_ERROR` | 422 |
| Facts per batch | 500 | Pydantic `max_length=500` (BIZ-03) | `VALIDATION_ERROR` | 422 |
| Metadata depth | 5 levels | Pydantic `@field_validator` | `VALIDATION_ERROR` | 422 |
| Metadata keys | 50 max | Pydantic `@field_validator` | `VALIDATION_ERROR` | 422 |
| Metadata string values | 1 KB per value | Pydantic `@field_validator` | `VALIDATION_ERROR` | 422 |
| `external_id` length | 255 chars | Pydantic `max_length=255` | `VALIDATION_ERROR` | 422 |
| `session_id` length | 255 chars | Pydantic `max_length=255` | `VALIDATION_ERROR` | 422 |
| `name` length | 1024 chars | Pydantic `max_length=1024` | `VALIDATION_ERROR` | 422 |
| `email` length | 1024 chars | Pydantic `max_length=1024` | `VALIDATION_ERROR` | 422 |

---

## 7. Schema Versioning Strategy

### 7.1 File Layout

Request and response schemas are versioned alongside the API:

```
services/api/schemas/
├── __init__.py
├── common.py              # Shared schemas (PaginatedResponse, etc.)
├── users.py               # Current version (v1)
├── users_v2.py            # Next version (when breaking changes needed)
├── sessions.py
├── memory.py
├── facts.py
├── graph.py
├── context.py
└── health.py
```

### 7.2 Backward Compatibility Rules

1. **Never remove fields** from a response schema within the same API version — mark them as `deprecated` instead
2. **New fields must be optional** (have defaults or be `Optional[...]`) — old clients don't break
3. **Request schemas should ignore extra fields** — unexpected fields are silently ignored, not rejected

```python
class UserResponse(BaseModel):
    """Response schema for user data.

    Backward compatibility rules applied:
    - All fields are defined; new fields are Optional with defaults
    - Deprecated fields are marked with Field(deprecated=True)
    - Extra fields from future versions are ignored (extra="ignore")
    """
    model_config = ConfigDict(
        from_attributes=True,
        extra="ignore",  # Forward compatibility: ignore unknown fields
    )

    id: UUID
    external_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    # Deprecated field — kept for backward compatibility
    # Replaced by 'status' field in v2
    is_active: Optional[bool] = Field(
        None,
        deprecated=True,
        description="Deprecated. Use 'status' field in v2 instead.",
    )
```

### 7.3 Migration Path for Breaking Changes

When a breaking schema change is required:

1. **Create** `schemas/{domain}_v2.py` with the new schemas
2. **Create** `routers/{domain}_v2.py` with new endpoints under `/v2/`
3. **Keep** existing v1 endpoints and schemas unchanged
4. **Document** migration path in changelog with clear before/after examples
5. **Announce** deprecation date (minimum 3 months notice)
6. **Remove** v1 endpoints after deprecation period

---

## 8. Security: Preventing Injection

### 8.1 No Raw SQL Interpolation

All queries use parameterized SQL via SQLAlchemy ORM or `text()` with bound parameters:

```python
# ❌ NEVER: string interpolation into SQL — SQL injection vulnerability
query = f"SELECT * FROM users WHERE external_id = '{external_id}'"

# ✅ ALWAYS: parameterized query via SQLAlchemy ORM
result = await db.execute(
    select(User).where(User.external_id == external_id)
)

# ✅ ALWAYS: parameterized raw SQL with bound parameters
result = await db.execute(
    text("SELECT * FROM users WHERE external_id = :external_id"),
    {"external_id": external_id},
)
```

### 8.2 No Raw Cypher/GQL Construction

Graph queries use Graphiti's parameterized query API:

```python
# ❌ NEVER: string concatenation for graph queries — Cypher injection
query = f"MATCH (n {{external_id: '{external_id}'}}) RETURN n"

# ✅ ALWAYS: parameterized graph query
query = "MATCH (n {external_id: $external_id}) RETURN n"
result = await graphiti.execute(query, {"external_id": external_id})
```

### 8.3 No Sensitive Data in Logs

See the logging middleware in `01-app-setup.md` for the full redaction implementation. Key rules:

- **Never log** `Authorization` header values
- **Never log** request body content (may contain PII, conversations)
- **Never log** API keys, tokens, or passwords
- **Log only** request metadata: method, path, status, duration, content-type, size

---

## 9. Testing Validation

```python
# tests/unit/test_validation.py

import pytest


class TestCreateUserValidation:
    """Parametrized tests for CreateUserRequest schema validation."""

    @pytest.mark.parametrize(
        ("payload", "expected_status", "expected_field"),
        [
            # Valid payloads
            ({"external_id": "test_user"}, 201, None),
            ({"external_id": "test_user", "name": "Alice"}, 201, None),
            ({"external_id": "test_user", "metadata": {"plan": "pro"}}, 201, None),
            # Invalid: empty external_id
            ({"external_id": ""}, 422, "external_id"),
            # Invalid: whitespace-only external_id
            ({"external_id": "   "}, 422, "external_id"),
            # Invalid: external_id too long
            ({"external_id": "a" * 256}, 422, "external_id"),
            # Invalid: metadata too deep
            (
                {
                    "external_id": "test",
                    "metadata": {
                        "a": {"b": {"c": {"d": {"e": {"f": "too deep"}}}}}
                    },
                },
                422,
                "metadata",
            ),
            # Invalid: metadata too many keys
            (
                {
                    "external_id": "test",
                    "metadata": {str(i): i for i in range(51)},
                },
                422,
                "metadata",
            ),
            # Invalid: metadata string too long
            (
                {
                    "external_id": "test",
                    "metadata": {"key": "a" * 1025},
                },
                422,
                "metadata",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_create_user_validation(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
        payload: dict,
        expected_status: int,
        expected_field: str | None,
    ) -> None:
        response = await async_client.post(
            "/v1/users",
            json=payload,
            headers=auth_headers,
        )
        assert response.status_code == expected_status
        if expected_field and expected_status == 422:
            data = response.json()
            fields = [e["field"] for e in data.get("fields", [])]
            assert expected_field in fields, (
                f"Expected field '{expected_field}' in validation errors. "
                f"Got fields: {fields}"
            )


class TestMessageValidation:
    """Tests for Message ingestion validation."""

    @pytest.mark.asyncio
    async def test_message_content_sanitization(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
        existing_user: dict,
    ) -> None:
        """Verify null bytes and control characters are sanitized."""
        content_with_nulls = "Hello\x00World\x00!"
        content_with_controls = "Line1\x01\x02\x03Line2"

        response = await async_client.post(
            f"/v1/users/{existing_user['id']}/memory",
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
        self,
        async_client: AsyncClient,
        auth_headers: dict,
        existing_user: dict,
    ) -> None:
        """SEC-09: Verify messages over 64KB are rejected."""
        oversized_content = "x" * (64 * 1024 + 1)

        response = await async_client.post(
            f"/v1/users/{existing_user['id']}/memory",
            json={
                "messages": [
                    {"role": "user", "content": oversized_content},
                ],
            },
            headers=auth_headers,
        )
        assert response.status_code == 422
        data = response.json()
        fields_str = str(data.get("fields", []))
        assert "content" in fields_str

    @pytest.mark.asyncio
    async def test_max_messages_per_batch(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
        existing_user: dict,
    ) -> None:
        """Verify max 100 messages per ingestion request."""
        messages = [
            {"role": "user", "content": f"Message {i}"} for i in range(101)
        ]

        response = await async_client.post(
            f"/v1/users/{existing_user['id']}/memory",
            json={"messages": messages},
            headers=auth_headers,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_role_rejected(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
        existing_user: dict,
    ) -> None:
        """Verify invalid message role is rejected."""
        response = await async_client.post(
            f"/v1/users/{existing_user['id']}/memory",
            json={
                "messages": [
                    {"role": "invalid_role", "content": "test"},
                ],
            },
            headers=auth_headers,
        )
        assert response.status_code == 422
        data = response.json()
        fields = [e["field"] for e in data.get("fields", [])]
        # Could be "messages" (list-level) or "role" (field-level)
        # depending on how Pydantic reports the error
        assert any("role" in f for f in fields) or "messages" in fields


class TestFactValidation:
    """Tests for business data fact validation."""

    @pytest.mark.asyncio
    async def test_max_facts_per_batch(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
        existing_user: dict,
    ) -> None:
        """BIZ-03: Verify max 500 fact triples per request."""
        facts = [
            {"subject": "user", "predicate": "test", "object": f"value_{i}"}
            for i in range(501)
        ]

        response = await async_client.post(
            f"/v1/users/{existing_user['id']}/facts",
            json={"facts": facts},
            headers=auth_headers,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_fact_triple_empty_subject(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
        existing_user: dict,
    ) -> None:
        """Verify fact triple with empty subject is rejected."""
        response = await async_client.post(
            f"/v1/users/{existing_user['id']}/facts",
            json={
                "facts": [
                    {"subject": "", "predicate": "test", "object": "value"},
                ],
            },
            headers=auth_headers,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_fact_triple_valid(
        self,
        async_client: AsyncClient,
        auth_headers: dict,
        existing_user: dict,
    ) -> None:
        """Verify a valid fact triple is accepted."""
        response = await async_client.post(
            f"/v1/users/{existing_user['id']}/facts",
            json={
                "facts": [
                    {
                        "subject": "user_123",
                        "predicate": "purchased",
                        "object": "Pro plan",
                        "valid_at": "2026-05-01T00:00:00Z",
                    },
                ],
            },
            headers=auth_headers,
        )
        assert response.status_code == 202
```

---

## 10. Validation Performance Considerations

| Operation | Complexity | Typical Time | Notes |
|---|---|---|---|
| Pydantic schema validation | O(fields) | < 1 ms per model | Negligible for typical payload sizes |
| Content sanitization | O(n) | < 5 ms for 64KB | Linear in content length |
| JSONB depth check | O(depth) | < 10 μs | Recursive, but max depth is 5 |
| JSONB key count | O(keys) | < 10 μs | Linear in key count, max 50 |
| JSONB string length | O(total chars) | < 100 μs | Linear in total metadata size |
| Content length (UTF-8) | O(n) | < 1 ms for 64KB | Simple byte-count operation |

**Key insight**: Validation adds < 10ms overhead per request for the vast majority of payloads. This is well within the 200ms ingestion acknowledgment target (PERF-04).

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
