# OpenAPI Specification Generation Guide

> **Phase:** Phase 0 (Foundation, Week 1–2) + Phase 4 (Dashboard & SDKs, Week 10–12)
> **Priority:** P0 (generation) / P1 (CI validation + SDK generation)
> **Requirements:** MAINT-03 (all public endpoints documented in OpenAPI 3.1 spec)
> **Handoff from:** Architect (ADR-007: OpenAPI & SDK Generation)
> **SRS Reference:** §6.4 MAINT-03, §5.8 SDKs, §8 API Specification

---

## 1. Overview

OpenZep uses **FastAPI's built-in OpenAPI generation** to produce a fully documented OpenAPI 3.1 specification. This single source of truth serves multiple purposes:

- **Interactive documentation** at `/docs` (Swagger UI) and `/redoc` (ReDoc)
- **Client SDK generation** for Python (`openapi-python-client`), TypeScript (`openapi-typescript`), and Go (`oapi-codegen`)
- **CI validation** to catch schema breaking changes via `openapi-diff`
- **API reference** for developers integrating with OpenZep
- **Published artifact** committed to `docs/openapi.yaml` in the repo

### 1.1 Pipeline Overview

```
FastAPI app (create_app)
       │
       ▼
OpenAPI 3.1 JSON (via get_openapi)
       │
       ├──► /docs         (Swagger UI — interactive)
       ├──► /redoc        (ReDoc — reference)
       ├──► /openapi.json (raw JSON)
       │
       ▼
YAML export (docs/openapi.yaml)
       │
       ├──► CI: redocly lint          ★ spec quality
       ├──► CI: openapi-diff          ★ breaking change detection
       ├──► Python SDK gen            ★ openapi-python-client
       ├──► TypeScript SDK gen        ★ openapi-typescript
       └──► Go SDK gen                ★ oapi-codegen
```

---

## 2. FastAPI OpenAPI Configuration

### 2.1 App Factory Configuration

```python
# services/api/main.py (inside create_app())

from fastapi import FastAPI


def create_app() -> FastAPI:
    settings = Settings()

    app = FastAPI(
        # ── OpenAPI metadata ──────────────────────────────────────────
        title="OpenZep API",
        version=settings.API_VERSION,       # "1.0.0"
        summary="Open-source temporal knowledge graph agent memory platform.",
        description="""
OpenZep is an open-source, self-hostable agent memory platform that provides
persistent, structured memory for LLM agents.

## Key Features

- **Episodic Memory**: Store conversation history with bi-temporal tracking
- **Knowledge Graph**: Extract entities, relationships, and facts from conversations
- **Hybrid Retrieval**: Combine vector similarity, BM25 full-text, and graph traversal
- **Context Assembly**: Build optimized context blocks for LLM injection
- **Multi-Tenant**: Fully isolated data per organization
- **GDPR Compliant**: Right to erasure, data portability, configurable retention

## Authentication

All API requests (except health endpoints and docs) require an API key
sent via the `Authorization` header:

```
Authorization: Bearer mg_live_<your_api_key>
```

API keys are generated through the admin dashboard or the `/v1/admin/organizations` endpoints.
Production keys are prefixed `mg_live_`, test/sandbox keys are prefixed `mg_test_`.

## Rate Limiting

API requests are rate-limited per key. The default limit is **100 requests per minute**.
Rate limit headers are included in all responses:

- `X-RateLimit-Limit`: Maximum requests per window
- `X-RateLimit-Remaining`: Remaining requests in current window
- `X-RateLimit-Reset`: Unix timestamp when the window resets

## Pagination

All list endpoints use cursor-based pagination:

- `?limit=50` — Items per page (default: 50, max: 200)
- `?cursor=<opaque>` — Cursor from previous response (omit for first page)
- `?include_total=true` — Include total count (expensive, default: false)

## Errors

All errors follow RFC 7807 Problem Details format.
""",
        # ── OpenAPI URLs (enabled in all environments) ────────────────
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",

        # ── Contact & License ─────────────────────────────────────────
        contact={
            "name": "TheLinkAI",
            "url": "https://thelink.ai",
            "email": "engineering@thelink.ai",
        },
        license_info={
            "name": "Apache 2.0",
            "identifier": "Apache-2.0",
            "url": "https://www.apache.org/licenses/LICENSE-2.0",
        },

        # ── Servers ───────────────────────────────────────────────────
        servers=[
            {
                "url": "https://api.OpenZep.dev",
                "description": "Production server",
            },
            {
                "url": "https://staging.OpenZep.dev",
                "description": "Staging server",
            },
            {
                "url": "http://localhost:8000",
                "description": "Local development",
            },
        ],

        # ── Swagger UI customization ──────────────────────────────────
        swagger_ui_parameters={
            "defaultModelsExpandDepth": -1,  # Hide schemas section initially
            "displayRequestDuration": True,   # Show request timing info
            "filter": True,                   # Enable endpoint search/filter
            "syntaxHighlight.theme": "monokai",
            "tryItOutEnabled": True,           # Enable Try It Out by default
        },
    )

    # ... register middleware, routers, exception handlers ...
    return app
```

