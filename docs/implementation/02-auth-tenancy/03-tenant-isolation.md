# Multi-Tenant Isolation

> **Phase:** 0 (Foundation)
> **SRS Requirements:** MT-01, MT-02, MT-03, MT-04, MT-05, SEC-03
> **Dependencies:** [01-api-key-auth.md](01-api-key-auth.md), [02-jwt-auth.md](02-jwt-auth.md), [01-postgresql-schema.md](../01-data-models/01-postgresql-schema.md)

---

## 1. Overview

OpenZep is a **multi-tenant platform** — a single deployment serves multiple organizations, strictly isolating their data. This document defines the **three-layer isolation strategy** that makes cross-tenant access impossible at every level of the stack:

| Layer | Mechanism | Guarantee |
|-------|-----------|-----------|
| **Primary** | PostgreSQL Row-Level Security (RLS) | DB-enforced: policy prevents any query from returning rows outside the current org |
| **Secondary** | SQLAlchemy `TenantAwareRepository` | Code-enforced: every repository query auto-filters by `organization_id` |
| **Tertiary** | FastAPI dependency injection | API-enforced: every endpoint has `org_id` from the authenticated identity |

**Principle of defence in depth:** Any single layer failing (e.g., RLS policy misconfigured, a new repository missing the filter) is caught by the other two layers. Cross-tenant data access requires all three layers to fail simultaneously.

---

## 2. Layer 1 — PostgreSQL Row-Level Security (Primary)

### 2.1 Enabling RLS

RLS is the **strongest guarantee** — it operates at the database engine level. Even if an attacker bypasses the application layer and connects directly to the database, RLS prevents them from reading data outside their organization.

