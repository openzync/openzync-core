# Cursor-Based Pagination Implementation Guide

> **Phase:** Phase 1 — Core Memory (Week 3–4)
> **Priority:** P0
> **Requirements:** USR-05, SES-02, SES-04, SRS §8.5 Pagination
> **Handoff from:** Architect (ADR-005: Pagination Strategy)
> **SRS Reference:** §8.5 Pagination, §7 Data Models (all list endpoints)

---

## 1. Overview

All MemGraph list endpoints use **cursor-based pagination** (keyset pagination) rather than traditional offset/limit pagination. This is a deliberate architectural choice for consistency and performance.

### 1.1 Why Cursor-Based?

| Property | Cursor Pagination | Offset Pagination |
|---|---|---|
| Page N performance | O(log N) — uses index seek | O(N) — must scan past N rows |
| Consistency under writes | ✅ Stable — new records don't shift pages | ❌ Unstable — duplicates or missed items |
| Real-time data | ✅ Safe for append-heavy workloads | ❌ Page boundaries shift on insert |
| Random page access | ❌ Not supported natively | ✅ `?page=5` works |
| Implementation | Slightly more complex | Trivial |

### 1.2 Default Sort Order

| Resource | Sort Field | Direction |
|---|---|---|
| Users | `created_at` | DESC (newest first) |
| Sessions | `created_at` | DESC (newest first) |
| Facts | `created_at` | DESC (newest first) |
| Episodes (messages) | `sequence_number` | ASC (chronological) |
| Graph nodes | `created_at` | DESC (newest first) |

---

## 2. Cursor Format

### 2.1 Standard Cursor (timestamp-based)

```python
# Format: base64url(json([sort_value, id]))
# Example:
#   Input:  created_at="2026-01-01T00:00:00Z", id="550e8400-e29b-41d4-a716-446655440000"
#   Output: c_eyIyMDI2LTAxLTAxVDAwOjAwOjAwWiIsICI1NTBlODQwMC1lMjliLTRkNC1hNzE2LTQ0NjY1NTQ0MDAwMCJ9
```

Implementation in `packages/core/utils/cursor.py`:

```python
import base64
import json
from datetime import datetime
from uuid import UUID

from core.exceptions import ValidationError


def encode_cursor(sort_value: datetime | int, id: UUID) -> str:
    """Encode a cursor from a sort field value and resource UUID.

    Args:
        sort_value: The value of the sort field.
                    datetime for created_at-based sorting.
                    int for sequence_number-based sorting.
        id: The resource UUID (used as tiebreaker for identical sort values).

    Returns:
        URL-safe base64-encoded cursor string (no padding).
        The cursor is opaque to clients and should be passed as-is.

    Example:
        >>> encode_cursor(datetime(2026, 1, 1, tzinfo=timezone.utc), uuid4())
        'c_eyIyMDI2LTAxLTAxVDAwOjAwOjAwWiIsICI1NTBl...'
    """
    if isinstance(sort_value, datetime):
        # Always serialize as ISO-8601 with timezone
        sort_value = sort_value.isoformat()
    payload = json.dumps([sort_value, str(id)], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple:
    """Decode a cursor back to (sort_value, UUID).

    Args:
        cursor: The opaque cursor string from a previous response's
                next_cursor field.

    Returns:
        Tuple of (sort_value, id).
        sort_value may be a str (ISO datetime) or int, depending on
        the sort field of the original query.

    Raises:
        ValidationError: If the cursor format is invalid, tampered,
                        or decoding fails.

    Example:
        >>> decode_cursor("c_eyIyMDI2LTAxLTAxVDAwOjAwOjAwWiIsICI1NTBl...")
        ('2026-01-01T00:00:00+00:00', UUID('550e8400-...'))
    """
    try:
        # Restore padding that was stripped by rstrip("=")
        padding = 4 - (len(cursor) % 4)
        if padding != 4:
            cursor += "=" * padding

        payload = base64.urlsafe_b64decode(cursor.encode()).decode()
        sort_value, id_str = json.loads(payload)
        return sort_value, UUID(id_str)
    except (ValueError, json.JSONDecodeError, IndexError, TypeError) as e:
        raise ValidationError(
            detail=f"Invalid cursor format: {e}",
            code="INVALID_CURSOR",
        )
```