### 2.2 Configuration Properties Summary

| Property | Value | Purpose |
|---|---|---|
| `title` | `"OpenZep API"` | Appears in Swagger UI title bar and OpenAPI `info.title` |
| `version` | `Settings().API_VERSION` (e.g., `"1.0.0"`) | Pinned to API release version |
| `summary` | Short tagline | OpenAPI 3.1 `info.summary` field |
| `description` | Full markdown description | Rendered in Swagger UI and ReDoc |
| `docs_url` | `"/docs"` | Swagger UI endpoint |
| `redoc_url` | `"/redoc"` | ReDoc endpoint |
| `openapi_url` | `"/openapi.json"` | Raw OpenAPI spec download |
| `contact` | TheLinkAI info | Who to contact for API support |
| `license_info` | Apache 2.0 | Open-source license reference |
| `servers` | Production, staging, local | Server dropdown in Swagger UI |

---

## 3. Custom OpenAPI Function with BearerAuth Security Scheme

By default, FastAPI does not include security schemes in the generated OpenAPI spec. We must add them via a custom `openapi()` function.

```python
# services/api/main.py

from fastapi.openapi.utils import get_openapi


def create_app() -> FastAPI:
    app = FastAPI(...)  # As above
    # ...
    # Register middleware, routers, exception handlers
    # ...

    # ── Custom OpenAPI schema with security scheme ────────────────────
    _configure_openapi_security(app, settings)

    return app


def _configure_openapi_security(app: FastAPI, settings: Settings) -> None:
    """Add BearerAuth security scheme to the OpenAPI spec.

    This replaces the default auto-generated schema with one that
    includes the BearerAuth security scheme and applies it globally
    to all endpoints (except public ones like health, docs).

    The BearerAuth scheme documents the API key authentication:
    - API keys must be sent as 'Authorization: Bearer <key>'
    - Production keys are prefixed 'mg_live_'
    - Test keys are prefixed 'mg_test_'
    """
    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema

        openapi_schema = get_openapi(
            title=app.title,
            version=app.version,
            summary=app.summary,
            description=app.description,
            routes=app.routes,
        )

        # Add BearerAuth security scheme
        openapi_schema.setdefault("components", {})
        openapi_schema["components"]["securitySchemes"] = {
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "API key",
                "description": (
                    "API key authentication.\n\n"
                    "All requests must include an API key in the "
                    "Authorization header:\n\n"
                    "```\n"
                    "Authorization: Bearer mg_live_<your_key>\n"
                    "```\n\n"
                    "**Key types:**\n"
                    "- Production keys: prefix `mg_live_`\n"
                    "- Test/sandbox keys: prefix `mg_test_`\n\n"
                    "Generate keys via the admin dashboard or "
                    "`POST /v1/admin/organizations/{org_id}/keys`."
                ),
            },
        }

        # Apply security globally — all endpoints require BearerAuth
        # Public endpoints (health, docs, openapi.json) are handled
        # by the AuthMiddleware (they skip auth verification).
        openapi_schema["security"] = [{"BearerAuth": []}]

        app.openapi_schema = openapi_schema
        return openapi_schema

    app.openapi = custom_openapi
```

---

## 4. Tags and Router Organization

### 4.1 Tag Assignment

Each domain router uses `tags` to organize endpoints in the OpenAPI spec:

```python
# routers/users.py
router = APIRouter(prefix="/users", tags=["Users"])

# routers/sessions.py
router = APIRouter(prefix="/users/{user_id}/sessions", tags=["Sessions"])

# routers/memory.py
router = APIRouter(prefix="/users/{user_id}", tags=["Memory"])