```sql
-- Migration: V004_enable_rls.sql
-- Run this migration AFTER creating all tenant-scoped tables.

-- Every table that references organization_id gets RLS enabled.

-- ============================================================
-- organizations: The root tenant table.
-- RLS policy: admins see their own org; super_admins see all.
-- ============================================================
ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;

CREATE POLICY org_isolation_organizations ON organizations
    FOR ALL
    USING (
        -- Super admin: bypass (checked via application role)
        current_setting('app.bypass_rls', true) = 'true'
        OR
        -- Regular admin: only see their own org
        id = current_setting('app.org_id')::UUID
    );

-- ============================================================
-- api_keys: Scoped to organization.
-- ============================================================
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;

CREATE POLICY org_isolation_api_keys ON api_keys
    FOR ALL
    USING (
        current_setting('app.bypass_rls', true) = 'true'
        OR
        organization_id = current_setting('app.org_id')::UUID
    );

-- ============================================================
-- users: Scoped to organization via organization_id.
-- ============================================================
ALTER TABLE users ENABLE ROW LEVEL SECURITY;

CREATE POLICY org_isolation_users ON users
    FOR ALL
    USING (
        current_setting('app.bypass_rls', true) = 'true'
        OR
        organization_id = current_setting('app.org_id')::UUID
    );

-- ============================================================
-- sessions: Scoped via users → organization.
-- Uses a subquery to resolve indirectly — sessions don't have
-- organization_id directly, but their parent user does.
-- ============================================================
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY org_isolation_sessions ON sessions
    FOR ALL
    USING (
        current_setting('app.bypass_rls', true) = 'true'
        OR
        EXISTS (
            SELECT 1 FROM users
            WHERE users.id = sessions.user_id
            AND users.organization_id = current_setting('app.org_id')::UUID
        )
    );

-- ============================================================
-- episodes: Scoped via sessions → users → organization.
-- ============================================================
ALTER TABLE episodes ENABLE ROW LEVEL SECURITY;

CREATE POLICY org_isolation_episodes ON episodes
    FOR ALL
    USING (
        current_setting('app.bypass_rls', true) = 'true'
        OR
        EXISTS (
            SELECT 1 FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.id = episodes.session_id
            AND users.organization_id = current_setting('app.org_id')::UUID
        )
    );

-- ============================================================
-- facts: Scoped via users → organization.
-- ============================================================
ALTER TABLE facts ENABLE ROW LEVEL SECURITY;

CREATE POLICY org_isolation_facts ON facts
    FOR ALL
    USING (
        current_setting('app.bypass_rls', true) = 'true'
        OR
        EXISTS (
            SELECT 1 FROM users
            WHERE users.id = facts.user_id
            AND users.organization_id = current_setting('app.org_id')::UUID
        )
    );

-- ============================================================
-- structured_extractions: Scoped via sessions → users → org.
-- ============================================================
ALTER TABLE structured_extractions ENABLE ROW LEVEL SECURITY;

CREATE POLICY org_isolation_structured_extractions ON structured_extractions
    FOR ALL
    USING (
        current_setting('app.bypass_rls', true) = 'true'
        OR
        EXISTS (
            SELECT 1 FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.id = structured_extractions.session_id
            AND users.organization_id = current_setting('app.org_id')::UUID
        )
    );

-- ============================================================
-- dialog_classifications: Scoped via episodes → sessions → users → org.
-- ============================================================
ALTER TABLE dialog_classifications ENABLE ROW LEVEL SECURITY;

CREATE POLICY org_isolation_dialog_classifications ON dialog_classifications
    FOR ALL
    USING (
        current_setting('app.bypass_rls', true) = 'true'
        OR
        EXISTS (
            SELECT 1 FROM episodes
            JOIN sessions ON sessions.id = episodes.session_id
            JOIN users ON users.id = sessions.user_id
            WHERE episodes.id = dialog_classifications.episode_id
            AND users.organization_id = current_setting('app.org_id')::UUID
        )
    );

-- ============================================================
-- admin_users: Scoped to organization.
-- ============================================================
ALTER TABLE admin_users ENABLE ROW LEVEL SECURITY;

CREATE POLICY org_isolation_admin_users ON admin_users
    FOR ALL
    USING (
        current_setting('app.bypass_rls', true) = 'true'
        OR
        organization_id = current_setting('app.org_id')::UUID
    );

-- ============================================================
-- refresh_tokens: Scoped via admin_users → organization.
-- ============================================================
ALTER TABLE refresh_tokens ENABLE ROW LEVEL SECURITY;

CREATE POLICY org_isolation_refresh_tokens ON refresh_tokens
    FOR ALL
    USING (
        current_setting('app.bypass_rls', true) = 'true'
        OR
        EXISTS (
            SELECT 1 FROM admin_users
            WHERE admin_users.id = refresh_tokens.user_id
            AND admin_users.organization_id = current_setting('app.org_id')::UUID
        )
    );
```

### 2.2 Setting `app.org_id` at Session Level

After API key or JWT validation succeeds, the auth middleware sets the PostgreSQL session parameters before any query executes:

```python
# middleware/tenant.py
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import text


class TenantSessionMiddleware(BaseHTTPMiddleware):
    """Set PostgreSQL session-level variables for RLS.

    This middleware runs AFTER auth middleware. It sets:
    - app.org_id: The authenticated organization UUID
    - app.bypass_rls: 'true' for super_admin, 'false' for regular users

    These are read by the RLS policies defined in V004_enable_rls.sql.
    """
    async def dispatch(self, request: Request, call_next):
        # Extract org_id from authenticated state (set by auth middleware)
        org_id = getattr(request.state, "org_id", None)
        role = getattr(request.state, "role", None)

        if org_id:
            async with request.app.state.db_session() as session:
                await session.execute(
                    text(f"SELECT set_config('app.org_id', :org_id, true)"),
                    {"org_id": str(org_id)},
                )

                # Super admin bypass
                bypass = "true" if role == "super_admin" else "false"
                await session.execute(
                    text(f"SELECT set_config('app.bypass_rls', :bypass, true)"),
                    {"bypass": bypass},
                )

        response = await call_next(request)
        return response
```

> **Important transaction note:** `set_config(..., true)` uses `transaction_local` scope — the setting is automatically cleared when the database session is returned to the connection pool. This prevents org_id leaking between requests in a pooled connection.

### 2.3 RLS Policy Type — Why `FOR ALL`?