### 2.2 Sequence Cursor (for messages)

Messages within a session are ordered by `sequence_number ASC` (not `created_at`) to avoid tie issues when multiple messages share the same timestamp:

```python
def encode_sequence_cursor(sequence_number: int, id: UUID) -> str:
    """Encode cursor using sequence_number for message ordering."""
    return encode_cursor(sequence_number, id)


def decode_sequence_cursor(cursor: str) -> tuple[int, UUID]:
    """Decode sequence-based cursor.

    Returns:
        Tuple of (sequence_number, id).
    """
    sort_value, id_ = decode_cursor(cursor)
    return int(sort_value), id_
```

---

## 3. Query Parameters

All list endpoints accept the same pagination parameters:

| Parameter | Type | Default | Max | Description |
|---|---|---|---|---|
| `limit` | integer | 50 | 200 | Number of items per page |
| `cursor` | string | null | — | Opaque cursor from previous page response. Omit for first page. |
| `include_total` | boolean | false | — | If true, include total count (expensive, see §6) |

### 3.1 Request Examples

```bash
# First page (default page size)
GET /v1/users?limit=50

# Second page (using cursor from previous response)
GET /v1/users?limit=50&cursor=c_eyIyMDI2LTA2LTAzVDEy...

# Small page
GET /v1/users?limit=10&cursor=c_eyIyMDI2LTA2LTAzVDEy...

# With total count (expensive — sequential scan)
GET /v1/users?limit=50&include_total=true
```

---

## 4. Response Format

```json
{
    "data": [
        {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "external_id": "alice_123",
            "name": "Alice",
            "created_at": "2026-06-05T12:00:00Z"
        }
    ],
    "next_cursor": "c_eyIyMDI2LTA2LTA0VDEyOjAwOjAwWiIsICI1NTBlODQwMC1lMjliLTRkNC1hNzE2LTQ0NjY1NTQ0MDAwMCJ9",
    "has_more": true,
    "total": null
}
```

| Field | Type | Always | Description |
|---|---|---|---|
| `data` | array | Yes | Array of items for this page (may be empty) |
| `next_cursor` | string\|null | Yes | Pass as `?cursor=` for the next page. Null when `has_more` is false. |
| `has_more` | boolean | Yes | True if there are additional pages beyond this one |
| `total` | int\|null | When `include_total=true` | Total number of items across all pages. Null when `include_total` is false or omitted. |

---

## 5. Repository Implementation Pattern

### 5.1 Generic Paginate Helper