# routers/facts.py
router = APIRouter(prefix="/users/{user_id}", tags=["Facts"])

# routers/graph.py
router = APIRouter(prefix="/users/{user_id}/graph", tags=["Graph"])

# routers/search.py
router = APIRouter(prefix="/users/{user_id}", tags=["Search"])

# routers/context.py
router = APIRouter(prefix="/users/{user_id}", tags=["Context"])

# routers/admin.py
router = APIRouter(prefix="/admin", tags=["Admin"])

# routers/health.py
router = APIRouter(tags=["Health"])
```

### 4.2 Tag Metadata with Descriptions

```python
# Inside create_app(), pass to FastAPI:

app = FastAPI(
    openapi_tags=[
        {
            "name": "Users",
            "description": (
                "Create, list, update, and delete users. "
                "Users are the primary entity — all memory, "
                "sessions, and facts are scoped to a user. "
                "Each user is identified by a caller-chosen `external_id` "
                "that must be unique within an organization."
            ),
        },
        {
            "name": "Sessions",
            "description": (
                "Manage conversation sessions within a user. "
                "Sessions group messages into logical conversations. "
                "Sessions auto-close after 24 hours of inactivity. "
                "Message history can be retrieved per session."
            ),
        },
        {
            "name": "Memory",
            "description": (
                "Ingest messages into a user's memory. "
                "Messages are stored as episodes in the temporal "
                "knowledge graph and trigger async enrichment "
                "(entity extraction, fact extraction, embeddings). "
                "Returns HTTP 202 (Accepted) — enrichment runs asynchronously."
            ),
        },
        {
            "name": "Facts",
            "description": (
                "Manage extracted and manually added facts. "
                "Facts are (subject, predicate, object) triples "
                "with bi-temporal validity tracking (`valid_from`, `valid_to`). "
                "Facts can be auto-extracted from conversations or "
                "manually added via the API."
            ),
        },
        {
            "name": "Graph",
            "description": (
                "Query the temporal knowledge graph: entity nodes, "
                "typed relationships, and community summaries. "
                "The graph is powered by Graphiti with FalkorDB or Neo4j backend."
            ),
        },
        {
            "name": "Search",
            "description": (
                "Hybrid search across user memory combining "
                "vector similarity (pgvector cosine distance), "
                "BM25 full-text search (PostgreSQL GIN), and "
                "graph BFS traversal — merged with Reciprocal Rank Fusion."
            ),
        },
        {
            "name": "Context",
            "description": (
                "Assemble an optimized context block for LLM "
                "injection, including relevant facts, entity "
                "summaries, and recent messages. "
                "The context endpoint is optimized for sub-300ms "
                "p99 latency with warm Redis cache."
            ),
        },
        {
            "name": "Admin",
            "description": (
                "Organization and API key management. "
                "Requires admin-level API keys with admin scopes. "
                "Use these endpoints to create organizations, "
                "generate API keys, and manage tenant configuration."
            ),
        },
        {
            "name": "Health",
            "description": (
                "Liveness and readiness probes for Kubernetes "
                "and other orchestrators. These endpoints do not "
                "require authentication."
            ),
        },
    ],
)
```

---

## 5. Endpoint Documentation Best Practices

### 5.1 Canonical Endpoint Docstring

Every endpoint must include:
1. **Clear docstring** (becomes the OpenAPI `description`)
2. **Proper response model** via `response_model=`
3. **Error responses** via `responses=` parameter
4. **Summary** via `summary=` parameter

```python
@router.post(
    "",
    response_model=UserResponseWithStats,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new user",
    response_description="The created user with aggregated stats.",
)
async def create_user(
    request: CreateUserRequest,
    service: UserService = Depends(get_user_service),
    org: Organization = Depends(get_current_organization),
) -> UserResponseWithStats:
    """Create a new user within the authenticated organization.

    The `external_id` is your identifier for this user (e.g., user ID
    from your application). It must be unique within your organization.

    **Auto-creation**: If `POST /memory` references a user that doesn't
    exist, the user is automatically created (configurable via the
    `USER_AUTO_CREATE` env var).

    **Rate limiting**: Standard rate limits apply (100 req/min per key).

    **Example request:**
    ```json
    {
        "external_id": "user_abc123",
        "name": "Alice",
        "email": "alice@example.com",
        "metadata": {
            "signup_date": "2026-01-15",
            "plan": "pro"
        }
    }
    ```

    Returns the created user with initial stats (message_count=0, fact_count=0).
    """
    return await service.create_user(
        organization_id=org.id,
        request=request,
    )
