# User CRUD Implementation Guide

> **Phase:** Phase 1 — Core Memory (Week 3-4)
> **Priority:** P0
> **Requirements:** USR-01, USR-02, USR-03, USR-04, USR-05, ING-05
> **Handoff from:** Architect (ADR-003: User & Session Data Model)

---

## 1. Overview

Users are the top-level entity in MemGraph's tenant hierarchy. Every piece of data — sessions, messages, facts, graph nodes — is scoped to a user within an organization. This document covers the complete User CRUD implementation, including auto-creation on memory ingestion, cursor-based pagination, search, and aggregated stats.

---

## 2. Pydantic Schemas

Located at `services/api/schemas/users.py`.

### 2.1 CreateUserRequest

```python
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from uuid import UUID


class CreateUserRequest(BaseModel):
    """Schema for creating a new user.

    The caller provides an `external_id` (their own user identifier).
    `name`, `email`, and `metadata` are optional.
    """
    external_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Caller-chosen unique identifier for this user, "
                    "scoped to the organization.",
        examples=["user_abc123", "alice@example.com"],
    )
    name: Optional[str] = Field(
        None,
        max_length=1024,
        description="Display name for the user.",
    )
    email: Optional[str] = Field(
        None,
        max_length=1024,
        description="Email address of the user.",
        examples=["alice@example.com"],
    )
    metadata: Optional[dict] = Field(
        None,
        description="Arbitrary caller-defined metadata (JSON object). "
                    "Max depth: 5 levels. Max keys: 50. Max string value length: 1024.",
    )

    @field_validator("external_id")
    @classmethod
    def external_id_must_not_be_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("external_id must not be empty or whitespace-only")
        return stripped

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, v: Optional[dict]) -> Optional[dict]:
        if v is None:
            return v
        _validate_jsonb_depth(v, max_depth=5)
        _validate_jsonb_key_count(v, max_keys=50)
        _validate_jsonb_string_lengths(v, max_length=1024)
        return v
```

### 2.2 UserResponse

```python
from datetime import datetime
from uuid import UUID


class UserResponse(BaseModel):
    """Public user representation returned by the API.

    Never exposes internal `organization_id` directly — the caller's
    organization is inferred from their API key.
    """
    id: UUID = Field(..., description="Internal MemGraph user UUID.")
    external_id: str = Field(..., description="Caller-chosen user identifier.")
    name: Optional[str] = Field(None, description="Display name.")
    email: Optional[str] = Field(None, description="Email address.")
    metadata: dict = Field(
        default_factory=dict,
        description="Arbitrary caller-defined metadata.",
    )
    created_at: datetime = Field(..., description="When the user was created.")
    updated_at: datetime = Field(..., description="When the user was last updated.")

    model_config = ConfigDict(from_attributes=True)
```

### 2.3 UserResponseWithStats

Extended response used in list and detail endpoints — includes aggregated stats.

```python
class UserStats(BaseModel):
    """Aggregated statistics for a user."""
    message_count: int = Field(..., description="Total number of episodes (messages).")
    fact_count: int = Field(..., description="Total number of extracted facts.")
    session_count: int = Field(..., description="Total number of sessions.")


class UserResponseWithStats(UserResponse):
    """User response with aggregate statistics."""
    stats: UserStats = Field(..., description="Aggregated usage statistics.")
```

### 2.4 UpdateUserRequest

```python
class UpdateUserRequest(BaseModel):
    """Schema for updating a user. All fields are optional — only provided fields are updated.

    Metadata uses merge semantics: provided top-level keys are merged into
    existing metadata, not replaced entirely.
    """
    name: Optional[str] = Field(None, max_length=1024)
    email: Optional[str] = Field(None, max_length=1024)
    metadata: Optional[dict] = None

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, v: Optional[dict]) -> Optional[dict]:
        if v is None:
            return v
        _validate_jsonb_depth(v, max_depth=5)
        _validate_jsonb_key_count(v, max_keys=50)
        _validate_jsonb_string_lengths(v, max_length=1024)
        return v
```

### 2.5 UserListResponse

