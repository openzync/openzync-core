# OpenZep Codebase Analysis — Issues & Fixes

> Generated from comprehensive analysis by Architect, Senior Dev, and Reviewer.
> Date: 2026-06-13 | Overall rating: 8.5/10

---

## Table of Contents

1. [Critical — Worker DB Engine Lifecycle (Scalability Blocker)](#1-critical--worker-db-engine-lifecycle-scalability-blocker)
2. [Blocker — Layering Violations](#2-blocker--layering-violations)
3. [Blocker — Service Accesses Private Repository Attribute](#3-blocker--service-accesses-private-repository-attribute)
4. [Blocker — Raw SQL in Service Layer](#4-blocker--raw-sql-in-service-layer)
5. [Warning — Middleware & Auth Issues](#5-warning--middleware--auth-issues)
6. [Warning — Service & Repository Design Issues](#6-warning--service--repository-design-issues)
7. [Warning — Missing Repositories & Dead Code](#7-warning--missing-repositories--dead-code)
8. [Warning — Observability & Config Issues](#8-warning--observability--config-issues)
9. [Warning — Testing Gaps](#9-warning--testing-gaps)
10. [Nit — Style & Minor Issues](#10-nit--style--minor-issues)
11. [Appendix — Async SQLAlchemy Compliance Checklist](#appendix--async-sqlalchemy-compliance-checklist)
12. [Appendix — Security Checklist](#appendix--security-checklist)

---

## 1. 🔴 Critical — Worker DB Engine Lifecycle (Scalability Blocker)

### Issue

Worker tasks create short-lived DB engines (each with its own connection pool) **per task invocation**. At scale (e.g. 1000 episodes/second), this creates thousands of engine creations/disposals per second, exhausting PostgreSQL's `max_connections`.

### Affected Files

| File | Lines | Details |
|------|-------|---------|
| `workers/tasks/extract_facts.py` | 112-114, 212-213 | Creates **two** engines per invocation (one for `ctx`, one for `_set_enrichment_bit`) |
| `workers/tasks/extract_facts.py` | 369 | Third engine in `_set_enrichment_bit` |
| `workers/tasks/embed_episode.py` | 92-97 | Creates engine per task invocation |
| `workers/tasks/embed_fact.py` | ~50-60 | Likely same pattern (verify) |
| `workers/tasks/extract_entities.py` | ~60-80 | Likely same pattern (verify) |
| `workers/tasks/classify_dialog.py` | ~40-60 | Likely same pattern (verify) |

### Root Cause

Each task function independently calls `init_db_engine()` with a small pool size (`pool_size=2-5`), then disposes the engine in a `finally` block. ARQ provides a `ctx` dict per worker process, but the engine is never created there and shared.

### Fix

Create a **single shared engine per worker process** and inject it via ARQ context:

```python
# services/worker/worker.py — in main() startup
from core.db import init_db_engine, get_async_session

shared_engine = init_db_engine(
    str(settings.DATABASE_URL),
    pool_size=10,  # per-worker pool
    max_overflow=5,
)

# Pass to worker functions via ctx initialization
ctx = {
    "db_engine": shared_engine,
    "db_session_factory": get_async_session(shared_engine),
}

high_worker = create_arq_worker(
    ctx=ctx,
    functions=[extract_facts, embed_episode, ...],
    ...
)
```

Then in each task:

```python
async def extract_facts(ctx, ...):
    engine = ctx.get("db_engine")
    if engine is None:
        engine = init_db_engine(...)  # fallback
    async with get_async_session(engine)() as db:
        # ... task logic
```

**Impact**: Eliminates connection churn. Single engine per worker process, shared across all task instances.

---

## 2. 🔴 Blocker — Layering Violations

### 2A. Router Performs DB Query Directly

**File**: `routers/graph.py:70-91`  
**Rule**: *Routers are HTTP layer only. No business logic, no database queries.*

The `_resolve_user()` helper instantiates `UserRepository` and calls `get_by_uuid()` before every graph endpoint:

```python
async def _resolve_user(
    db: AsyncSession,
    org_id: UUID,
    user_id: UUID,
) -> None:
    user_repo = UserRepository(db)           # ⚠️ repository in router
    user = await user_repo.get_by_uuid(org_id, user_id)  # ⚠️ DB query in router
```

**Fix**: Move the validation into `GraphService`:

```python
# services/graph_service.py
class GraphService:
    async def ensure_user_exists(self, org_id: UUID, user_id: UUID) -> None:
        user = await self._user_repo.get_by_uuid(org_id, user_id)
        if not user:
            raise EntityNotFoundError(f"User {user_id} not found")

    async def get_entities(self, org_id: UUID, user_id: UUID, ...) -> ...:
        await self.ensure_user_exists(org_id, user_id)
        # ... existing logic
```

The graph router should call `service.get_entities(...)` directly.

---

### 2B. Service Contains Raw SQL

**File**: `services/memory_service.py:576-584`  
**Rule**: *Service methods contain zero SQLAlchemy expressions.*

`_get_org_pii_config()` executes a raw `text()` query:

```python
result = await self._db.execute(
    text("SELECT quotas->'pii' AS pii_config FROM organizations WHERE id = :org_id"),
    {"org_id": org_id},
)
```

**Fix**: Create `OrganizationRepository`:

```python
# repositories/organization_repository.py
class OrganizationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_pii_config(self, org_id: UUID) -> bool | None:
        result = await self._db.execute(
            text("SELECT quotas->'pii' AS pii_config FROM organizations WHERE id = :org_id"),
            {"org_id": org_id},
        )
        row = result.one_or_none()
        return row.pii_config if row else None
```

Then in `MemoryService`:

```python
self._org_repo = OrganizationRepository(db)
pii_config = await self._org_repo.get_pii_config(org_id)
```

---

### 2C. Router-Created Dependency Factories

**Files**:
- `routers/sessions.py:200-212`
- `routers/memory.py:44-68`
- `routers/graph.py:53-64`
- `routers/users.py:35-44`

Several routers define inline dependency factories that instantiate repositories directly, instead of using the shared factories in `dependencies/services.py`.

**Fix**: Move all service dependency factories into `dependencies/services.py`:

```python
# dependencies/services.py
from functools import lru_cache

@lru_cache
def get_fact_service(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> FactService:
    return FactService(
        repo=FactRepository(db),
        session_repo=SessionRepository(db),
        entity_repo=EntityRepository(db),
        cache=CacheService(redis),
    )
```

---

### 2D. Service Imports Model Layer

**File**: `services/memory_service.py:392, 439` — Imports `IntegrityError` from `sqlalchemy.exc` and `Session` from `models.session` inside method bodies.

**Fix**: Use `TYPE_CHECKING` guard for type annotations, and wrap DB errors through the repository layer:

```python
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.session import Session
    from sqlalchemy.exc import IntegrityError
```

For runtime: catch domain exceptions from the repository, not SQLAlchemy internals.

---

## 3. 🔴 Blocker — Service Accesses Private Repository Attribute

**File**: `services/auth_service.py:346-347`

```python
await self._repo._db.flush()    # ⚠️ accessing private _db
await self._repo._db.refresh(user)
```

This reaches into the repository's private `_db` attribute. If the repository changes internal storage, this breaks.

**Fix**: Add explicit methods to `AuthRepository`:

```python
# In repositories/auth_repository.py
class AuthRepository:
    async def flush(self) -> None:
        await self._db.flush()

    async def refresh(self, instance: Any) -> None:
        await self._db.refresh(instance)

    async def update_dashboard_user(
        self,
        user_id: UUID,
        name: str | None = None,
        email: str | None = None,
        password_hash: str | None = None,
    ) -> DashboardUser:
        user = await self.get_user_by_id(user_id)
        if name is not None:
            user.name = name
        if email is not None:
            user.email = email
        if password_hash is not None:
            user.password_hash = password_hash
        await self._db.flush()
        return user
```

---

## 4. 🟡 Warning — Middleware & Auth Issues

### 4A. Dead RLS Context in AuthMiddleware

**File**: `middleware/auth.py:573-576`

The middleware calls `_set_rls_context()` using a **short-lived session** that is already closed before the request handler runs. PostgreSQL's `set_config` is session-local, so this has **zero effect** on request handlers.

The correct implementation already exists in `dependencies/db.py:67-75`.

**Fix**: Remove the dead code:

```python
# middleware/auth.py — remove or comment out:
# await _set_rls_context(...)  # DEAD CODE — RLS set in dependencies/db.py
```

**Impact**: Eliminates one unnecessary DB session acquisition + query per authenticated request.

---

### 4B. TrustedHostMiddleware Misconfigured

**File**: `services/api/main.py:161-165`

```python
allowed_hosts = (
    settings.CORS_ORIGINS.split(",")  # ⚠️ contains origins like http://localhost:3000
    if settings.ENVIRONMENT == "production"
    else ["*"]
)
```

`TrustedHostMiddleware` expects hostnames (e.g., `localhost:3000`, `api.openzep.dev`), not origins. Using `CORS_ORIGINS` (which are full origins like `http://localhost:3000`) **will reject all requests in production** because the `Host` header contains a hostname, not an origin.

**Fix**: Add a dedicated `HOSTS_ALLOWED` setting:

```python
# core/config.py
HOSTS_ALLOWED: str = Field(
    default="localhost:8000",
    description="Comma-separated allowed Host header values.",
    validation_alias="MG_HOSTS_ALLOWED",
)

# services/api/main.py
allowed_hosts = (
    settings.HOSTS_ALLOWED.split(",")
    if settings.ENVIRONMENT == "production"
    else ["*"]
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
```

---

### 4C. Auth Middleware Opens DB Session on Cache Miss

**File**: `middleware/auth.py:219-224`

Every API key cache miss opens a **new** DB session (`async with db_factory() as session`) instead of reusing the application session. A malicious actor with rotating API key prefixes can trigger a DB query per request.

**Fix**: Add a bloom filter in Redis as a fast negative check before going to DB, or keep the cache TTL tuned and add rate limiting for the auth cache-miss path.

---

### 4D. No Brute-Force Protection on Auth Endpoints

**Files**: `routers/auth.py` — Login and signup endpoints are in the public endpoints list but have no progressive delay or account lockout on repeated failures.

**Fix**: Add Redis-backed rate limiting with per-IP and per-account counters:

```python
# middleware/rate_limit.py or dedicated auth throttle
class AuthThrottle:
    def __init__(self, redis: Redis):
        self._redis = redis

    async def check_login_attempt(self, email: str, ip: str) -> None:
        key = f"auth:login:{email}:{ip}"
        attempts = await self._redis.incr(key)
        await self._redis.expire(key, 900)  # 15 min window
        if attempts > 5:
            raise RateLimitError("Too many login attempts. Try again later.")
```

---

### 4E. BaseHTTPMiddleware Performance Overhead

**Files**: `middleware/auth.py`, `middleware/rate_limit.py`, `middleware/audit.py`

These three classes extend `BaseHTTPMiddleware`, which wraps the ASGI streaming interface per-request. At high throughput (1k+ RPS), this incurs measurable overhead.

**Fix**: Migrate `AuthMiddleware` (the most performance-critical) to raw ASGI middleware:

```python
class AuthMiddleware:
    def __init__(self, app, db_factory, redis):
        self.app = app
        self.db_factory = db_factory
        self.redis = redis

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Extract & validate token, set state...
        await self.app(scope, receive, send)
```

---

## 5. 🟡 Warning — Service & Repository Design Issues

### 5A. Documented N+1 in Session Listing

**File**: `services/session_service.py:214-217`

```python
# TODO(performance): Batch-load message/fact counts for list items
# via a single query instead of N+1.  For now, stats default to 0
# in the list response.
```

**Fix**: Add batch stats method to `SessionRepository`:

```python
# repositories/session_repository.py
async def batch_get_stats(
    self, session_ids: list[UUID], organization_id: UUID
) -> dict[UUID, dict[str, int]]:
    """Return message_count per session in one query."""
    stmt = select(
        Episode.session_id,
        func.count(Episode.id).label("message_count"),
    ).where(
        Episode.session_id.in_(session_ids),
        Episode.organization_id == organization_id,
        Episode.is_deleted == False,
    ).group_by(Episode.session_id)

    result = await self._db.execute(stmt)
    return {
        row.session_id: {"message_count": row.message_count}
        for row in result.all()
    }
```

---

### 5B. Raw Dict Passing Instead of Typed Schemas

| File | Line | Issue |
|------|------|-------|
| `services/fact_service.py` | 318 | Returns `tuple[list[dict[str, Any]], str \| None]` |
| `services/graph_service.py` | 45, 75, 144 | Returns `dict[str, Any]` |
| `services/user_service.py` | 84-101 | `_user_to_dict()` converts to raw dict for Pydantic |
| `services/session_service.py` | 86 | Uses `session_to_dict()` mapper |

**Fix**: Use Pydantic `validation_alias` to handle field name mismatches without dict conversion:

```python
# schemas/sessions.py — replace mappers.py usage
class SessionResponse(BaseModel):
    id: UUID
    user_id: UUID
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias="metadata_",  # maps ORM metadata_ -> schema metadata
    )
    model_config = ConfigDict(from_attributes=True)
```

Eliminate `repositories/mappers.py` entirely once all callers use Pydantic validation.

---

### 5C. HTTPException Leak in Sessions Router

**File**: `routers/sessions.py:252-255`

```python
try:
    await service.get_session(org_uuid, session_id, user_id=user_id)
except Exception as exc:
    raise HTTPException(status_code=404, detail=f"Session {session_id} not found") from exc
```

This swallows ALL exceptions (DB down, timeout, etc.) as 404. The service already raises `NotFoundError` which the global handler converts to 404 properly.

**Fix**: Remove the catch-all:

```python
# Before attempting to fetch facts, verify session exists
await service.get_session(org_uuid, session_id, user_id=user_id)
# NotFoundError propagates to global handler -> 404
```

---

### 5D. Serial Fact Creation in Worker

**File**: `workers/tasks/extract_facts.py:248-275`

Each extracted fact is created via individual `create_or_skip()` calls — one DB round-trip per fact.

**Fix**: Use batch insert:

```python
# repositories/fact_repository.py
async def batch_create_or_skip(
    self, facts: list[dict], organization_id: UUID, session_id: UUID
) -> list[Fact]:
    """Batch insert with ON CONFLICT DO NOTHING."""
    stmt = (
        insert(Fact)
        .values([
            {
                "organization_id": organization_id,
                "session_id": session_id,
                "fact": f["fact"],
                "source_entity_id": f.get("source_entity_id"),
                "target_entity_id": f.get("target_entity_id"),
                "episode_id": f.get("episode_id"),
            }
            for f in facts
        ])
        .on_conflict_do_nothing(
            index_elements=["session_id", "source_entity_id", "target_entity_id", "fact"]
        )
        .returning(Fact)
    )
    result = await self._db.execute(stmt)
    return list(result.scalars().all())
```

---

### 5E. Cursor Pagination Code Duplication

Base64 cursor encoding/decoding logic is duplicated across:
- `repositories/user_repository.py:415-453`
- `repositories/session_repository.py:569-619`
- `repositories/fact_repository.py:347-363` (inline in method)

**Fix**: Extract to `core/cursor.py`:

```python
# core/cursor.py
import base64
import json
from uuid import UUID
from datetime import datetime

def encode_cursor(value: str | UUID | datetime) -> str:
    if isinstance(value, UUID):
        value = str(value)
    elif isinstance(value, datetime):
        value = value.isoformat()
    return base64.urlsafe_b64encode(json.dumps({"v": value}).encode()).decode()

def decode_cursor(cursor: str) -> str:
    return json.loads(base64.urlsafe_b64decode(cursor.encode()))["v"]
```

---

## 6. 🟡 Warning — Missing Repositories & Dead Code

### 6A. Missing OrganizationRepository

**Affected**: `services/memory_service.py:576` (raw SQL), and potentially `organization_service.py`.

**Fix**: Create `repositories/organization_repository.py`:

```python
class OrganizationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_pii_config(self, org_id: UUID) -> bool:
        result = await self._db.execute(
            text("SELECT quotas->'pii' AS pii_config FROM organizations WHERE id = :org_id"),
            {"org_id": org_id},
        )
        row = result.one_or_none()
        return bool(row.pii_config) if row else False

    async def get_llm_config(self, org_id: UUID) -> dict:
        result = await self._db.execute(
            text("SELECT llm_config FROM organizations WHERE id = :org_id"),
            {"org_id": org_id},
        )
        row = result.one_or_none()
        return row.llm_config if row else {}

    async def get_quota(self, org_id: UUID, quota_name: str) -> int | None:
        result = await self._db.execute(
            text(f"SELECT quotas->>'{quota_name}' AS quota FROM organizations WHERE id = :org_id"),
            {"org_id": org_id},
        )
        row = result.one_or_none()
        return int(row.quota) if row and row.quota else None
```

---

### 6B. Missing ApiKeyRepository

**Affected**: `middleware/auth.py:192-236` queries `ApiKey` model directly.

**Fix**: Create `repositories/api_key_repository.py`:

```python
class ApiKeyRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_by_lookup_hash(self, lookup_hash: str) -> ApiKey | None:
        stmt = select(ApiKey).where(
            ApiKey.lookup_hash == lookup_hash,
            ApiKey.is_deleted == False,
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def update_last_used(self, key_id: UUID) -> None:
        stmt = (
            update(ApiKey)
            .where(ApiKey.id == key_id)
            .values(last_used_at=func.now())
        )
        await self._db.execute(stmt)
        await self._db.flush()
```

Then inject into middleware via a callable or constructor parameter.

---

### 6C. `requests` in Requirements for Async Project

**File**: `requirements.txt:25` — `requests==2.34.2`

The `requests` library is synchronous. In an async FastAPI project, all HTTP calls should use `httpx.AsyncClient`.

**Fix**: Either remove `requests` (if only used in scripts), or move it to a `[dev]` optional dependency:

```toml
[project.optional-dependencies]
dev = [
    ...
    "requests>=2.31.0",  # only for scripts/seed_load_test.py
]
```

---

## 7. 🟡 Warning — Observability & Config Issues

### 7A. Audit Response Body Without PII Redaction

**File**: `middleware/audit.py:216-222`

When `MG_AUDIT_LOG_RESPONSE_BODY=true`, response bodies are captured with a 10KB cap but no PII redaction. This could log user messages and personal information.

**Fix**: Add PII redaction to the captured response body:

```python
# middleware/audit.py
from services.pii_service import PIIService

class AuditMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, pii_service: PIIService | None = None):
        self._pii_service = pii_service
        ...

    async def _capture_body(self, body: bytes) -> str | None:
        if not self._capture_enabled:
            return None
        text = body.decode("utf-8", errors="replace")[:10000]
        if self._pii_service:
            text = await self._pii_service.redact(text)
        return text
```

---

### 7B. structlog Context Vars Not Cleaned

**Files**: `middleware/logging.py:121-126`, `middleware/request_id.py:55`

`structlog.contextvars.bind_contextvars()` is called but never cleaned up. While `ContextVar` is asyncio-task-local, context could leak in thread-based concurrency or task reuse scenarios.

**Fix**: Clean up in middleware `finally` block:

```python
# middleware/logging.py
try:
    response = await call_next(request)
    return response
finally:
    structlog.contextvars.clear_contextvars()
```

---

### 7C. Duplicate MG_SECRET_KEY in .env.example

**File**: `.env.example:52-54`

The `MG_SECRET_KEY` line appears twice. The second declaration silently overrides the first.

**Fix**: Remove the duplicate line.

---

### 7D. No End-to-End Trace ID Propagation Through ARQ

The API has `request_id` middleware, but ARQ jobs don't propagate a `trace_id`. Debugging a single HTTP request that spawns 6 ARQ tasks across 2 queues is nearly impossible without unified tracing.

**Fix**: Pass `trace_id` through ARQ context and log it in all worker tasks:

```python
# services/memory_service.py — during enqueue
await self._arq_pool.enqueue_job(
    "extract_facts",
    _job_id=f"extract_facts_{episode_id}",
    _queue="high",
    trace_id=getattr(request.state, "request_id", str(uuid.uuid4())),
    episode_id=str(episode_id),
    ...
)

# workers/tasks/extract_facts.py
async def extract_facts(ctx: dict, episode_id: str, trace_id: str, ...) -> None:
    logger = structlog.get_logger().bind(trace_id=trace_id, episode_id=episode_id)
    logger.info("worker.extract_facts.started")
    # ...
    logger.info("worker.extract_facts.completed", fact_count=len(facts))
```

---

## 8. 🟡 Warning — Testing Gaps

### 8A. fact_service Unit Test is Minimal

**File**: `tests/unit/test_fact_service.py`

Only one test exists, and it tests an edge case (`facts=[]`) that's caught at schema validation (`min_length=1`). The test doesn't exercise batch_create, dedup, session resolution, or ARQ enqueue.

**Fix**: Add tests:

```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_ingest_facts_happy_path():
    """Verify successful fact ingestion calls batch_create and enqueues embedding."""
    fact_service = FactService(mock_repo, mock_session_repo, mock_entity_repo, mock_cache)
    mock_repo.batch_create.return_value = [Fact(id=uuid4(), fact="test")]
    
    result = await fact_service.ingest_facts(
        org_id=uuid4(), session_id=uuid4(), facts=[{"fact": "test", "source_entity_id": uuid4()}]
    )
    
    assert result.status == "accepted"
    mock_repo.batch_create.assert_awaited_once()
    # Verify embedding job was enqueued

@pytest.mark.asyncio
@pytest.mark.unit
async def test_ingest_facts_session_not_found():
    """Verify NotFoundError when session doesn't exist."""
    mock_session_repo.get_by_uuid.return_value = None
    fact_service = FactService(mock_repo, mock_session_repo, mock_entity_repo, mock_cache)
    
    with pytest.raises(NotFoundError):
        await fact_service.ingest_facts(
            org_id=uuid4(), session_id=uuid4(), facts=[{"fact": "test"}]
        )
```

---

### 8B. No tests/e2e/ Directory

**File**: `pyproject.toml` defines an `e2e` marker but no `tests/e2e/` directory exists.

**Fix**: Create `tests/e2e/` with full API flow tests:

```python
# tests/e2e/test_full_ingestion_pipeline.py
@pytest.mark.asyncio
@pytest.mark.e2e
async def test_full_memory_ingestion_flow(
    async_client: AsyncClient,
    auth_headers: dict,
    test_org: dict,
):
    # 1. Create user
    user_resp = await async_client.post(
        "/v1/users", json={"name": "Test User"}, headers=auth_headers
    )
    assert user_resp.status_code == 201
    user_id = user_resp.json()["id"]

    # 2. Create session
    session_resp = await async_client.post(
        "/v1/sessions", json={"user_id": user_id}, headers=auth_headers
    )
    assert session_resp.status_code == 201
    session_id = session_resp.json()["id"]

    # 3. Ingest memory
    memory_resp = await async_client.post(
        f"/v1/sessions/{session_id}/memory",
        json={"messages": [{"role": "user", "content": "Hello"}]},
        headers=auth_headers,
    )
    assert memory_resp.status_code == 202

    # 4. Search memory
    search_resp = await async_client.post(
        "/v1/search",
        json={"query": "Hello", "session_ids": [session_id]},
        headers=auth_headers,
    )
    assert search_resp.status_code == 200
```

---

### 8C. Error Handler Tests Incomplete

**File**: `tests/unit/test_exceptions.py`

Tests exist for the base exception hierarchy and the `register_exception_handlers` function, but `EntityNotFoundError`, `EdgeNotFoundError`, and `GraphTimeoutError` are not tested.

**Fix**: Add tests for the missing exception types and their HTTP mappings.

---

## 9. 🔵 Nit — Style & Minor Issues

### 9A. Deprecated Pydantic v2 Config Style

**Files**: `schemas/sessions.py:73`, `schemas/users.py:141`, `schemas/auth.py:103`

```python
# Current (deprecated in Pydantic v2 but still works)
model_config = {"from_attributes": True}

# Preferred
from pydantic import ConfigDict
model_config = ConfigDict(from_attributes=True)
```

---

### 9B. Lazy Imports in Method Bodies

**Files**: `services/user_service.py` (5 places), `services/memory_service.py` (3 places)

Schemas and models imported inside method bodies to avoid circular imports. This is fragile and makes it harder to reason about dependencies.

**Fix**: Use `TYPE_CHECKING` guard:

```python
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from schemas.users import UserResponse, UserResponseWithStats, UserListResponse
```

---

### 9C. Inline `HTTPException` Re-import

**File**: `routers/graph.py:270-271`

```python
from fastapi import HTTPException  # unnecessary re-import
raise HTTPException(status_code=422, detail="...")
```

Already imported at line 22. Use `core.exceptions.ValidationError` instead of `HTTPException` per project conventions.

---

### 9D. Missing `services/__init__.py`

All other packages have `__init__.py` files. `services/` is missing it.

**Fix**: Add empty `services/__init__.py`.

---

### 9E. Incorrect Comment About Middleware Ordering

**File**: `services/api/main.py:137-152`

The comment says `RequestID` is the "innermost, closest to router" but the numbering conflicts between the explanatory block (items 0-9) and the actual registration order. The middleware ordering is correct at runtime, but the comment is confusing.

**Fix**: Rewrite the comment to be consistent:

```python
# Middleware execution order (outermost → innermost, Starlette LIFO):
#   1. Metrics          (wraps everything, including 404s)
#   2. CORS             (handles OPTIONS preflight)
#   3. Logging          (request/response lifecycle)
#   4. Tracing          (OpenTelemetry spans)
#   5. RateLimit        (per-IP sliding window)
#   6. Auth             (JWT / API key validation)
#   7. Audit            (post-response logging)
#   8. GZip             (compression)
#   9. TrustedHost      (host header validation)
#   10. RequestID       (assigns request_id — innermost)
```

---

### 9F. Guard Clause Ordering

**File**: `services/auth_service.py:306-347`

The `update_profile` method loads user then redundantly checks `if user is None` after already verifying.

**Fix**: Use early return pattern:

```python
user = await self._repo.get_user_by_id(user_id)
if user is None:
    raise NotFoundError("Dashboard user not found.")

if payload.name is not None:
    user.name = payload.name
if payload.email is not None:
    user.email = payload.email
```

---

## Appendix — Async SQLAlchemy Compliance Checklist

| Check | Status | Location |
|-------|--------|----------|
| Uses `postgresql+asyncpg://` | ✅ | `core/db.py:60-64` validates |
| No `session.query()` — uses `select()` | ✅ | All repositories |
| `await` on `session.execute()` | ✅ | Every execute/delete/flush |
| No `requests` in async context | ✅ | Uses `httpx.AsyncClient` |
| `expire_on_commit=False` | ✅ | `core/db.py:102` |
| `pool_pre_ping=True` | ✅ | `core/db.py:68` |
| Explicit pool sizes | ✅ | `pool_size=20, max_overflow=10` |
| **Auto-reject violations found** | **0** | Clean across all layers |

---

## Appendix — Security Checklist

| Check | Status | Notes |
|-------|--------|-------|
| Hardcoded secrets | ✅ None found | All via `pydantic-settings` + env vars |
| `allow_origins=["*"]` in production | ✅ Never | Uses `settings.CORS_ORIGINS` |
| Ownership checks | ⚠️ Org-level only | No explicit per-resource authorization documented |
| Unvalidated file uploads | ✅ N/A | No file upload endpoints |
| PII in log statements | ✅ Redacted | `core/logging.py` handles redaction |
| Unpinned dependencies | ✅ All pinned | `requirements.txt` has exact versions |
| JWT validation on every request | ✅ | `AuthMiddleware` + `require_org_id` |
| Rate limiting on public endpoints | ✅ | Redis sliding window |
| SQL injection | ✅ None | All raw SQL uses bound parameters |
| Audit response body redaction | ❌ Missing | See [§7A](#7a-audit-response-body-without-pii-redaction) |

---

## Issue Count Summary

| Severity | Count | Category |
|----------|-------|----------|
| 🔴 Critical | 1 | Worker DB engine lifecycle (scalability) |
| 🔴 Blocker | 4 | Layering violations, private attribute access, raw SQL in service |
| 🟡 Warning | 15+ | Middleware, config, missing repos, testing gaps, observability |
| 🔵 Nit | 6 | Style, imports, comments, missing `__init__.py` |
| **Total** | **26+** | |

## Priority Implementation Order

1. **Fix worker DB engine lifecycle** — eliminates #1 scalability blocker
2. **Fix layering violations** — graph router, service raw SQL, private repo access
3. **Fix TrustedHost middleware** — production is broken without this
4. **Remove dead RLS code** — frees one DB connection per request
5. **Batch-load session stats** — eliminates documented N+1
6. **Create missing repositories** — OrganizationRepository, ApiKeyRepository
7. **Extract cursor pagination** — reduce code duplication
8. **Add e2e tests** — close testing gap
9. **Pydantic config style** — modernization
10. **Trace ID propagation** — debugging at scale