```

### 5.2 Error Responses in OpenAPI

Document error codes using the `responses` parameter:

```python
@router.get(
    "/{user_id}",
    response_model=UserResponseWithStats,
    responses={
        404: {
            "description": "User not found",
            "content": {
                "application/problem+json": {
                    "example": {
                        "type": "https://api.OpenZep.dev/errors/resource_not_found",
                        "title": "Resource Not Found",
                        "status": 404,
                        "detail": "User '550e8400-e29b-41d4-a716-446655440000' not found",
                        "instance": "req_01j9xmf...",
                    },
                },
            },
        },
        401: {
            "description": "Missing or invalid API key",
        },
        429: {
            "description": "Rate limit exceeded",
            "headers": {
                "Retry-After": {
                    "description": "Seconds until rate limit resets",
                    "schema": {"type": "integer"},
                },
            },
        },
    },
)
async def get_user(
    user_id: UUID = Path(..., description="Internal OpenZep user UUID"),
    service: UserService = Depends(get_user_service),
    org: Organization = Depends(get_current_organization),
) -> UserResponseWithStats:
    """Get a user by their internal UUID, including aggregated stats.

    The response includes the user's profile information along with
    summary statistics (message count, fact count, session count).

    **Rate limiting**: Standard rate limits apply.
    """
    return await service.get_user(user_id=str(user_id), organization_id=org.id)
```

---

## 6. Pydantic Schema to OpenAPI Schema Mapping

FastAPI automatically converts Pydantic models to OpenAPI schema components. Use Pydantic's metadata to enrich the spec:

```python
from pydantic import BaseModel, Field
from typing import Optional


class CreateUserRequest(BaseModel):
    """Request body for creating a user.

    FastAPI converts this to an OpenAPI schema component
    with properties, types, constraints, and examples.
    """
    external_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description=(
            "Caller-chosen unique identifier for the user, "
            "scoped to the organization."
        ),
        json_schema_extra={
            "example": "user_abc123",
        },
    )
    name: Optional[str] = Field(
        None,
        max_length=1024,
        description="Display name for the user.",
        json_schema_extra={
            "example": "Alice Smith",
        },
    )
    email: Optional[str] = Field(
        None,
        max_length=1024,
        description="Email address of the user.",
        json_schema_extra={
            "example": "alice@example.com",
        },
    )
    metadata: Optional[dict] = Field(
        None,
        description=(
            "Arbitrary caller-defined metadata (JSON object). "
            "Max 5 levels deep, max 50 keys, 1KB per string value."
        ),
        json_schema_extra={
            "example": {"plan": "pro", "signup_date": "2026-01-15"},
        },
    )
```

This generates the following OpenAPI schema:

```yaml
CreateUserRequest:
  type: object
  required:
    - external_id
  properties:
    external_id:
      type: string
      minLength: 1
      maxLength: 255
      description: Caller-chosen unique identifier...
      example: user_abc123
    name:
      type: string
      nullable: true
      maxLength: 1024
      description: Display name for the user.
      example: Alice Smith
    email:
      type: string
      nullable: true
      format: email
      maxLength: 1024
    metadata:
      type: object
      nullable: true
      description: Arbitrary caller-defined metadata...
      example:
        plan: pro
        signup_date: "2026-01-15"
```

---

## 7. Pydantic Config for Better OpenAPI Output

```python
class UserResponse(BaseModel):
    """Response schema with OpenAPI-friendly configuration."""

    model_config = ConfigDict(
        from_attributes=True,       # Allow construction from ORM objects
        json_schema_extra={         # Extra OpenAPI metadata
            "example": {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "external_id": "user_abc123",
                "name": "Alice Smith",
                "email": "alice@example.com",
                "created_at": "2026-01-15T10:30:00Z",
                "updated_at": "2026-06-05T14:22:00Z",
            },
        },
    )

    id: UUID
    external_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
```

---

## 8. CI Validation Pipeline

### 8.1 Validate OpenAPI Spec Generation

```yaml
# .gitlab-ci.yml — OpenAPI jobs

stages:
  - validate
  - test
  - build
  - publish