```python
from typing import List


class UserListResponse(BaseModel):
    """Cursor-paginated list response for users."""
    data: List[UserResponseWithStats] = Field(..., description="List of users.")
    next_cursor: Optional[str] = Field(
        None,
        description="Opaque cursor for the next page. Omitted on last page.",
    )
    has_more: bool = Field(
        ...,
        description="True if there are more results beyond this page.",
    )
    total: Optional[int] = Field(
        None,
        description="Total number of users (only present if ?include_total=true).",
    )
```

---

## 3. Data Model (SQLAlchemy)

Located at `packages/core/models/user.py`.

```python
import uuid
from datetime import datetime
from sqlalchemy import String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "external_id",
            name="uq_users_org_external_id",
        ),
        # Index for listing: ordered by created_at DESC, filtered by org
        # Index for search: GIN on metadata, btree on name/email
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_id: Mapped[str] = mapped_column(
        String(255), nullable=False,
    )
    name: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    email: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    metadata: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(
        default=False, nullable=False,
        comment="Soft-delete flag for GDPR grace period.",
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="When the user was soft-deleted.",
    )
```

### Indexes

```sql
-- Primary listing index: organization-scoped, ordered by creation date
CREATE INDEX ix_users_org_created_at
    ON users (organization_id, created_at DESC);

-- Search indexes
CREATE INDEX ix_users_external_id ON users (organization_id, external_id);
CREATE INDEX ix_users_name ON users USING gin (name gin_trgm_ops);
CREATE INDEX ix_users_email ON users USING gin (email gin_trgm_ops);
CREATE INDEX ix_users_metadata ON users USING gin (metadata jsonb_path_ops);
```

---

## 4. Repository Layer

Located at `packages/core/repositories/user_repository.py`.