```python
# packages/core/utils/pagination.py

from typing import Optional, TypeVar, Generic
from uuid import UUID
from datetime import datetime
from sqlalchemy import select, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from packages.core.utils.cursor import encode_cursor, decode_cursor

ModelT = TypeVar("ModelT", bound=DeclarativeBase)


async def paginate(
    db: AsyncSession,
    model: type[ModelT],
    *filters,
    limit: int = 50,
    cursor: Optional[str] = None,
    sort_field: str = "created_at",
    sort_dir: str = "desc",
    id_field: str = "id",
    include_total: bool = False,
) -> tuple[list[ModelT], Optional[str], bool, Optional[int]]:
    """Generic cursor-based pagination for SQLAlchemy models.

    Args:
        db: Async SQLAlchemy session.
        model: SQLAlchemy model class.
        *filters: Additional filter expressions (e.g., Model.organization_id == ...).
        limit: Maximum items to return (default 50, max 200).
        cursor: Opaque cursor string for pagination. None for first page.
        sort_field: Column name for sorting (default "created_at").
        sort_dir: Sort direction ("desc" or "asc", default "desc").
        id_field: Column name for tiebreaker ID (default "id").
        include_total: If True, also return total count.

    Returns:
        Tuple of (items, next_cursor, has_more, total).

    SQL Pattern (for DESC):
        SELECT *
        FROM users
        WHERE organization_id = :org_id
          AND ( (created_at = :cursor_date AND id < :cursor_id)
               OR created_at < :cursor_date )
        ORDER BY created_at DESC, id DESC
        LIMIT :limit + 1
    """
    # Ensure limit is within bounds
    limit = min(max(limit, 1), 200)

    # Get column objects for the sort field and ID
    sort_column = getattr(model, sort_field, None)
    id_column = getattr(model, id_field, None)
    if sort_column is None:
        raise ValueError(f"Model {model.__name__} has no column '{sort_field}'")
    if id_column is None:
        raise ValueError(f"Model {model.__name__} has no column '{id_field}'")

    # Build base query
    query = select(model).where(*filters)

    # Apply cursor condition
    if cursor:
        cursor_val, cursor_id = decode_cursor(cursor)

        # Handle string (ISO datetime) → datetime comparison
        if isinstance(cursor_val, str):
            try:
                cursor_val = datetime.fromisoformat(cursor_val)
            except ValueError:
                pass  # Keep as string if not a datetime (e.g., int for sequence)

        if sort_dir == "desc":
            query = query.where(
                or_(
                    and_(
                        sort_column == cursor_val,
                        id_column < cursor_id,
                    ),
                    and_(
                        sort_column < cursor_val,
                    ),
                )
            )
        else:
            query = query.where(
                or_(
                    and_(
                        sort_column == cursor_val,
                        id_column > cursor_id,
                    ),
                    and_(
                        sort_column > cursor_val,
                    ),
                )
            )

    # Apply ordering
    if sort_dir == "desc":
        query = query.order_by(sort_column.desc(), id_column.desc())
    else:
        query = query.order_by(sort_column.asc(), id_column.asc())

    # Fetch N+1 to detect has_more
    query = query.limit(limit + 1)

    result = await db.execute(query)
    items = list(result.scalars().all())

    # Determine has_more and trim to limit
    has_more = len(items) > limit
    if has_more:
        items = items[:limit]

    # Generate next cursor from the last item
    next_cursor = None
    if has_more and items:
        last = items[-1]
        sort_value = getattr(last, sort_field)
        last_id = getattr(last, id_field)
        next_cursor = encode_cursor(sort_value, last_id)

    # Optional total count
    total = None
    if include_total:
        count_query = select(func.count()).select_from(model).where(*filters)
        count_result = await db.execute(count_query)
        total = count_result.scalar() or 0

    return items, next_cursor, has_more, total
```

### 5.2 Manual Repository Implementation

For repositories that need custom filtering beyond the generic helper:

```python
# repositories/user_repository.py

from typing import Optional
from uuid import UUID
from datetime import datetime
from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User
from packages.core.utils.cursor import encode_cursor, decode_cursor


class UserRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_paginated(
        self,
        organization_id: UUID,
        limit: int = 50,
        cursor: Optional[str] = None,
        search: Optional[str] = None,
    ) -> tuple[list[User], Optional[str], bool]:
        """List users with cursor-based pagination and optional search.

        Returns (items, next_cursor, has_more).

        The query uses a composite index on (organization_id, created_at DESC, id DESC)
        for O(log N) pagination performance.
        """
        # Base filter: tenant-scoped, not deleted
        filters = [
            User.organization_id == organization_id,
            User.is_deleted == False,
        ]

        # Optional search filter
        if search:
            filters.append(User.name.ilike(f"%{search}%"))

        query = select(User).where(*filters)

        # ── Cursor condition ──────────────────────────────────────────
        # Pattern: (created_at, id) < (:cursor_date, :cursor_id)
        # This is a composite key comparison that uses the index correctly.
        if cursor:
            cursor_date_str, cursor_id = decode_cursor(cursor)
            cursor_date = datetime.fromisoformat(cursor_date_str)
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

        # ── Ordering ──────────────────────────────────────────────────
        # Must match the composite index exactly
        query = query.order_by(
            User.created_at.desc(),
            User.id.desc(),
        ).limit(limit + 1)  # Fetch N+1 to detect has_more

        result = await self._db.execute(query)
        items = list(result.scalars().all())

        has_more = len(items) > limit
        if has_more:
            items = items[:limit]

        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = encode_cursor(last.created_at, last.id)

        return items, next_cursor, has_more
```