validate-openapi:
  stage: validate
  image: python:3.12-slim
  script:
    - pip install -r services/api/requirements.txt
    - |
      python -c "
      from services.api.main import create_app
      app = create_app()
      spec = app.openapi()
      assert spec is not None, 'OpenAPI spec generation failed'
      assert spec['info']['title'] == 'OpenZep API'
      assert spec['info']['version'] == '1.0.0'
      assert '/v1/users' in str(spec['paths']), 'Missing /v1/users endpoint'
      print(f'OpenAPI spec valid: {len(spec[\"paths\"])} paths, '
            f'{len(spec[\"components\"][\"schemas\"])} schemas')
      "
  rules:
    - if: $CI_MERGE_REQUEST_ID
    - if: $CI_COMMIT_BRANCH == "main"
```

### 8.2 Breaking Change Detection (openapi-diff)

```yaml
validate-openapi-breaking:
  stage: validate
  image: node:20
  before_script:
    - pip install -r services/api/requirements.txt
    - npm install -g @openapi-contrib/openapi-diff
  script:
    # Generate current spec
    - python -c "
      from services.api.main import create_app
      import json
      spec = create_app().openapi()
      with open('openapi_current.json', 'w') as f:
          json.dump(spec, f, indent=2)
      "
    # Compare with the committed spec
    - openapi-diff docs/openapi.yaml openapi_current.json --markdown report.md
    # Fail if there are breaking changes
    - |
      if grep -q "breaking" report.md; then
        echo "❌ Breaking changes detected in OpenAPI spec!"
        cat report.md
        exit 1
      fi
    - echo "✅ No breaking changes detected"
  rules:
    - if: $CI_MERGE_REQUEST_ID
```

### 8.3 Lint OpenAPI Spec (redocly)

```yaml
lint-openapi:
  stage: validate
  image: node:20
  script:
    - npm install -g @redocly/cli
    - redocly lint docs/openapi.yaml
  rules:
    - if: $CI_MERGE_REQUEST_ID
    - if: $CI_COMMIT_BRANCH == "main"
```

### 8.4 Redocly Configuration

```yaml
# redocly.yaml

extends:
  - recommended

rules:
  operation-2xx-response: warn
  operation-4xx-response: error
  path-excludes-patterns:
    - /health
    - /ready
  no-server-example.com: error
  no-unused-components: warn
  no-ambiguous-paths: error
  security-defined: error
  spec: error
```

---

## 9. SDK Code Generation

### 9.1 Python SDK (openapi-python-client)

```bash
# Install
pip install openapi-python-client

# Generate Python client from OpenAPI spec
openapi-python-client generate \
    --path docs/openapi.yaml \
    --output-path packages/sdk-python/openzep_client \
    --overwrite

# The generated client provides typed methods for every endpoint.
# Wrap it in the SDK layer for a cleaner developer experience:
```

```python
# packages/sdk-python/OpenZep/_client.py

from openzep_client import Client as GeneratedClient
from openzep_client.api.users import create_user, list_users
from openzep_client.models import CreateUserRequest as GeneratedCreateUserRequest
from openzep_client.types import Response as GeneratedResponse


class MemGraphClient:
    """High-level Python SDK wrapping the auto-generated OpenAPI client.

    Usage:
        client = MemGraphClient(api_key="mg_live_...")
        user = await client.create_user(external_id="user_123", name="Alice")
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.OpenZep.dev",
    ) -> None:
        self._client = GeneratedClient(
            base_url=base_url,
            token=api_key,
            timeout=30,
        )

    async def create_user(
        self,
        external_id: str,
        name: str | None = None,
        email: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Create a user with a clean, type-safe interface."""
        request = GeneratedCreateUserRequest(
            external_id=external_id,
            name=name,
            email=email,
            metadata=metadata,
        )
        response = await create_user.async_(
            client=self._client,
            body=request,
        )
        return response.parsed
```

### 9.2 TypeScript SDK (openapi-typescript)

```bash
# Generate TypeScript types from OpenAPI spec
npx openapi-typescript docs/openapi.yaml \
    --output packages/sdk-typescript/src/generated.ts

# This generates fully typed interfaces for all paths and schemas
```

```typescript
// packages/sdk-typescript/src/client.ts
import createClient from "openapi-fetch";
import type { paths } from "./generated";

export class MemGraphClient {
  private client: ReturnType<typeof createClient<paths>>;

  constructor(
    apiKey: string,
    baseUrl = "https://api.OpenZep.dev",
  ) {
    this.client = createClient<paths>({
      baseUrl,
      headers: { Authorization: `Bearer ${apiKey}` },
    });
  }