```python
from uuid import UUID
from typing import Optional, Tuple, List
from datetime import datetime
from sqlalchemy import select, func, or_, and_, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload


class UserRepository:
    """All DB access for users. No business logic — just queries."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Basic CRUD ──────────────────────────────────────────────────────

    async def create(
        self,
        organization_id: UUID,
        external_id: str,
        name: Optional[str] = None,
        email: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> User:
        user = User(
            organization_id=organization_id,
            external_id=external_id,
            name=name,
            email=email,
            metadata=metadata or {},
        )
        self._db.add(user)
        await self._db.flush()
        await self._db.refresh(user)
        return user

    async def get_by_id(
        self, user_id: UUID, organization_id: UUID,
    ) -> Optional[User]:
        result = await self._db.execute(
            select(User).where(
                User.id == user_id,
                User.organization_id == organization_id,
                User.is_deleted == False,  # noqa: E712
            )
        )
        return result.scalar_one_or_none()

    async def get_by_external_id(
        self, external_id: str, organization_id: UUID,
    ) -> Optional[User]:
        result = await self._db.execute(
            select(User).where(
                User.external_id == external_id,
                User.organization_id == organization_id,
                User.is_deleted == False,  # noqa: E712
            )
        )
        return result.scalar_one_or_none()

    async def update(
        self,
        user: User,
        name: Optional[str] = None,
        email: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> User:
        if name is not None:
            user.name = name
        if email is not None:
            user.email = email
        if metadata is not None:
            # Merge: top-level keys overwrite existing, others preserved
            existing = dict(user.metadata or {})
            existing.update(metadata)
            user.metadata = existing
        user.updated_at = func.now()
        await self._db.flush()
        await self._db.refresh(user)
        return user

    async def soft_delete(self, user: User) -> None:
        """Mark user as deleted for GDPR grace period."""
        user.is_deleted = True
        user.deleted_at = func.now()
        await self._db.flush()

    async def hard_delete(self, user: User) -> None:
        """Permanently delete user record."""
        await self._db.delete(user)
        await self._db.flush()

    # ── Listing with cursor-based pagination ────────────────────────────

    async def list_paginated(
        self,
        organization_id: UUID,
        limit: int = 50,
        cursor: Optional[str] = None,
        search: Optional[str] = None,
        external_id_filter: Optional[str] = None,
        email_filter: Optional[str] = None,
        metadata_filter: Optional[dict] = None,
    ) -> Tuple[List[User], Optional[str], bool]:
        """Return (users, next_cursor, has_more).

        Cursor format: base64("{created_at.isoformat()}:{id}").
        Ordered by created_at DESC, id DESC.
        """
        query = select(User).where(
            User.organization_id == organization_id,
            User.is_deleted == False,  # noqa: E712
        )

        # ── Search filters ──────────────────────────────────────────
        if search:
            search_pattern = f"%{search}%"
            query = query.where(
                or_(
                    User.external_id.ilike(search_pattern),
                    User.name.ilike(search_pattern),
                    User.email.ilike(search_pattern),
                )
            )

        if external_id_filter:
            query = query.where(User.external_id.ilike(f"%{external_id_filter}%"))

        if email_filter:
            query = query.where(User.email.ilike(f"%{email_filter}%"))

        if metadata_filter:
            # JSONB containment query: metadata @> metadata_filter
            query = query.where(User.metadata.contains(metadata_filter))

        # ── Cursor ──────────────────────────────────────────────────
        if cursor:
            cursor_date, cursor_id = decode_cursor(cursor)
            query = query.where(
                or_(
                    and_(
                        User.created_at == cursor_date,
                        User.id < cursor_id,  # id DESC tiebreaker
                    ),
                    and_(
                        User.created_at < cursor_date,
                    ),
                )
            )

        # ── Ordering + Limit ────────────────────────────────────────
        query = query.order_by(
            User.created_at.desc(),
            User.id.desc(),
        ).limit(limit + 1)  # Fetch one extra to detect has_more

        result = await self._db.execute(query)
        users = list(result.scalars().all())

        has_more = len(users) > limit
        if has_more:
            users = users[:limit]

        next_cursor = None
        if has_more and users:
            last = users[-1]
            next_cursor = encode_cursor(last.created_at, last.id)

        return users, next_cursor, has_more

    # ── Stats ──────────────────────────────────────────────────────────

    async def get_user_stats(
        self, user_id: UUID,
    ) -> dict:
        """Return aggregated stats for a user.

        Single aggregate query — no N+1.
        """
        from models.episode import Episode
        from models.fact import Fact

        # Use correlated subqueries for efficiency
        message_count = (
            select(func.count(Episode.id))
            .where(Episode.user_id == user_id)
            .correlate(User)
            .scalar_subquery()
        )
        fact_count = (
            select(func.count(Fact.id))
            .where(Fact.user_id == user_id)
            .correlate(User)
            .scalar_subquery()
        )

        result = await self._db.execute(
            select(message_count, fact_count)
        )
        row = result.one()
        return {
            "message_count": row[0] or 0,
            "fact_count": row[1] or 0,
        }

    async def count_total(
        self, organization_id: UUID,
    ) -> int:
        """Expensive COUNT query — only used when ?include_total=true."""
        result = await self._db.execute(
            select(func.count(User.id)).where(
                User.organization_id == organization_id,
                User.is_deleted == False,  # noqa: E712
            )
        )
        return result.scalar() or 0

    # ── Existence check ─────────────────────────────────────────────────

    async def exists_by_external_id(
        self, external_id: str, organization_id: UUID,
    ) -> bool:
        result = await self._db.execute(
            select(select(User).where(
                User.external_id == external_id,
                User.organization_id == organization_id,
                User.is_deleted == False,  # noqa: E712
            ).exists().select())
        )
        return result.scalar() or False
```

---

## 5. Service Layer

Located at `services/api/services/user_service.py`.