### 5.3 Ascending Order (Messages)

```python
# repositories/episode_repository.py

async def get_messages_paginated(
    self,
    session_id: UUID,
    limit: int = 50,
    cursor: Optional[str] = None,
) -> tuple[list[Episode], Optional[str], bool]:
    """Get messages within a session in chronological order.

    Messages use sequence_number ASC for deterministic ordering.
    """
    query = select(Episode).where(
        Episode.session_id == session_id,
    )

    if cursor:
        cursor_seq, cursor_id = decode_sequence_cursor(cursor)
        query = query.where(
            or_(
                and_(
                    Episode.sequence_number == cursor_seq,
                    Episode.id > cursor_id,  # ASC: larger UUIDs are "after"
                ),
                and_(
                    Episode.sequence_number > cursor_seq,
                ),
            )
        )

    query = query.order_by(
        Episode.sequence_number.asc(),
        Episode.id.asc(),
    ).limit(limit + 1)

    result = await self._db.execute(query)
    episodes = list(result.scalars().all())

    has_more = len(episodes) > limit
    if has_more:
        episodes = episodes[:limit]

    next_cursor = None
    if has_more and episodes:
        last = episodes[-1]
        next_cursor = encode_sequence_cursor(last.sequence_number, last.id)

    return episodes, next_cursor, has_more
```

---

## 6. `include_total` — Optional COUNT Query

The `total` field is **null by default** because `COUNT(*)` on large tables is expensive (sequential scan in PostgreSQL).

```python
# In repository:

async def count_total(self, organization_id: UUID, search: Optional[str] = None) -> int:
    """Expensive COUNT query.

    Performance characteristics:
      - Tables < 100k rows with appropriate indexes: < 10ms
      - Tables > 1M rows: can take 100ms+ (sequential scan)
      - Tables > 10M rows: avoid in hot path; use approximate count

    Returns 0 if no rows match.
    """
    filters = [
        User.organization_id == organization_id,
        User.is_deleted == False,
    ]
    if search:
        filters.append(User.name.ilike(f"%{search}%"))

    result = await self._db.execute(
        select(func.count()).where(*filters)
    )
    return result.scalar() or 0


# In service:

async def list_users(
    self,
    organization_id: UUID,
    limit: int = 50,
    cursor: Optional[str] = None,
    search: Optional[str] = None,
    include_total: bool = False,
) -> UserListResponse:
    items, next_cursor, has_more = await self._repo.list_paginated(
        organization_id=organization_id,
        limit=limit,
        cursor=cursor,
        search=search,
    )

    total = None
    if include_total:
        total = await self._repo.count_total(
            organization_id=organization_id,
            search=search,
        )

    return UserListResponse(
        data=[UserResponse.model_validate(u) for u in items],
        next_cursor=next_cursor,
        has_more=has_more,
        total=total,
    )
```

### 6.1 Approximate Count for Large Tables

For databases with > 1M rows, use PostgreSQL's pg_class estimate instead of exact COUNT:

```python
async def count_total_approximate(self, table_name: str) -> int:
    """Fast approximate count using PostgreSQL statistics.

    Uses pg_class.reltuples which is updated by ANALYZE/VACUUM.
    Accuracy: typically within 2-5% of actual count.
    Suitable for dashboard displays where exact count isn't critical.
    """
    result = await self._db.execute(
        text("""
            SELECT reltuples::bigint AS estimate
            FROM pg_class
            WHERE relname = :table_name
        """),
        {"table_name": table_name},
    )
    return result.scalar() or 0
```

---

## 7. Pydantic Response Schemas

### 7.1 Generic Paginated Response

```python
# schemas/common.py

from pydantic import BaseModel, Field
from typing import Generic, TypeVar, List, Optional

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response wrapper.

    Usage:
        ```python
        class UserListResponse(PaginatedResponse[UserResponse]):
            pass
        ```
    """
    data: List[T] = Field(
        ...,
        description="List of items for this page.",
    )
    next_cursor: Optional[str] = Field(
        None,
        description=(
            "Opaque cursor for the next page. "
            "Pass as ?cursor= in the next request to fetch the next page. "
            "Null when has_more is false (this is the last page)."
        ),
    )
    has_more: bool = Field(
        ...,
        description=(
            "True if there are more results beyond this page. "
            "Clients should check this field rather than checking "
            "if next_cursor is null."
        ),
    )
    total: Optional[int] = Field(
        None,
        description=(
            "Total number of items across all pages. "
            "Only present when ?include_total=true is specified. "
            "This field is expensive to compute for large datasets. "
            "The value is null when include_total is not requested."
        ),
    )
```

### 7.2 Concrete Response Types

```python
# schemas/users.py
class UserListResponse(PaginatedResponse[UserResponse]):
    """Paginated list of users."""
    pass


# schemas/sessions.py
class SessionListResponse(PaginatedResponse[SessionResponse]):
    pass


# schemas/facts.py
class FactListResponse(PaginatedResponse[FactResponse]):
    pass


# schemas/memory.py
class MessageListResponse(PaginatedResponse[MessageResponse]):
    pass
```

---

## 8. Router Implementation

Each list endpoint follows the same pattern. Here is the canonical example:

```python
# routers/users.py

from fastapi import APIRouter, Depends, Query
from typing import Optional

router = APIRouter(prefix="/users", tags=["users"])


@router.get(
    "",
    response_model=UserListResponse,
    summary="List users with pagination",
)
async def list_users(
    service: UserService = Depends(get_user_service),
    org: Organization = Depends(get_current_organization),
    # Pagination parameters
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Number of items per page (1-200, default 50).",
    ),
    cursor: Optional[str] = Query(
        default=None,
        description=(
            "Opaque cursor from the previous response's 'next_cursor' field. "
            "Omit for the first page."
        ),
    ),
    include_total: bool = Query(
        default=False,
        description=(
            "If true, include the total count of items across all pages. "
            "This triggers a COUNT query which is expensive on large tables. "
            "Default is false (total will be null)."
        ),
    ),
    # Domain-specific filters
    search: Optional[str] = Query(
        default=None,
        description="Search users by name or email (case-insensitive partial match).",
    ),
) -> UserListResponse:
    """List all users in the authenticated organization.

    Results are paginated using cursor-based pagination (keyset pagination).
    This is more efficient and consistent than traditional offset pagination,
    especially under write load.

    **Pagination flow:**
    1. First request: call without cursor
    2. Subsequent requests: pass the `next_cursor` from the previous response
    3. Stop when `has_more` is false

    **Example requests:**
    - `GET /v1/users?limit=50` — first page, 50 items
    - `GET /v1/users?limit=50&cursor=c_eyIyMDI2...` — second page
    - `GET /v1/users?limit=10&include_total=true` — with total count
    """
    return await service.list_users(
        organization_id=org.id,
        limit=limit,
        cursor=cursor,
        search=search,
        include_total=include_total,
    )
```

---

## 9. Client Usage Guide

### 9.1 Python (httpx)