The policies above use `FOR ALL` (equivalent to `FOR SELECT, INSERT, UPDATE, DELETE`). This means:

| Operation | RLS Check |
|-----------|-----------|
| `SELECT` | `USING` clause filters returned rows |
| `INSERT` | `USING` clause checks the row being inserted belongs to the org |
| `UPDATE` | `USING` clause filters rows to update |
| `DELETE` | `USING` clause filters rows to delete |

**Edge case — INSERT:** When inserting a new row, the `USING` clause checks the row's `organization_id` (or resolved org path) against `app.org_id`. This prevents creating data that belongs to another org, even if the application inadvertently passes the wrong ID.

### 2.4 Testing RLS Directly

```sql
-- Verify org isolation directly in PostgreSQL:

-- As superuser
SET app.org_id = 'org_abc';
SET app.bypass_rls = 'false';

-- Should only return users belonging to org_abc
SELECT * FROM users;

-- Attempt to insert a user into another org — should fail
INSERT INTO users (organization_id, external_id)
VALUES ('org_xyz', 'user_999');
-- ERROR: new row violates row-level security policy for "users"
```

---

## 3. Layer 2 — SQLAlchemy `TenantAwareRepository` (Secondary)

### 3.1 Base Repository Class

The application code **never writes raw SQL** that could bypass RLS. Every repository inherits from `TenantAwareRepository`, which automatically appends `organization_id` filters to all queries:

```python
# packages/core/repositories/base.py
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID
from typing import Any


class TenantAwareRepository:
    """Base repository with automatic tenant filtering.

    All domain repositories inherit from this class. It provides:
    - Automatic `organization_id` filter on every query
    - Safe construction of WHERE clauses without SQL injection
    - Consistent method naming across all repositories

    Usage:
        class UserRepository(TenantAwareRepository):
            def __init__(self, db: AsyncSession, org_id: UUID):
                super().__init__(db, org_id)

            async def get_all(self) -> list[User]:
                stmt = select(User).where(User.is_active == True)
                result = await self._db.execute(self._apply_org_filter(stmt, User))
                return result.scalars().all()
    """

    def __init__(self, db: AsyncSession, org_id: UUID):
        self._db = db
        self._org_id = org_id

    def _apply_org_filter(self, stmt: Any, model_class: type) -> Any:
        """Append organization_id filter to a SQLAlchemy select statement.

        Args:
            stmt: A SQLAlchemy select() statement.
            model_class: The ORM model class that has organization_id.

        Returns:
            The statement with an additional WHERE clause.

        Raises:
            ValueError: If the model doesn't have an organization_id column.
        """
        if not hasattr(model_class, "organization_id"):
            raise ValueError(
                f"{model_class.__name__} has no organization_id column. "
                "Cannot apply tenant filter."
            )
        return stmt.where(model_class.organization_id == self._org_id)

    @staticmethod
    def _resolve_org_via_join(stmt: Any, model_class: Any, join_path: list[tuple[Any, Any]]) -> Any:
        """For models without direct organization_id, join through parent tables.

        Example:
            _resolve_org_via_join(
                stmt,
                Episode,
                [(Session, Episode.session_id == Session.id),
                 (User, Session.user_id == User.id)]
            )
        """
        for joined_model, on_clause in join_path:
            stmt = stmt.join(joined_model, on_clause)
        return stmt.where(User.organization_id == self._org_id)
```

### 3.2 Domain Repository Examples

