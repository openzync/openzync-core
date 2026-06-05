# OpenAPI Specification Generation Guide

> **Phase:** Phase 0 (Foundation) + Phase 4 (Dashboard & SDKs)
> **Priority:** P0 (generation) / P1 (CI validation + SDK generation)
> **Requirements:** MAINT-03 (all public endpoints documented in OpenAPI 3.1 spec), SRS §10 (Phase 4)
> **Handoff from:** Architect (ADR-007: OpenAPI & SDK Generation)

---

## 1. Overview

MemGraph uses **FastAPI's built-in OpenAPI generation** to produce a fully documented OpenAPI 3.1 specification. This spec serves as:

- **Interactive documentation** at `/docs` (Swagger UI) and `/redoc` (ReDoc)
- **Client SDK generation** for Python, TypeScript, and Go
- **CI validation** to catch schema breaking changes
- **API reference** for developers integrating with MemGraph

---

## 2. FastAPI Configuration

### 2.1 App Factory Configuration

```python
# services/api/main.py

from fastapi import FastAPI
from typing import Optional


def create_app() -> FastAPI:
    settings = Settings()

    app = FastAPI(
        # ── OpenAPI metadata ──────────────────────────────────────
        title="MemGraph API",
        version=settings.API_VERSION,  # "1.0.0"
        summary="Open-source temporal knowledge graph agent memory platform.",
        description="""
MemGraph is an open-source, self-hostable agent memory platform that provides
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
        # ── OpenAPI URLs ──────────────────────────────────────────
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",

        # ── Contact & License ─────────────────────────────────────
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

        # ── Servers ───────────────────────────────────────────────
        servers=[
            {
                "url": "https://api.memgraph.dev",
                "description": "Production server",
            },
            {
                "url": "https://staging.memgraph.dev",
                "description": "Staging server",
            },
            {
                "url": "http://localhost:8000",
                "description": "Local development",
            },
        ],

        # ── Swagger UI customization ──────────────────────────────
        swagger_ui_parameters={
            "defaultModelsExpandDepth": -1,  # Hide schemas section by default
            "displayRequestDuration": True,   # Show request duration
            "filter": True,                   # Enable endpoint filtering
            "syntaxHighlight.theme": "monokai",
        },
    )

    # ... register middleware, routers, exception handlers ...
    return app
```

### 2.2 OpenAPI Configuration Properties

| Property | Value | Purpose |
|---|---|---|
| `title` | `"MemGraph API"` | Appears in Swagger UI title bar and OpenAPI `info.title` |
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

## 3. Security Scheme Configuration

FastAPI's OpenAPI supports security schemes via the `security_schemes` parameter on the app's OpenAPI metadata.

```python
# services/api/main.py (inside create_app)

# Register security scheme for OpenAPI
security_scheme = {
    "bearerAuth": {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "API key",
        "description": "API key authentication. "
                       "Prefix: 'mg_live_' (production) or 'mg_test_' (sandbox). "
                       "Example: 'mg_live_abc123def456'",
    },
}

# Override the default OpenAPI schema to include security
from fastapi.openapi.utils import get_openapi


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title="MemGraph API",
        version=settings.API_VERSION,
        summary="Open-source temporal knowledge graph agent memory platform.",
        description=app.description,
        routes=app.routes,
    )

    # Add security scheme
    openapi_schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "API key",
            "description": "API key authentication. "
                           "All requests must include an API key in the "
                           "Authorization header:\n\n"
                           "`Authorization: Bearer mg_live_<your_key>`\n\n"
                           "Generate keys via the admin dashboard or "
                           "`POST /v1/admin/organizations/{org_id}/keys`.",
        }
    }

    # Apply security globally (all endpoints except public ones)
    openapi_schema["security"] = [{"BearerAuth": []}]

    app.openapi_schema = openapi_schema
    return openapi_schema


app.openapi = custom_openapi
```

---

## 4. Tags and Router Organization

Each domain router has a `tags` list that maps to OpenAPI tag groups:

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
router = APIRouter(prefix="", tags=["Health"])
```

### 4.1 Tag Metadata

Add tag descriptions for better documentation:

```python
# In main.py, pass tag metadata to FastAPI:

app = FastAPI(
    # ...
    openapi_tags=[
        {
            "name": "Users",
            "description": "Create, list, update, and delete users. "
                          "Users are the primary entity — all memory, "
                          "sessions, and facts are scoped to a user.",
        },
        {
            "name": "Sessions",
            "description": "Manage conversation sessions within a user. "
                          "Sessions group messages into logical conversations. "
                          "Sessions auto-close after 24 hours of inactivity.",
        },
        {
            "name": "Memory",
            "description": "Ingest messages into a user's memory. "
                          "Messages are stored as episodes in the temporal "
                          "knowledge graph and trigger async enrichment "
                          "(entity extraction, fact extraction, embeddings).",
        },
        {
            "name": "Facts",
            "description": "Manage extracted and manually added facts. "
                          "Facts are (subject, predicate, object) triples "
                          "with bi-temporal validity tracking.",
        },
        {
            "name": "Graph",
            "description": "Query the temporal knowledge graph: entity nodes, "
                          "relationships, and community summaries.",
        },
        {
            "name": "Search",
            "description": "Hybrid search across user memory combining "
                          "vector similarity, BM25 full-text, and graph traversal.",
        },
        {
            "name": "Context",
            "description": "Assemble an optimized context block for LLM "
                          "injection, including relevant facts, entity "
                          "summaries, and recent messages.",
        },
        {
            "name": "Admin",
            "description": "Organization and API key management. "
                          "Requires admin-level API keys with admin scopes.",
        },
        {
            "name": "Health",
            "description": "Liveness and readiness probes for Kubernetes "
                          "and other orchestrators.",
        },
    ],
)
```

---

## 5. Endpoint Documentation Best Practices

Every endpoint should have:
1. A clear docstring (becomes the OpenAPI `description`)
2. Proper response model annotations
3. Status code annotations

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

    **Rate limiting**: Standard rate limits apply.

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

### 5.1 Error Responses in OpenAPI

Document error responses using `responses` parameter:

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
                        "type": "https://api.memgraph.dev/errors/resource_not_found",
                        "title": "Resource Not Found",
                        "status": 404,
                        "detail": "User '550e8400-e29b-41d4-a716-446655440000' not found",
                        "instance": "req_01j9xmf...",
                    }
                }
            },
        },
        401: {
            "description": "Missing or invalid API key",
        },
        429: {
            "description": "Rate limit exceeded",
        },
    },
)
async def get_user(
    user_id: UUID = Path(..., description="Internal MemGraph user UUID"),
    # ...
) -> UserResponseWithStats:
    """Get a user by their internal UUID, including aggregated stats."""
    ...
```

---

## 6. Pydantic Schema → OpenAPI Schema

FastAPI automatically converts Pydantic models to OpenAPI schema components. Use Pydantic's Field metadata to enrich the spec:

```python
class CreateUserRequest(BaseModel):
    external_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Caller-chosen unique identifier for the user, scoped to the organization.",
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
        description="Arbitrary caller-defined metadata (JSON object).",
        json_schema_extra={
            "example": {"plan": "pro", "signup_date": "2026-01-15"},
        },
    )
```

---

## 7. OpenAPI Export Endpoints

FastAPI generates these endpoints automatically:

| URL | Description |
|---|---|
| `/openapi.json` | Raw OpenAPI 3.1 spec (JSON) |
| `/docs` | Swagger UI (interactive) |
| `/redoc` | ReDoc (reference documentation) |

### 7.1 Custom OpenAPI Endpoint (for commit to repo)

```python
# routers/openapi.py

from fastapi import APIRouter
from fastapi.responses import JSONResponse


@router.get("/openapi.yaml", include_in_schema=False)
async def get_openapi_yaml():
    """Return the OpenAPI spec in YAML format (for CI publishing)."""
    from fastapi.openapi.utils import get_openapi
    import yaml

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    yaml_str = yaml.dump(openapi_schema, default_flow_style=False)
    return Response(
        content=yaml_str,
        media_type="text/yaml",
        headers={
            "Content-Disposition": "attachment; filename=openapi.yaml",
        },
    )
```

---

## 8. CI Validation

### 8.1 Validate OpenAPI Spec Generation

Add this to your CI pipeline to ensure the OpenAPI spec is always valid:

```yaml
# .gitlab-ci.yml

stages:
  - validate
  - test
  - build
  - deploy

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
      assert spec['info']['title'] == 'MemGraph API'
      assert spec['info']['version'] == '1.0.0'
      assert '/v1/users' in str(spec['paths']), 'Missing /v1/users endpoint'
      print(f'OpenAPI spec valid: {len(spec[\"paths\"])} paths, '
            f'{len(spec[\"components\"][\"schemas\"])} schemas')
      "
  rules:
    - if: $CI_MERGE_REQUEST_ID
    - if: $CI_COMMIT_BRANCH == "main"
```

### 8.2 Validate OpenAPI Spec Diff (Breaking Change Detection)