```python
import httpx


async def paginate_all_users(base_url: str, api_key: str) -> list[dict]:
    """Iterate over all users using cursor pagination.

    Handles pagination transparently: fetches page after page
    until has_more is false.
    """
    all_users: list[dict] = []
    cursor: str | None = None

    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    ) as client:
        while True:
            params: dict = {"limit": 200}
            if cursor:
                params["cursor"] = cursor

            response = await client.get("/v1/users", params=params)
            response.raise_for_status()
            data = response.json()

            all_users.extend(data["data"])

            if not data["has_more"]:
                break
            cursor = data["next_cursor"]

    return all_users
```

### 9.2 cURL

```bash
# First page
curl -H "Authorization: Bearer mg_live_..." \
  "https://api.memgraph.dev/v1/users?limit=50"

# Subsequent pages
curl -H "Authorization: Bearer mg_live_..." \
  "https://api.memgraph.dev/v1/users?limit=50&cursor=c_eyIyMDI2..."
```

### 9.3 Handling Empty Results

```json
GET /v1/users?limit=50
{
    "data": [],
    "next_cursor": null,
    "has_more": false,
    "total": 0
}
```

---

## 10. SQL Index Requirements

For cursor pagination to achieve O(log N) performance, the composite index must match the query's WHERE + ORDER BY exactly.

### 10.1 Primary Listing Indexes

```sql
-- Users: tenant scope + created_at DESC + id DESC tiebreaker
CREATE INDEX ix_users_org_created_at
    ON users (organization_id, created_at DESC, id DESC)
    WHERE is_deleted = false;  -- Partial index: smaller, faster

-- Sessions: user scope + created_at DESC
CREATE INDEX ix_sessions_user_created_at
    ON sessions (user_id, created_at DESC, id DESC);

-- Facts: user scope + created_at DESC
CREATE INDEX ix_facts_user_created_at
    ON facts (user_id, created_at DESC, id DESC);

-- Episodes (messages): session scope + sequence_number ASC
CREATE INDEX ix_episodes_session_sequence
    ON episodes (session_id, sequence_number ASC, id ASC);
```

### 10.2 Why These Indexes Matter

Without the composite index, PostgreSQL falls back to:
1. **Sequential scan** + sort: O(N log N) per page
2. **Index scan on single column** + filter: O(N) per page

With the composite index:
- **Index-only scan**: O(log N) per page
- The `WHERE (created_at, id) < (:val, :id)` predicate is a **range condition on a composite B-tree index** — the database seeks directly to the cursor position and scans forward N rows.

### 10.3 Verifying Index Usage

```sql
EXPLAIN ANALYZE
SELECT id, created_at
FROM users
WHERE organization_id = 'some-uuid'
  AND is_deleted = false
  AND ( (created_at = '2026-06-01' AND id < 'some-uuid')
       OR created_at < '2026-06-01' )
ORDER BY created_at DESC, id DESC
LIMIT 51;

-- Expected: Index Only Scan using ix_users_org_created_at
-- Expected: No Sort node (index order matches ORDER BY)
```

---

## 11. Performance Characteristics

| Metric | Cursor Pagination | Offset Pagination |
|---|---|---|
| Page 1 latency | O(log N) | O(log N) |
| Page 1000 latency | O(log N) | O(N) — sequential scan past 1000 rows |
| Consistency under writes | ✅ Stable | ❌ Shifts (duplicate/miss items) |
| Total count cost | Optional, O(N) | Optional, O(N) |
| Random page access | ❌ Not supported | ✅ `?page=5` works |
| Real-time data | ✅ Yes | ❌ No |

### 11.1 When to Use Offset Pagination Instead

Cursor pagination does **not** support random page access (`?page=5`). If the admin dashboard needs "jump to page N" functionality, implement a separate offset-based endpoint:

```python
@router.get("/admin/users")
async def list_users_admin(
    page: int = Query(1, ge=1, le=1000),
    limit: int = Query(50, ge=1, le=100),
) -> UserListResponse:
    """Admin-only: offset-based pagination.

    ⚠️ Only use this for admin UIs, not API clients.
    Offset pagination is unstable under write load — items
    may appear on multiple pages or be skipped entirely
    when new records are inserted between requests.

    Guardrails:
    - Max page: 1000 (prevents deep-page scans)
    - Max limit: 100
    - Returns total count always (admin UI needs it for pagination controls)
    """
    offset = (page - 1) * limit
    query = select(User).where(...).order_by(
        User.created_at.desc()
    ).offset(offset).limit(limit)
    # ...
```

---

## 12. Testing Pagination

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_pagination_no_duplicates_or_missing(
    async_client: AsyncClient,
    auth_headers: dict,
) -> None:
    """Verify paginating through all items returns exactly
    the same set as fetching all items with a large limit.

    This is the definitive test for cursor pagination correctness.
    """
    # Create 55 test users
    created_ids: set[str] = set()
    for i in range(55):
        resp = await async_client.post(
            "/v1/users",
            json={"external_id": f"pagination_test_{i}"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        created_ids.add(resp.json()["id"])

    # Paginate through with limit=10
    paginated_ids: set[str] = set()
    cursor = None

    while True:
        params = {"limit": 10}
        if cursor:
            params["cursor"] = cursor

        response = await async_client.get(
            "/v1/users", params=params, headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()

        for item in data["data"]:
            # Only count our test users
            if item["external_id"].startswith("pagination_test_"):
                assert item["id"] not in paginated_ids, (
                    f"Duplicate detected: {item['id']}"
                )
                paginated_ids.add(item["id"])

        if not data["has_more"]:
            break
        cursor = data["next_cursor"]
        assert cursor is not None, "has_more=true but next_cursor is null"

    # Verify we got all 55 users with no duplicates and no missing
    assert paginated_ids == created_ids, (
        f"Missing items: {created_ids - paginated_ids}"
    )
    assert len(paginated_ids) == 55


@pytest.mark.asyncio
async def test_pagination_limit_max(
    async_client: AsyncClient, auth_headers: dict,
) -> None:
    """Verify max limit is enforced (422 for > 200)."""
    response = await async_client.get(
        "/v1/users?limit=999", headers=auth_headers,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_pagination_invalid_cursor(
    async_client: AsyncClient, auth_headers: dict,
) -> None:
    """Verify invalid cursor returns 400."""
    response = await async_client.get(
        "/v1/users?cursor=not-valid-base64", headers=auth_headers,
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_pagination_empty_result(
    async_client: AsyncClient, auth_headers: dict,
) -> None:
    """Verify empty result set returns empty data with has_more=false."""
    response = await async_client.get(
        "/v1/users?limit=50", headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["data"], list)
    assert data["has_more"] is False
    assert data["next_cursor"] is None


@pytest.mark.asyncio
async def test_pagination_include_total(
    async_client: AsyncClient, auth_headers: dict,
) -> None:
    """Verify ?include_total=true returns the total count."""
    response = await async_client.get(
        "/v1/users?limit=10&include_total=true", headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] is not None
    assert isinstance(data["total"], int)
    assert data["total"] >= 0


@pytest.mark.asyncio
async def test_pagination_include_total_default_null(
    async_client: AsyncClient, auth_headers: dict,
) -> None:
    """Verify total is null by default (no include_total param)."""
    response = await async_client.get(
        "/v1/users?limit=10", headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pagination_consistency_under_write(
    async_client: AsyncClient, auth_headers: dict,
) -> None:
    """Verify pagination is stable when new items are inserted.

    With cursor pagination, adding new items after pagination
    has started should NOT cause duplicates or missing items
    because the cursor points to a specific position in the
    order, not an offset count.
    """
    # Insert some initial users
    for i in range(5):
        await async_client.post(
            "/v1/users",
            json={"external_id": f"initial_{i}"},
            headers=auth_headers,
        )

    # Start paginating
    response = await async_client.get(
        "/v1/users?limit=3", headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    first_page_ids = {item["id"] for item in data["data"]}
    cursor = data["next_cursor"]

    # Insert new users mid-pagination
    for i in range(3):
        await async_client.post(
            "/v1/users",
            json={"external_id": f"mid_insert_{i}"},
            headers=auth_headers,
        )

    # Continue paginating — should NOT see items from first_page_ids again
    while cursor:
        response = await async_client.get(
            f"/v1/users?limit=3&cursor={cursor}", headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        for item in data["data"]:
            assert item["id"] not in first_page_ids, (
                f"Duplicate detected with cursor pagination! "
                f"Item {item['id']} seen again."
            )
        cursor = data["next_cursor"]
```

---

## 13. Full End-to-End Example

```
Client                                                  MemGraph API
  │                                                         │
  │  GET /v1/users?limit=3                                 │
  │ ──────────────────────────────────────────────────────► │
  │                                                         │
  │  SELECT id, external_id, created_at                    │
  │  FROM users                                             │
  │  WHERE organization_id = 'org_abc'                     │
  │    AND is_deleted = false                               │
  │  ORDER BY created_at DESC, id DESC                      │
  │  LIMIT 4                                                │
  │                                                         │
  │ ◄── 200 OK                                             │
  │  {                                                      │
  │    "data": [                                           │
  │      { "id": "aaa", "created_at": "2026-06-05T12:00" },│
  │      { "id": "bbb", "created_at": "2026-06-04T12:00" },│
  │      { "id": "ccc", "created_at": "2026-06-03T12:00" } │
  │    ],                                                   │
  │    "next_cursor": "c_eyIyMDI2LTA2LTAzVDEy...",         │
  │    "has_more": true,                                    │
  │    "total": null                                        │
  │  }                                                      │
  │                                                         │
  │  GET /v1/users?limit=3&cursor=c_eyIyMDI2LTA2LTAz...    │
  │ ──────────────────────────────────────────────────────► │
  │                                                         │
  │  SELECT ...                                             │
  │  WHERE organization_id = 'org_abc'                     │
  │    AND is_deleted = false                               │
  │    AND (                                                │
  │      (created_at = '2026-06-03T12:00' AND id < 'ccc')  │
  │      OR created_at < '2026-06-03T12:00'                │
  │    )                                                    │
  │  ORDER BY created_at DESC, id DESC                      │
  │  LIMIT 4                                                │
  │                                                         │
  │ ◄── 200 OK                                             │
  │  {                                                      │
  │    "data": [                                            │
  │      { "id": "ddd", "created_at": "2026-06-02T12:00" },│
  │      { "id": "eee", "created_at": "2026-06-01T12:00" } │
  │    ],                                                   │
  │    "next_cursor": null,                                 │
  │    "has_more": false,                                   │
  │    "total": null                                        │
  │  }                                                      │
```

---

## 14. Migration from Offset to Cursor Pagination

If an existing endpoint uses offset pagination and needs to switch:

1. **Add cursor parameters** alongside existing `page`/`offset` params (don't remove them immediately)
2. **Deprecate offset params** with a sunset header
3. **Migrate clients** to use cursor parameters
4. **Remove offset params** after the deprecation period

```python
# Migration pattern:

@router.get("/users")
async def list_users(
    # Old params (deprecated)
    page: Optional[int] = Query(None, ge=1, deprecated=True),
    # New params
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None),
) -> UserListResponse:
    if page is not None:
        # Log warning
        logger.warning("deprecated.offset_pagination", path="/v1/users")
        # Fall back to offset
        return await service.list_users_offset(limit=limit, offset=(page - 1) * limit)
    return await service.list_users(limit=limit, cursor=cursor)
```

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