```python
from uuid import UUID
from typing import Optional, Tuple, List


class UserService:
    """All business logic for user operations.

    Delegates all DB access to UserRepository.
    Delegates external effects (graph, cache) to respective clients.
    """

    def __init__(
        self,
        repo: UserRepository,
        graphiti_client: GraphitiClient,
        cache: RedisCache,
    ) -> None:
        self._repo = repo
        self._graphiti = graphiti_client
        self._cache = cache

    # ── Create ──────────────────────────────────────────────────────────

    async def create_user(
        self,
        organization_id: UUID,
        request: CreateUserRequest,
    ) -> UserResponseWithStats:
        """Create a new user.

        Raises:
            ValidationError: If external_id already exists in this organization.
        """
        existing = await self._repo.get_by_external_id(
            request.external_id, organization_id,
        )
        if existing:
            raise ValidationError(
                f"User with external_id '{request.external_id}' "
                f"already exists in this organization."
            )

        user = await self._repo.create(
            organization_id=organization_id,
            external_id=request.external_id,
            name=request.name,
            email=request.email,
            metadata=request.metadata,
        )
        return await self._build_response_with_stats(user)

    # ── Auto-create (on memory ingestion) ───────────────────────────────

    async def get_or_create_user(
        self,
        organization_id: UUID,
        external_id: str,
        auto_create: bool = True,
    ) -> User:
        """Retrieve a user by external_id, or auto-create if configured.

        This is called during memory ingestion (POST /memory) when the
        caller references a user that doesn't exist yet.

        Args:
            organization_id: Tenant scope.
            external_id: Caller's user identifier.
            auto_create: If True (default), create the user on miss.
                         If False, raise NotFoundError.

        Returns:
            The existing or newly created User.

        Raises:
            NotFoundError: If the user doesn't exist and auto_create is False.
        """
        user = await self._repo.get_by_external_id(external_id, organization_id)
        if user:
            return user

        if not auto_create:
            raise NotFoundError(
                f"User '{external_id}' not found in organization "
                f"and auto_create is disabled."
            )

        return await self._repo.create(
            organization_id=organization_id,
            external_id=external_id,
        )

    # ── Get ─────────────────────────────────────────────────────────────

    async def get_user(
        self, user_id: UUID, organization_id: UUID,
    ) -> UserResponseWithStats:
        user = await self._repo.get_by_id(user_id, organization_id)
        if not user:
            raise NotFoundError(f"User '{user_id}' not found.")
        return await self._build_response_with_stats(user)

    # ── Update ──────────────────────────────────────────────────────────

    async def update_user(
        self,
        user_id: UUID,
        organization_id: UUID,
        request: UpdateUserRequest,
    ) -> UserResponseWithStats:
        user = await self._repo.get_by_id(user_id, organization_id)
        if not user:
            raise NotFoundError(f"User '{user_id}' not found.")

        updated = await self._repo.update(
            user=user,
            name=request.name,
            email=request.email,
            metadata=request.metadata,
        )
        await self._invalidate_user_cache(user_id)
        return await self._build_response_with_stats(updated)

    # ── Delete ──────────────────────────────────────────────────────────

    async def delete_user(
        self, user_id: UUID, organization_id: UUID,
    ) -> None:
        """Soft-delete a user.

        Actual deletion happens via the GDPR worker task after the
        configurable grace period (see GDPR compliance guide).
        """
        user = await self._repo.get_by_id(user_id, organization_id)
        if not user:
            raise NotFoundError(f"User '{user_id}' not found.")

        await self._repo.soft_delete(user)
        await self._invalidate_user_cache(user_id)

        # Enqueue async cleanup task (graph, cache, jobs)
        await self._enqueue_user_deletion(user_id, organization_id)

    # ── List ────────────────────────────────────────────────────────────

    async def list_users(
        self,
        organization_id: UUID,
        limit: int = 50,
        cursor: Optional[str] = None,
        search: Optional[str] = None,
        external_id: Optional[str] = None,
        email: Optional[str] = None,
        metadata: Optional[dict] = None,
        include_total: bool = False,
    ) -> UserListResponse:
        users, next_cursor, has_more = await self._repo.list_paginated(
            organization_id=organization_id,
            limit=min(limit, 200),  # Cap at max page size
            cursor=cursor,
            search=search,
            external_id_filter=external_id,
            email_filter=email,
            metadata_filter=metadata,
        )

        # Build responses with stats
        user_responses = [
            await self._build_response_with_stats(u) for u in users
        ]

        total = None
        if include_total:
            total = await self._repo.count_total(organization_id)

        return UserListResponse(
            data=user_responses,
            next_cursor=next_cursor,
            has_more=has_more,
            total=total,
        )

    # ── Private helpers ─────────────────────────────────────────────────

    async def _build_response_with_stats(
        self, user: User,
    ) -> UserResponseWithStats:
        stats = await self._repo.get_user_stats(user.id)
        return UserResponseWithStats(
            id=user.id,
            external_id=user.external_id,
            name=user.name,
            email=user.email,
            metadata=user.metadata,
            created_at=user.created_at,
            updated_at=user.updated_at,
            stats=UserStats(**stats),
        )

    async def _invalidate_user_cache(self, user_id: UUID) -> None:
        await self._cache.delete_pattern(f"user:{user_id}:*")

    async def _enqueue_user_deletion(
        self, user_id: UUID, organization_id: UUID,
    ) -> None:
        """Enqueue the asynchronous full user deletion task.

        The worker handles: cascade delete from all tables, graph node
        deletion, cache invalidation, and ARQ job cancellation.
        """
        await self._arq_queue.enqueue(
            "delete_user_data",
            user_id=str(user_id),
            organization_id=str(organization_id),
            _job_id=f"delete_user_{user_id}",
            _defer_until=datetime.utcnow() + timedelta(
                seconds=self._settings.SOFT_DELETE_GRACE_SECONDS
            ),
        )
```