```python
# packages/core/repositories/user_repository.py
class UserRepository(TenantAwareRepository):
    """Repository for the users table — scoped to organization_id."""

    async def get_by_external_id(self, external_id: str) -> User | None:
        stmt = select(User).where(User.external_id == external_id)
        stmt = self._apply_org_filter(stmt, User)
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all(self, limit: int = 50, offset: int = 0) -> list[User]:
        stmt = select(User).offset(offset).limit(limit)
        stmt = self._apply_org_filter(stmt, User)
        result = await self._db.execute(stmt)
        return result.scalars().all()

    async def create(self, external_id: str, name: str | None = None, email: str | None = None) -> User:
        user = User(
            organization_id=self._org_id,
            external_id=external_id,
            name=name,
            email=email,
        )
        self._db.add(user)
        await self._db.flush()
        await self._db.refresh(user)
        return user


# packages/core/repositories/session_repository.py
class SessionRepository(TenantAwareRepository):
    """Repository for sessions — no direct organization_id, resolved via user."""

    async def get_by_external_id(self, user_id: UUID, external_id: str) -> Session | None:
        # Sessions don't have organization_id. We join through users to enforce
        # the tenant boundary.
        stmt = (
            select(Session)
            .join(User, Session.user_id == User.id)
            .where(
                Session.external_id == external_id,
                Session.user_id == user_id,
                User.organization_id == self._org_id,
            )
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()
```

### 3.3 Repository Injection

```python
# dependencies/db.py
from fastapi import Request


def get_user_repository(request: Request) -> UserRepository:
    """Factory dependency that creates a tenant-scoped repository.

    The org_id is extracted from the authenticated request state
    (set by auth middleware in 01-api-key-auth.md).
    """
    db = request.app.state.db_session()
    org_id = request.state.org_id  # Set by auth middleware
    return UserRepository(db=db, org_id=org_id)
```

---

## 4. Layer 3 — API Dependency Injection (Tertiary)

### 4.1 Mandatory `org_id` on Every Endpoint

Every API route handler receives `org_id` as a dependency injected by the auth middleware:

```python
# dependencies/auth.py
from typing import Annotated, Optional
from uuid import UUID


async def get_org_id(request: Request) -> UUID:
    """Extract the authenticated organization ID from request state.

    This dependency is used on every /v1/ endpoint to ensure:
    1. The authenticated org is explicitly available to the handler
    2. Any repository or service instantiated gets the correct org_id
    3. Cross-org access is structurally impossible without changing this ID

    Raises:
        HTTPException(401): If no org_id in request state (not authenticated).
    """
    org_id = getattr(request.state, "org_id", None)
    if org_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "NOT_AUTHENTICATED", "message": "Authentication required."},
        )
    return org_id


# Type alias for convenience
OrgId = Annotated[UUID, Depends(get_org_id)]
```

### 4.2 Usage in Route Handlers

```python
# routers/users.py
from dependencies.auth import OrgId

router = APIRouter(prefix="/v1/users", tags=["users"])


@router.get("")
async def list_users(
    org_id: OrgId,  # ← Injected by dependency. The handler never guesses the org.
    repo: UserRepository = Depends(get_user_repository),
    limit: int = 50,
    offset: int = 0,
) -> list[UserResponse]:
    """List users in the authenticated organization."""
    users = await repo.get_all(limit=limit, offset=offset)
    return [UserResponse.model_validate(u) for u in users]


@router.post("")
async def create_user(
    org_id: OrgId,  # ← Explicit dependency, even if not used in handler body
    payload: CreateUserRequest,
    service: UserService = Depends(get_user_service),
) -> UserResponse:
    """Create a user in the authenticated organization."""
    user = await service.create_user(
        org_id=org_id,
        external_id=payload.external_id,
        name=payload.name,
        email=payload.email,
    )
    return UserResponse.model_validate(user)
```

### 4.3 What Happens If a Route Forgets `org_id`?

The code **will not compile** — the repository requires `org_id` in its constructor:

```python
# If a route handler forgets to inject org_id:
@router.get("/users")
async def list_users(repo: UserRepository = Depends(get_user_repository)):
    # ❌ UserRepository was instantiated in get_user_repository()
    #    without an org_id — AttributeError at construction time.
    return await repo.get_all()
```

The `get_user_repository` dependency reads `request.state.org_id`, which requires the auth middleware to have run. If the auth middleware didn't run (e.g., route is not behind the auth dependency), `request.state.org_id` is `None`, and the repository will fail immediately.

---

## 5. FalkorDB Graph Namespace Isolation

### 5.1 Namespace Strategy

Each organization's graph data is isolated into a FalkorDB namespace using the prefix `org_{uuid}`:

```python
# packages/graphiti-client/client.py
import redis.asyncio as aioredis
from uuid import UUID


class GraphiteClient:
    """Thin wrapper around Graphiti with tenant isolation.

    Every graph operation is scoped to an organization namespace.
    The namespace is derived from org_id and cannot be overridden
    by callers.
    """

    def __init__(self, redis: aioredis.Redis, org_id: UUID, graphiti_config: dict):
        self._redis = redis
        self._org_id = org_id
        self._namespace = f"org_{org_id}"  # Graph namespace prefix
        self._graphiti = Graphiti(
            redis=redis,
            namespace=self._namespace,  # Isolated namespace in FalkorDB
            **graphiti_config,
        )

    @property
    def namespace(self) -> str:
        """Return the FalkorDB namespace for this organization.

        Graph keys will be: org_{uuid}:entity, org_{uuid}:episode, etc.
        """
        return self._namespace

    async def add_episode(self, episode_data: dict) -> str:
        """Add an episode node. Automatically namespaced to this org."""
        return await self._graphiti.add_episode(episode_data)

    async def search_entities(self, query: str, limit: int = 10) -> list[dict]:
        """Search entities within this org's namespace only."""
        return await self._graphiti.search(query=query, limit=limit)

    # All other methods follow the same pattern —
    # namespace is set at construction and cannot be changed.
```

### 5.2 FalkorDB Key Layout

```
org_{uuid}:
  ├── entities          → Set of entity node keys
  ├── episodes          → Set of episode node keys
  ├── communities       → Set of community node keys
  ├── entity:{uuid}     → Individual entity hash
  ├── episode:{uuid}    → Individual episode hash
  ├── community:{uuid}  → Individual community hash
  └── edges:{uuid}      → Adjacency list for each entity
```

This means:
- A single FalkorDB instance serves all organizations
- There is no graph query that can traverse from `org_a` to `org_b` — namespaces are hard-separated
- Backup/restore can be scoped to a single namespace

### 5.3 Client Instantiation

```python
# services/graph_service.py
from packages.graphiti_client import GraphiteClient


class GraphService:
    def __init__(self, org_id: UUID, redis: aioredis.Redis, config: dict):
        self._client = GraphiteClient(redis=redis, org_id=org_id, graphiti_config=config)

    async def add_entity(self, entity_data: dict) -> dict:
        node_id = await self._client.add_episode(entity_data)
        return {"node_id": node_id}

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        return await self._client.search_entities(query=query, limit=limit)
```

**Key invariant:** The `GraphiteClient` is instantiated **per-request** (fast, no I/O) with the authenticated org's namespace. It is never reused across organizations.

---

## 6. `graphiti-client`: Mandatory `org_id` Parameter

The `graphiti-client` package (thin wrapper around the Graphiti library) enforces `org_id` as a **mandatory parameter** on every public method. This prevents a developer from accidentally omitting the tenant context:

```python
# packages/graphiti-client/client.py
from uuid import UUID
from typing import Any, Optional


class TenantAwareGraphiteClient:
    """Graphiti wrapper with mandatory org_id on every method.

    Unlike the per-request GraphiteClient class above, this client
    is a single-instance wrapper that requires org_id on each call.
    Use whichever pattern fits your service architecture.

    Both enforce the same invariant: no org_id, no graph operation.
    """

    def __init__(self, redis_url: str, graphiti_config: dict):
        self._redis_url = redis_url
        self._config = graphiti_config

    async def add_entity(
        self,
        org_id: UUID,            # ← Mandatory — no default
        name: str,
        entity_type: str,
        metadata: dict | None = None,
    ) -> str:
        """Add an entity node in the org's namespace.

        Args:
            org_id: The tenant UUID. Determines the FalkorDB namespace.
                    Every entity is stored under org_{org_id}:entity:{uuid}.
            name: Entity display name.
            entity_type: Type label (Person, Company, Product, etc.).

        Returns:
            The UUID of the created entity node.
        """
        if not org_id:
            raise ValueError("org_id is required for all graph operations")

        namespace = f"org_{org_id}"
        # ... graphiti call with namespace ...
        # ...

    async def get_entity(
        self,
        org_id: UUID,             # ← Mandatory
        entity_id: str,
    ) -> dict | None:
        """Get an entity node. Only searches within the org's namespace."""
        # ...
```