```yaml
validate-openapi-breaking:
  stage: validate
  image: node:20
  script:
    # Install openapi-diff tool
    - npm install -g @openapi-contrib/openapi-diff
    # Generate current spec
    - pip install -r services/api/requirements.txt
    - python -c "
      from services.api.main import create_app
      import json
      spec = create_app().openapi()
      with open('openapi_current.json', 'w') as f:
          json.dump(spec, f)
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
```

### 8.3 Lint OpenAPI Spec

```yaml
lint-openapi:
  stage: validate
  image: node:20
  script:
    - npm install -g @redocly/cli
    - redocly lint docs/openapi.yaml
  rules:
    - if: $CI_MERGE_REQUEST_ID
```

---

## 9. SDK Code Generation

### 9.1 Python SDK (openapi-python-client)

```bash
# Generate Python client from OpenAPI spec
pip install openapi-python-client
openapi-python-client generate --path docs/openapi.yaml \
    --output-path packages/sdk-python/memgraph_client \
    --overwrite

# The generated client is published to PyPI as memgraph-py
```

**Customization**: The generated client is wrapped in the SDK layer:

```python
# packages/sdk-python/memgraph/_client.py

from memgraph_client import Client as GeneratedClient
from memgraph_client.api.users import create_user, list_users
from memgraph_client.models import CreateUserRequest as GeneratedCreateUserRequest


class MemGraphClient:
    """High-level SDK wrapping the auto-generated OpenAPI client."""

    def __init__(self, api_key: str, base_url: str = "https://api.memgraph.dev"):
        self._client = GeneratedClient(
            base_url=base_url,
            token=api_key,
        )

    async def create_user(self, external_id: str, **kwargs):
        """Create a user with a clean interface."""
        request = GeneratedCreateUserRequest(external_id=external_id, **kwargs)
        return await create_user.async_(client=self._client, body=request)
```

### 9.2 TypeScript SDK (openapi-typescript)

```bash
# Generate TypeScript types from OpenAPI spec
npx openapi-typescript docs/openapi.yaml \
    --output packages/sdk-typescript/src/generated.ts

# This generates typed interfaces for all request/response models
```

```typescript
// packages/sdk-typescript/src/client.ts
import createClient from "openapi-fetch";
import type { paths } from "./generated";

export class MemGraphClient {
  private client: ReturnType<typeof createClient<paths>>;

  constructor(apiKey: string, baseUrl = "https://api.memgraph.dev") {
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
}
```

### 9.3 Go SDK (oapi-codegen)

```bash
# Generate Go client from OpenAPI spec
go install github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@latest
oapi-codegen -package memgraph -generate types,client docs/openapi.yaml \
    > packages/sdk-go/memgraph/client.gen.go
```

```go
// packages/sdk-go/memgraph/client.go
package memgraph

import (
    "context"
    "net/http"
)

type Client struct {
    *ClientWithResponses
}

func NewClient(apiKey, baseURL string) *Client {
    c, _ := NewClientWithResponses(
        baseURL,
        WithRequestEditorFn(func(ctx context.Context, req *http.Request) error {
            req.Header.Set("Authorization", "Bearer "+apiKey)
            return nil
        }),
    )
    return &Client{c}
}
```

---

## 10. Publishing the OpenAPI Spec

### 10.1 Commit to Repository

The OpenAPI spec is committed to the repository at `docs/openapi.yaml`:

```bash
# Generate and save the spec
python -c "
from services.api.main import create_app
import yaml
app = create_app()
with open('docs/openapi.yaml', 'w') as f:
    yaml.dump(app.openapi(), f, default_flow_style=False)
"
```

### 10.2 CI Step: Auto-Update on Release

```yaml
update-openapi-spec:
  stage: build
  image: python:3.12-slim
  script:
    - pip install -r services/api/requirements.txt
    - python scripts/generate_openapi.py
    - git add docs/openapi.yaml
    - git commit -m "docs(openapi): update OpenAPI spec to v${CI_COMMIT_TAG}"
    - git push
  rules:
    - if: $CI_COMMIT_TAG =~ /^v\d+\.\d+\.\d+$/
```

---

## 11. OpenAPI Spec Structure (Example)

The generated spec at `/openapi.json` follows this structure:

```yaml
openapi: 3.1.0
info:
  title: MemGraph API
  version: 1.0.0
  summary: Open-source temporal knowledge graph agent memory platform.
  description: |
    MemGraph is an open-source, self-hostable agent memory platform...
  contact:
    name: TheLinkAI
    url: https://thelink.ai
    email: engineering@thelink.ai
  license:
    name: Apache 2.0
    identifier: Apache-2.0
    url: https://www.apache.org/licenses/LICENSE-2.0
servers:
  - url: https://api.memgraph.dev
    description: Production server
  - url: http://localhost:8000
    description: Local development
paths:
  /v1/users:
    get:
      tags: [Users]
      summary: List users
      parameters:
        - name: limit
          in: query
          schema: { type: integer, default: 50, maximum: 200 }
        - name: cursor
          in: query
          schema: { type: string, nullable: true }
      responses:
        "200":
          description: Paginated list of users
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/UserListResponse'
    post:
      tags: [Users]
      summary: Create a new user
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateUserRequest'
      responses:
        "201":
          description: The created user with stats
        "409":
          description: User with this external_id already exists
components:
  securitySchemes:
    BearerAuth:
      type: http
      scheme: bearer
      bearerFormat: API key
  schemas:
    CreateUserRequest:
      type: object
      required: [external_id]
      properties:
        external_id:
          type: string
          maxLength: 255
          example: user_abc123
        name:
          type: string
          nullable: true
          maxLength: 1024
        email:
          type: string
          nullable: true
          format: email
        metadata:
          type: object
          nullable: true
    UserResponse:
      type: object
      properties:
        id:
          type: string
          format: uuid
        external_id:
          type: string
        name:
          type: string
          nullable: true
        stats:
          $ref: '#/components/schemas/UserStats'
security:
  - BearerAuth: []
```

---

## 12. Testing the OpenAPI Spec

```python
@pytest.mark.asyncio
async def test_openapi_spec_generation(async_client: AsyncClient) -> None:
    """Verify OpenAPI spec is valid and contains expected endpoints."""
    response = await async_client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()

    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == "MemGraph API"
    assert spec["info"]["version"] == "1.0.0"

    # Verify all required paths exist
    paths = spec["paths"]
    assert "/v1/users" in paths
    assert "/v1/users/{user_id}" in paths
    assert "/v1/users/{user_id}/sessions" in paths
    assert "/v1/users/{user_id}/sessions/{session_id}" in paths
    assert "/v1/users/{user_id}/sessions/{session_id}/messages" in paths
    assert "/v1/users/{user_id}/memory" in paths
    assert "/v1/users/{user_id}/context" in paths
    assert "/v1/users/{user_id}/facts" in paths
    assert "/v1/users/{user_id}/graph/nodes" in paths
    assert "/v1/users/{user_id}/graph/edges" in paths
    assert "/v1/users/{user_id}/search" in paths
    assert "/health" in paths
    assert "/ready" in paths
    assert "/v1/admin/organizations" in paths


@pytest.mark.asyncio
async def test_openapi_security_scheme(async_client: AsyncClient) -> None:
    """Verify security scheme is properly configured."""
    response = await async_client.get("/openapi.json")
    spec = response.json()

    assert "securitySchemes" in spec["components"]
    assert "BearerAuth" in spec["components"]["securitySchemes"]
    auth = spec["components"]["securitySchemes"]["BearerAuth"]
    assert auth["type"] == "http"
    assert auth["scheme"] == "bearer"


@pytest.mark.asyncio
async def test_openapi_swagger_ui_accessible(async_client: AsyncClient) -> None:
    """Verify Swagger UI loads."""
    response = await async_client.get("/docs")
    assert response.status_code == 200
    assert "swagger" in response.text.lower()


@pytest.mark.asyncio
async def test_openapi_redoc_accessible(async_client: AsyncClient) -> None:
    """Verify ReDoc loads."""
    response = await async_client.get("/redoc")
    assert response.status_code == 200
    assert "redoc" in response.text.lower()


@pytest.mark.asyncio
async def test_openapi_spec_is_valid_openapi_3_1(async_client: AsyncClient) -> None:
    """Validate the OpenAPI spec against the standard."""
    import json
    from openapi_spec_validator import validate_spec

    response = await async_client.get("/openapi.json")
    spec = response.json()

    # Validate structure
    validate_spec(spec)

    # Verify no PII in schemas
    spec_str = json.dumps(spec).lower()
    assert "password" not in spec_str
    assert "secret" not in spec_str
```

---

## 13. CI Configuration Summary

```yaml
# .gitlab-ci.yml — OpenAPI-related jobs

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
  rules:
    - if: $CI_MERGE_REQUEST_ID

openapi-publish:
  stage: publish
  image: python:3.12-slim
  before_script:
    - pip install pyyaml
  script:
    - python scripts/generate_openapi.py --output docs/openapi.yaml
    - git config user.email "ci@memgraph.dev"
    - git config user.name "MemGraph CI"
    - git add docs/openapi.yaml
    - git commit -m "docs(openapi): update OpenAPI spec [skip ci]" || true
    - git push
  rules:
    - if: $CI_COMMIT_TAG =~ /^v\d+\.\d+\.\d+$/
```

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