---

## 6. Router Layer

Located at `services/api/routers/users.py`.

```python
from fastapi import APIRouter, Depends, Query, Path, status
from uuid import UUID

router = APIRouter(prefix="/v1/users", tags=["users"])


@router.post("", response_model=UserResponseWithStats, status_code=201)
async def create_user(
    request: CreateUserRequest,
    service: UserService = Depends(get_user_service),
    org: Organization = Depends(get_current_organization),
) -> UserResponseWithStats:
    """Create a new user.

    The `external_id` is your identifier for this user (e.g., user ID
    from your application). It must be unique within your organization.
    """
    return await service.create_user(
        organization_id=org.id,
        request=request,
    )


@router.get("", response_model=UserListResponse)
async def list_users(
    service: UserService = Depends(get_user_service),
    org: Organization = Depends(get_current_organization),
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None),
    search: Optional[str] = Query(None, max_length=256),
    external_id: Optional[str] = Query(None, max_length=255),
    email: Optional[str] = Query(None, max_length=255),
    metadata: Optional[str] = Query(
        None,
        description="JSON object for metadata filtering (JSONB containment).",
    ),
    include_total: bool = Query(False),
) -> UserListResponse:
    """List all users with cursor-based pagination.

    Supports search by external_id, name, email, and JSONB metadata
    containment queries. Results ordered by created_at DESC.
    """
    metadata_filter = json.loads(metadata) if metadata else None
    return await service.list_users(
        organization_id=org.id,
        limit=limit,
        cursor=cursor,
        search=search,
        external_id=external_id,
        email=email,
        metadata=metadata_filter,
        include_total=include_total,
    )


@router.get("/{user_id}", response_model=UserResponseWithStats)
async def get_user(
    user_id: UUID = Path(..., description="Internal MemGraph user UUID"),
    service: UserService = Depends(get_user_service),
    org: Organization = Depends(get_current_organization),
) -> UserResponseWithStats:
    """Get a user by their internal UUID, including aggregated stats."""
    return await service.get_user(
        user_id=user_id,
        organization_id=org.id,
    )


@router.patch("/{user_id}", response_model=UserResponseWithStats)
async def update_user(
    request: UpdateUserRequest,
    user_id: UUID = Path(...),
    service: UserService = Depends(get_user_service),
    org: Organization = Depends(get_current_organization),
) -> UserResponseWithStats:
    """Update a user's metadata.

    Metadata uses merge semantics: provided top-level keys overwrite
    existing ones; omitted keys are preserved.
    """
    return await service.update_user(
        user_id=user_id,
        organization_id=org.id,
        request=request,
    )


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: UUID = Path(...),
    service: UserService = Depends(get_user_service),
    org: Organization = Depends(get_current_organization),
) -> None:
    """Delete a user and all associated data.

    This is a soft-delete. All data is permanently removed after the
    configurable grace period (default: 30 days). During the grace period,
    the user is hidden from all queries but can be restored by contacting
    support.
    """
    await service.delete_user(
        user_id=user_id,
        organization_id=org.id,
    )
```

---

## 7. Cursor Encoding / Decoding

Located at `packages/core/utils/cursor.py`.