> **Testing note:** Unit tests for `graphiti-client` should verify that every method raises `ValueError` when `org_id` is `None` or empty.

---

## 7. Super Admin Bypass

### 7.1 When is Bypass Allowed?

Super admins (role: `super_admin`) can operate across organizations. This is required for:
- Creating new organizations on the platform
- Viewing cross-tenant usage metrics in the dashboard
- Investigating support tickets across orgs
- Performing emergency data cleanup

### 7.2 How Bypass Works

The bypass operates at **all three isolation layers**:

| Layer | Bypass Mechanism |
|-------|-----------------|
| **RLS** | `app.bypass_rls = 'true'` → RLS policies return all rows (see `USING` clause in Section 2.1) |
| **Repository** | `TenantAwareRepository` accepts an optional `bypass_rls` flag |
| **API** | `require_super_admin` dependency on the route |

```python
# Example: super admin route that lists all orgs
@router.get("/admin/organizations")
async def list_all_organizations(
    admin: AdminUser = Depends(require_super_admin),  # ← Role check
    # No org_id dependency — this is intentionally cross-tenant
    service: AdminService = Depends(get_admin_service),
):
    """List all organizations. Super admin only."""
    orgs = await service.list_all_organizations()
    return orgs


# Example: super admin repository bypass
class SuperAdminRepository:
    """Repository for super admin operations — no tenant filter."""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def get_all_users_across_orgs(self, limit: int = 50) -> list[User]:
        stmt = select(User)  # ← No _apply_org_filter — intentionally cross-tenant
        result = await self._db.execute(stmt.limit(limit))
        return result.scalars().all()
```

### 7.3 Audit Requirement

Every super admin cross-tenant action **must be logged**:

```python
async def audit_super_admin_action(
    admin_id: UUID,
    action: str,
    target_org_id: UUID | None,
    details: dict,
) -> None:
    """Log super admin cross-tenant access for compliance.

    Stored in the audit_log table (immutable, append-only).
    """
    logger.info("auth.super_admin_action", extra={
        "admin_id": str(admin_id),
        "action": action,
        "target_org_id": str(target_org_id) if target_org_id else None,
        "details": details,
    })
    # Also persist to audit_log table
    await db.execute(
        text("""
            INSERT INTO audit_log (admin_id, action, target_org_id, details)
            VALUES (:admin_id, :action, :target_org_id, :details::jsonb)
        """),
        {
            "admin_id": admin_id,
            "action": action,
            "target_org_id": target_org_id,
            "details": json.dumps(details),
        },
    )
    await db.commit()
```

---

## 8. Error Behaviour — 404 NOT FOUND

### 8.1 Why 404 and Not 403

When an authenticated user attempts to access a resource in another organization, the system returns **404 Not Found** — never **403 Forbidden**.

```python
# Example: cross-tenant user access
@router.get("/v1/users/{user_id}")
async def get_user(
    user_id: str,
    org_id: OrgId,
    repo: UserRepository = Depends(get_user_repository),
) -> UserResponse:
    user = await repo.get_by_external_id(user_id)
    if not user:
        # We don't know if the user exists in another org.
        # Return 404 regardless — no information about the resource's existence.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "USER_NOT_FOUND",
                "message": f"User '{user_id}' not found.",
            },
        )
    return UserResponse.model_validate(user)
```

**Rationale:**

- Returning 403 would confirm that the resource **exists** but belongs to another org — leaking information
- Returning 404 is ambiguous: the resource might not exist at all, or it might belong to another tenant
- This is a standard security practice (used by AWS, GitHub, etc.)

### 8.2 The `safe_get` Pattern