  async createUser(externalId: string, name?: string) {
    const { data, error } = await this.client.POST("/v1/users", {
      body: { external_id: externalId, name },
    });
    if (error) throw new Error(error.detail);
    return data;
  }

  async listUsers(cursor?: string, limit = 50) {
    const { data, error } = await this.client.GET("/v1/users", {
      params: {
        query: { cursor, limit },
      },
    });
    if (error) throw new Error(error.detail);
    return data;
  }
}
```

### 9.3 Go SDK (oapi-codegen)

```bash
# Install
go install github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@latest

# Generate Go client from OpenAPI spec
oapi-codegen \
    -package OpenZep \
    -generate types,client \
    docs/openapi.yaml \
    > packages/sdk-go/OpenZep/client.gen.go
```

```go
// packages/sdk-go/OpenZep/client.go
package OpenZep

import (
    "context"
    "net/http"
)

type Client struct {
    *ClientWithResponses
}

func NewClient(apiKey, baseURL string) *Client {
    c, err := NewClientWithResponses(
        baseURL,
        WithRequestEditorFn(func(ctx context.Context, req *http.Request) error {
            req.Header.Set("Authorization", "Bearer "+apiKey)
            return nil
        }),
    )
    if err != nil {
        panic(err) // Should never happen with valid config
    }
    return &Client{c}
}

// CreateUser creates a new user in the organization.
func (c *Client) CreateUser(ctx context.Context, req CreateUserRequest) (*UserResponse, error) {
    resp, err := c.CreateUserWithResponse(ctx, req)
    if err != nil {
        return nil, err
    }
    if resp.JSON201 == nil {
        return nil, fmt.Errorf("unexpected response: %d", resp.HTTPResponse.StatusCode)
    }
    return resp.JSON201, nil
}
```

---

## 10. Publishing the OpenAPI Spec

### 10.1 Generation Script

```python
# scripts/generate_openapi.py

"""Generate the OpenAPI spec and save it to docs/openapi.yaml.

Usage:
    python scripts/generate_openapi.py
    python scripts/generate_openapi.py --output docs/openapi.yaml
"""

import argparse
import yaml
import sys
from pathlib import Path

# Ensure the services package is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "services"))

from api.main import create_app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate OpenAPI spec from FastAPI app."
    )
    parser.add_argument(
        "--output",
        default="docs/openapi.yaml",
        help="Output file path (default: docs/openapi.yaml)",
    )
    args = parser.parse_args()

    # Create the FastAPI app
    app = create_app()

    # Generate the OpenAPI schema
    openapi_schema = app.openapi()

    # Write YAML
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        yaml.dump(openapi_schema, f, default_flow_style=False, sort_keys=False)

    print(f"✅ OpenAPI spec written to {output_path}")
    print(f"   Paths: {len(openapi_schema['paths'])}")
    print(f"   Schemas: {len(openapi_schema.get('components', {}).get('schemas', {}))}")


if __name__ == "__main__":
    main()
```

### 10.2 CI: Auto-Update on Release

```yaml
update-openapi-spec:
  stage: publish
  image: python:3.12-slim
  before_script:
    - pip install pyyaml
  script:
    - python scripts/generate_openapi.py --output docs/openapi.yaml
    - git config user.email "ci@OpenZep.dev"
    - git config user.name "OpenZep CI"
    - git add docs/openapi.yaml
    - git commit -m "docs(openapi): update OpenAPI spec to v${CI_COMMIT_TAG} [skip ci]" || true
    - git push
  rules:
    - if: $CI_COMMIT_TAG =~ /^v\d+\.\d+\.\d+$/
```

---

## 11. Validation Script

```python
# scripts/validate_openapi.py

"""Validate the generated OpenAPI spec against structural requirements."""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "services"))

from api.main import create_app


# Required paths that must exist in the OpenAPI spec
REQUIRED_PATHS = [
    "/v1/users",
    "/v1/users/{user_id}",
    "/v1/users/{user_id}/sessions",
    "/v1/users/{user_id}/sessions/{session_id}",
    "/v1/users/{user_id}/sessions/{session_id}/messages",
    "/v1/users/{user_id}/memory",
    "/v1/users/{user_id}/context",
    "/v1/users/{user_id}/facts",
    "/v1/users/{user_id}/graph/nodes",
    "/v1/users/{user_id}/graph/edges",
    "/v1/users/{user_id}/search",
    "/health",
    "/ready",
    "/v1/admin/organizations",
]