```python
import base64
import json
from datetime import datetime
from uuid import UUID


def encode_cursor(date: datetime, id: UUID) -> str:
    """Encode a cursor from a sort field value and ID.

    Format: base64(json([iso_timestamp, uuid_string]))
    """
    payload = json.dumps([date.isoformat(), str(id)])
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Decode a cursor back to (datetime, UUID).

    Raises:
        ValidationError: If cursor format is invalid.
    """
    try:
        # Add padding if stripped
        padding = 4 - (len(cursor) % 4)
        if padding != 4:
            cursor += "=" * padding
        payload = base64.urlsafe_b64decode(cursor.encode()).decode()
        date_str, id_str = json.loads(payload)
        return datetime.fromisoformat(date_str), UUID(id_str)
    except (ValueError, json.JSONDecodeError, IndexError) as e:
        raise ValidationError(f"Invalid cursor format: {e}")
```

---

## 8. Auto-Creation Configuration

The auto-creation behaviour on `POST /memory` is controlled by an environment variable:

```python
# core/config.py
class Settings(BaseSettings):
    # ... other settings ...

    USER_AUTO_CREATE: bool = Field(
        default=True,
        description="Auto-create users on memory ingestion if they don't exist. "
                    "Set to false to require explicit user creation.",
    )
```

When disabled, `POST /v1/users/{user_id}/memory` returns **404 Not Found** if the user doesn't exist, rather than auto-creating.

---

## 9. Error Scenarios

| Scenario | HTTP Status | Error Code | Detail |
|---|---|---|---|
| External ID already exists | 409 Conflict | `RESOURCE_CONFLICT` | "User with external_id 'X' already exists" |
| User not found | 404 Not Found | `RESOURCE_NOT_FOUND` | "User 'uuid' not found" |
| User not found (auto-create disabled) | 404 Not Found | `RESOURCE_NOT_FOUND` | "User 'X' not found in organization and auto_create is disabled" |
| Invalid cursor | 400 Bad Request | `INVALID_CURSOR` | "Invalid cursor format" |
| Metadata too deep/nested | 422 Unprocessable | `VALIDATION_ERROR` | Field-level error on metadata |
| Non-existent org (auth layer) | 401 Unauthorized | `UNAUTHORIZED` | Handled by auth middleware |

---