For routes where the resource is expected to exist (e.g., fetching a user's sessions after user creation), use the same pattern — 404 on not found, never 403:

```python
async def get_session_or_404(
    session_id: str,
    user_id: str,
    org_id: UUID,
) -> Session:
    """Get a session with implicit tenant and user scope check.

    The TenantAwareRepository ensures the session belongs to the org.
    If it's not found, it could mean:
    - The session doesn't exist
    - The session belongs to another org
    - The session belongs to another user

    All three cases return the same 404.
    """
    repo = SessionRepository(db=get_db(), org_id=org_id)
    session = await repo.get_by_external_id(
        user_id=UUID(user_id),
        external_id=session_id,
    )
    if not session:
        raise HTTPException(status_code=404, detail={
            "code": "SESSION_NOT_FOUND",
            "message": f"Session '{session_id}' not found.",
        })
    return session
```

---

## 9. Cross-Tenant Test Matrix

> **Full testing doc:** [14-testing/04-cross-tenant-test-matrix.md](../14-testing/04-cross-tenant-test-matrix.md)

The following matrix must pass for every P0 endpoint before the release:

| Test Case | Org A Identity | Target Resource | Expected Result |
|-----------|---------------|-----------------|-----------------|
| Same-org read | `org_a` key | `org_a` user | 200 OK |
| Same-org write | `org_a` key | `org_a` user | 201/200 Created |
| Cross-org read | `org_a` key | `org_b` user | 404 NOT FOUND |
| Cross-org write | `org_a` key | `org_b` user | 404 NOT FOUND |
| Unauthenticated | No key | Any user | 401 UNAUTHORIZED |
| Expired key | `org_a` expired key | `org_a` user | 401 UNAUTHORIZED |
| Wrong-scope key | `org_a` read-only key | Write endpoint | 403 FORBIDDEN |
| Super admin cross-org | Super admin JWT | `org_b` user | 200 OK (audit logged) |
| Graph cross-org | `org_a` key | `org_b` namespace | Empty results / 404 |
| Deleted org | `deleted_org` key | Any user | 401 UNAUTHORIZED |

**Automation:** These tests run as parameterized integration tests using `pytest`:

```python
# tests/integration/test_cross_tenant.py
import pytest

TENANTS = ["org_a", "org_b", "org_c"]

@pytest.mark.parametrize("target_org", TENANTS)
@pytest.mark.parametrize("auth_org", TENANTS)
async def test_cross_tenant_user_access(
    auth_org: str,
    target_org: str,
    test_data_factory,
    async_client,
):
    """N×N matrix test: every org trying to access every org's users."""
    key = test_data_factory.get_api_key(auth_org)
    user_id = test_data_factory.get_user_id(target_org)

    resp = await async_client.get(
        f"/v1/users/{user_id}",
        headers={"Authorization": f"Bearer {key}"},
    )

    if auth_org == target_org:
        assert resp.status_code == 200
    else:
        assert resp.status_code == 404  # No info leak
```

---

## 10. Implementation Checklist

Use this checklist when adding a new table or endpoint to ensure tenant isolation:

### For New Tables

- [ ] Table has `organization_id` column (directly or via foreign key chain)
- [ ] RLS policy created in migration (`CREATE POLICY org_isolation_...`)
- [ ] `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` executed
- [ ] Repository inherits from `TenantAwareRepository` or uses the org join pattern
- [ ] Repository constructor requires `org_id` parameter
- [ ] All queries use `_apply_org_filter()` or explicit join filter

### For New Endpoints

- [ ] Route has `org_id: OrgId` dependency
- [ ] Repository instantiated with `org_id` from auth context
- [ ] Error responses return 404 (not 403) for cross-tenant access
- [ ] Cross-tenant test case added to the matrix

### For Graph Operations

- [ ] `org_id` is a mandatory parameter on every method call
- [ ] FalkorDB namespace uses `org_{uuid}` prefix
- [ ] No graph query can cross namespace boundaries

---

> **Commit convention:** `feat(auth): implement multi-tenant isolation with RLS + repo + DI layers`
> **Review checklist:** Verify RLS policies exist on ALL tenant-scoped tables, confirm 404 (not 403) on cross-tenant access, check super admin audit logging, validate graph namespace isolation.