def validate() -> None:
    app = create_app()
    spec = app.openapi()

    errors: list[str] = []

    # Check spec version
    if not spec.get("openapi", "").startswith("3."):
        errors.append("Spec must be OpenAPI 3.x")

    # Check required metadata
    if spec.get("info", {}).get("title") != "OpenZep API":
        errors.append("info.title must be 'OpenZep API'")
    if not spec.get("info", {}).get("version"):
        errors.append("info.version must be set")

    # Check required paths
    spec_paths = set(spec.get("paths", {}).keys())
    for path in REQUIRED_PATHS:
        if path not in spec_paths:
            errors.append(f"Required path missing: {path}")

    # Check security scheme
    components = spec.get("components", {})
    if "BearerAuth" not in components.get("securitySchemes", {}):
        errors.append("Missing BearerAuth security scheme")

    # Check servers
    if not spec.get("servers"):
        errors.append("At least one server must be configured")

    # Report
    if errors:
        print("❌ OpenAPI validation failed:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)
    else:
        print("✅ OpenAPI spec is valid")
        print(f"   Paths: {len(spec['paths'])}")
        print(f"   Schemas: {len(components.get('schemas', {}))}")
        print(f"   Security schemes: {list(components.get('securitySchemes', {}).keys())}")


if __name__ == "__main__":
    validate()
```

---

## 12. Testing the OpenAPI Spec

```python
# tests/unit/test_openapi.py

import pytest


class TestOpenAPISpec:
    """Tests for OpenAPI spec generation and correctness."""

    @pytest.mark.asyncio
    async def test_openapi_spec_generation(
        self, async_client: AsyncClient,
    ) -> None:
        """Verify OpenAPI spec is valid and contains expected endpoints."""
        response = await async_client.get("/openapi.json")
        assert response.status_code == 200
        spec = response.json()

        # Spec version
        assert spec["openapi"].startswith("3.")
        assert spec["info"]["title"] == "OpenZep API"
        assert spec["info"]["version"] == "1.0.0"

        # All required paths exist
        paths = spec["paths"]
        required = [
            "/v1/users",
            "/v1/users/{user_id}",
            "/v1/users/{user_id}/sessions",
            "/v1/users/{user_id}/sessions/{session_id}",
            "/v1/users/{user_id}/sessions/{session_id}/messages",
            "/v1/users/{user_id}/memory",
            "/v1/users/{user_id}/context",
            "/v1/users/{user_id}/facts",
            "/v1/users/{user_id}/graph/nodes",
            "/v1/users/{user_id}/graph/edges",
            "/v1/users/{user_id}/search",
            "/health",
            "/ready",
            "/v1/admin/organizations",
        ]
        for path in required:
            assert path in paths, f"Required path missing: {path}"

    @pytest.mark.asyncio
    async def test_openapi_security_scheme(
        self, async_client: AsyncClient,
    ) -> None:
        """Verify BearerAuth security scheme is configured."""
        response = await async_client.get("/openapi.json")
        spec = response.json()

        assert "securitySchemes" in spec["components"]
        assert "BearerAuth" in spec["components"]["securitySchemes"]
        auth = spec["components"]["securitySchemes"]["BearerAuth"]
        assert auth["type"] == "http"
        assert auth["scheme"] == "bearer"

    @pytest.mark.asyncio
    async def test_openapi_swagger_ui_accessible(
        self, async_client: AsyncClient,
    ) -> None:
        """Verify Swagger UI loads."""
        response = await async_client.get("/docs")
        assert response.status_code == 200
        assert "swagger" in response.text.lower()

    @pytest.mark.asyncio
    async def test_openapi_redoc_accessible(
        self, async_client: AsyncClient,
    ) -> None:
        """Verify ReDoc loads."""
        response = await async_client.get("/redoc")
        assert response.status_code == 200
        assert "redoc" in response.text.lower()

    @pytest.mark.asyncio
    async def test_openapi_spec_no_pii_in_schemas(
        self, async_client: AsyncClient,
    ) -> None:
        """Verify no sensitive field names appear in the spec.

        This catches accidental exposure of internal field names
        that could aid attackers.
        """
        response = await async_client.get("/openapi.json")
        spec = response.json()
        spec_str = json.dumps(spec).lower()

        sensitive_terms = [
            "password", "secret", "token", "api_key",
            "credit_card", "ssn", "dob", "private_key",
        ]
        for term in sensitive_terms:
            assert term not in spec_str, (
                f"Sensitive term '{term}' found in OpenAPI spec"
            )

    @pytest.mark.asyncio
    async def test_openapi_schema_examples_present(
        self, async_client: AsyncClient,
    ) -> None:
        """Verify request/response schemas have example values."""
        response = await async_client.get("/openapi.json")
        spec = response.json()
        schemas = spec.get("components", {}).get("schemas", {})

        # Check that at least some schemas have examples
        schemas_with_examples = 0
        for name, schema in schemas.items():
            if "example" in schema or any(
                prop.get("example") for prop in schema.get("properties", {}).values()
            ):
                schemas_with_examples += 1

        assert schemas_with_examples > 0, (
            "No schemas have example values"
        )

    @pytest.mark.asyncio
    async def test_openapi_endpoint_summaries(
        self, async_client: AsyncClient,
    ) -> None:
        """Verify all endpoints have summaries (not just auto-generated ones)."""
        response = await async_client.get("/openapi.json")
        spec = response.json()

        endpoints_without_summary = []
        for path, methods in spec.get("paths", {}).items():
            for method, details in methods.items():
                if "summary" not in details:
                    endpoints_without_summary.append(f"{method.upper()} {path}")

        # Health endpoint is allowed to have auto-generated summary
        allowed_no_summary = {"GET /health", "GET /ready"}

        missing = [
            e for e in endpoints_without_summary
            if e not in allowed_no_summary
        ]
        assert not missing, (
            f"Endpoints without summaries: {missing}"
        )
```

---

## 13. CI Configuration Summary

```yaml
# .gitlab-ci.yml — complete OpenAPI section

stages:
  - lint
  - validate
  - test
  - build
  - publish

openapi-lint:
  stage: lint
  image: node:20
  script:
    - npm install -g @redocly/cli
    - redocly lint docs/openapi.yaml
  rules:
    - if: $CI_MERGE_REQUEST_ID
    - if: $CI_COMMIT_BRANCH == "main"

openapi-validate:
  stage: validate
  image: python:3.12-slim
  before_script:
    - pip install -r services/api/requirements.txt pyyaml
  script:
    - python scripts/validate_openapi.py
  rules:
    - if: $CI_MERGE_REQUEST_ID
    - if: $CI_COMMIT_BRANCH == "main"

openapi-breaking-check:
  stage: validate
  image: node:20
  before_script:
    - pip install -r services/api/requirements.txt
    - npm install -g @openapi-contrib/openapi-diff
  script:
    - python scripts/generate_openapi.py --output /tmp/openapi_new.yaml
    - openapi-diff docs/openapi.yaml /tmp/openapi_new.yaml --markdown /tmp/diff.md
    - |
      if grep -q "breaking" /tmp/diff.md; then
        echo "❌ Breaking changes detected!"
        cat /tmp/diff.md
        exit 1
      fi
    - echo "✅ No breaking changes detected"
  rules:
    - if: $CI_MERGE_REQUEST_ID

openapi-publish:
  stage: publish
  image: python:3.12-slim
  before_script:
    - pip install pyyaml
  script:
    - python scripts/generate_openapi.py --output docs/openapi.yaml
    - git config user.email "ci@OpenZep.dev"
    - git config user.name "OpenZep CI"
    - git add docs/openapi.yaml
    - git commit -m "docs(openapi): update OpenAPI spec [skip ci]" || true
    - git push
  rules:
    - if: $CI_COMMIT_TAG =~ /^v\d+\.\d+\.\d+$/
```

---

## 14. OpenAPI Committed to Repo Checklist

- [ ] `docs/openapi.yaml` is generated and committed
- [ ] `redocly lint` passes without errors
- [ ] All required paths are present (verified in CI)
- [ ] Security scheme (`BearerAuth`) is documented
- [ ] All endpoints have summaries and descriptions
- [ ] Error responses (4xx, 5xx) are documented
- [ ] Request/response schemas have example values
- [ ] Pydantic field descriptions are informative
- [ ] Servers section includes dev, staging, production
- [ ] `scripts/generate_openapi.py` exists in repo
- [ ] CI validation jobs are configured in `.gitlab-ci.yml`

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