## 10. Testing

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_user(
    async_client: AsyncClient,
    auth_headers: dict,
) -> None:
    response = await async_client.post(
        "/v1/users",
        json={"external_id": "alice_123", "name": "Alice"},
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.json()
    assert data["external_id"] == "alice_123"
    assert data["stats"]["message_count"] == 0
    assert data["stats"]["fact_count"] == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_duplicate_external_id(
    async_client: AsyncClient,
    auth_headers: dict,
    existing_user: User,
) -> None:
    response = await async_client.post(
        "/v1/users",
        json={"external_id": existing_user.external_id},
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_users_pagination(
    async_client: AsyncClient,
    auth_headers: dict,
) -> None:
    # Create 55 users
    for i in range(55):
        await async_client.post(
            "/v1/users",
            json={"external_id": f"user_{i}"},
            headers=auth_headers,
        )

    # First page
    response = await async_client.get(
        "/v1/users?limit=50", headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["data"]) == 50
    assert data["has_more"] is True
    assert data["next_cursor"] is not None

    # Second page
    response = await async_client.get(
        f"/v1/users?limit=50&cursor={data['next_cursor']}",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["data"]) == 5
    assert data["has_more"] is False
    assert data["next_cursor"] is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_delete_user_cascade(
    async_client: AsyncClient,
    auth_headers: dict,
    existing_user_with_data: User,  # fixture with sessions, messages, facts
) -> None:
    response = await async_client.delete(
        f"/v1/users/{existing_user_with_data.id}",
        headers=auth_headers,
    )
    assert response.status_code == 204
    # Verify soft-deleted
    get_resp = await async_client.get(
        f"/v1/users/{existing_user_with_data.id}",
        headers=auth_headers,
    )
    assert get_resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
async def test_search_users(
    async_client: AsyncClient,
    auth_headers: dict,
) -> None:
    # Create users with searchable fields
    await async_client.post(
        "/v1/users", json={"external_id": "bob", "email": "bob@test.com"},
        headers=auth_headers,
    )
    await async_client.post(
        "/v1/users", json={"external_id": "alice", "email": "alice@test.com"},
        headers=auth_headers,
    )

    response = await async_client.get(
        "/v1/users?search=bob", headers=auth_headers,
    )
    assert response.status_code == 200
    assert len(response.json()["data"]) == 1
    assert response.json()["data"][0]["external_id"] == "bob"
```

---

## 11. Migration

```python
"""add_users_table

Revision ID: 001
Revises: 000_organizations
Create Date: 2026-06-05

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "001"
down_revision = "000_organizations"


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(1024), nullable=True),
        sa.Column("email", sa.String(1024), nullable=True),
        sa.Column("metadata", JSONB, server_default="{}", nullable=False),
        sa.Column("is_deleted", sa.Boolean, server_default="false", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("organization_id", "external_id", name="uq_users_org_external_id"),
    )
    op.create_index("ix_users_org_created_at", "users", ["organization_id", sa.text("created_at DESC")])
    op.create_index("ix_users_external_id", "users", ["organization_id", "external_id"])
    op.create_index("ix_users_name", "users", sa.text("name gin_trgm_ops"), postgresql_using="gin")
    op.create_index("ix_users_email", "users", sa.text("email gin_trgm_ops"), postgresql_using="gin")
    op.create_index("ix_users_metadata", "users", sa.text("metadata jsonb_path_ops"), postgresql_using="gin")


def downgrade() -> None:
    op.drop_table("users")
```

---

## 12. Sequence Diagram

```
Caller                    FastAPI                  UserService              UserRepository          PostgreSQL
  │                         │                         │                        │                      │
  │  POST /v1/users         │                         │                        │                      │
  │  {external_id, name}    │                         │                        │                      │
  │ ──────────────────────► │                         │                        │                      │
  │                         │  validate + auth         │                        │                      │
  │                         │ ───────────────────────► │                        │                      │
  │                         │                         │ get_by_external_id()   │                      │
  │                         │                         │ ─────────────────────► │                      │
  │                         │                         │                        │ SELECT ...           │
  │                         │                         │                        │ ────────────────────► │
  │                         │                         │                        │ ◄──── row or None ── │
  │                         │                         │ ◄── User or None ───── │                      │
  │                         │                         │                        │                      │
  │                         │                         │ if exists → 409        │                      │
  │                         │                         │                        │                      │
  │                         │                         │ create()               │                      │
  │                         │                         │ ─────────────────────► │                      │
  │                         │                         │                        │ INSERT ...           │
  │                         │                         │                        │ ────────────────────► │
  │                         │                         │                        │ ◄─── new User ────── │
  │                         │                         │ ◄── User ──────────── │                      │
  │                         │                         │                        │                      │
  │                         │                         │ get_user_stats()       │                      │
  │                         │                         │ ─────────────────────► │                      │
  │                         │                         │                        │ SELECT COUNT ...     │
  │                         │                         │                        │ ────────────────────► │
  │                         │                         │                        │ ◄─── stats ───────── │
  │                         │                         │ ◄── stats ─────────── │                      │
  │                         │                         │                        │                      │
  │                         │ ◄─ UserResponseWithStats│                        │                      │
  │                         │                         │                        │                      │
  │ ◄─── 201 Created ────── │                         │                        │                      │
  │    {id, external_id,    │                         │                        │                      │
  │     stats: {...}}       │                         │                        │                      │
```

---

## 13. Performance Considerations

1. **Aggregate query is single SQL**, not N+1: The `get_user_stats` method uses subqueries executed in one round-trip.
2. **Cursor pagination is O(1) per page**: Uses `WHERE (created_at, id) < (:date, :uuid)` — constant-time per page regardless of total rows.
3. **Trigram indexes on name/email**: `gin_trgm_ops` enables fast `ILIKE` searches without full table scans.
4. **JSONB containment is indexed**: `jsonb_path_ops` GIN index accelerates metadata filter queries.
5. **Search uses combined OR**: Ensure `pg_trgm` extension is installed (`CREATE EXTENSION IF NOT EXISTS pg_trgm`).

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
