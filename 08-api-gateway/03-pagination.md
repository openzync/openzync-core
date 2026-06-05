# Cursor-Based Pagination Implementation Guide

> **Phase:** Phase 1 — Core Memory (Week 3-4)
> **Priority:** P0
> **Requirements:** USR-05, SES-02, SES-04 (all list endpoints), SRS §8.5
> **Handoff from:** Architect (ADR-005: Pagination Strategy)

---

## 1. Overview

All MemGraph list endpoints use **cursor-based pagination** (also known as keyset pagination) rather than traditional offset/limit pagination.

**Why cursor-based?**
- **O(1) per page**: No `OFFSET` scan — performance is constant regardless of page number.
- **Consistent under write load**: Adding new items doesn't shift page boundaries (no duplicates or missing items).
- **Safe for real-time data**: New records appended to the end don't affect cursor-based reads.

**Default sort order**: `created_at DESC` for most resources (newest first). Message retrieval within sessions uses `sequence_number ASC`.

---

## 2. Cursor Format

### 2.1 Standard Cursor (created_at-based)

```python
# Format: base64url(json([ISO_timestamp, UUID]))
# Example:
#   Input:  created_at="2026-01-01T00:00:00Z", id="550e8400-e29b-41d4-a716-446655440000"
#   Output: c_eyIyMDI2LTAxLTAxVDAwOjAwOjAwWiI6ICI1NTBlODQwMC1lMjliLTRkZ..."  (52 chars)
```

Implementation in `packages/core/utils/cursor.py`:

```python
import base64
import json
from datetime import datetime
from uuid import UUID


def encode_cursor(sort_value: datetime | int, id: UUID) -> str:
    """Encode a cursor from a sort field value and resource ID.

    Args:
        sort_value: The value of the sort field (datetime for created_at,
                    int for sequence_number).
        id: The resource UUID (used as tiebreaker).

    Returns:
        URL-safe base64-encoded cursor string.
    """
    if isinstance(sort_value, datetime):
        sort_value = sort_value.isoformat()
    payload = json.dumps([sort_value, str(id)])
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple:
    """Decode a cursor back to (sort_value, UUID).

    Args:
        cursor: The opaque cursor string from a previous response.

    Returns:
        Tuple of (sort_value, id). sort_value may be str (datetime.isoformat)
        or int, depending on the sort field.

    Raises:
        ValidationError: If the cursor format is invalid.
    """
    try:
        # Restore padding
        padding = 4 - (len(cursor) % 4)
        if padding != 4:
            cursor += "=" * padding
        payload = base64.urlsafe_b64decode(cursor.encode()).decode()
        sort_value, id_str = json.loads(payload)
        return sort_value, UUID(id_str)
    except (ValueError, json.JSONDecodeError, IndexError) as e:
        raise ValidationError(f"Invalid cursor format: {e}")
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
    sort_value, id = decode_cursor(cursor)
    return int(sort_value), id
```

---

## 3. Query Parameters

All list endpoints accept the same pagination parameters:

| Parameter | Type | Default | Max | Description |
|---|---|---|---|---|
| `limit` | integer | 50 | 200 | Number of items per page |
| `cursor` | string | null | — | Opaque cursor from previous page response. Omit for first page. |
| `include_total` | boolean | false | — | If true, include total count (expensive on large datasets) |

### 3.1 Request Examples

```bash
# First page (default page size)
GET /v1/users?limit=50

# Second page
GET /v1/users?limit=50&cursor=c_eyIyMDI2...

# Small page
GET /v1/users?limit=10&cursor=c_eyIyMDI2...

# With total count (expensive)
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
            "created_at": "2026-06-05T12:00:00Z",
            "stats": {
                "message_count": 42,
                "fact_count": 7
            }
        }
    ],
    "next_cursor": "c_eyIyMDI2LTA2LTA0VDEyOjAwOjAwWiI6ICI1NTBlODQwMC1lMjliLTRkNC1hNzE2LTQ0NjY1NTQ0MDAwMCJ9",
    "has_more": true,
    "total": null
}
```

| Field | Type | Always Present | Description |
|---|---|---|---|
| `data` | array | Yes | Array of items (may be empty) |
| `next_cursor` | string | When `has_more` is true | Pass as `?cursor=` for the next page |
| `has_more` | boolean | Yes | True if there are additional pages |
| `total` | integer or null | When `include_total=true` | Total number of items (expensive) |

---

## 5. Repository Implementation Pattern

Every repository that supports list endpoints implements the same pattern:

```python
async def list_paginated(
    self,
    organization_id: UUID,   # Tenant scope (or user_id for sessions)
    limit: int = 50,
    cursor: Optional[str] = None,
    # Additional filter parameters...
) -> Tuple[List[Model], Optional[str], bool]:
    """Return (items, next_cursor, has_more).

    Implementation pattern:
      1. Build base query with tenant scope and filters
      2. Apply cursor condition (WHERE (sort_field, id) < (:val, :id))
      3. Order by sort_field DESC, id DESC
      4. Fetch limit + 1 to determine has_more
      5. Return items (sliced to limit), cursor from last item, has_more flag
    """
    query = select(Model).where(
        Model.organization_id == organization_id,
        # Additional filters...
    )

    # ── Cursor ──────────────────────────────────────────────────
    if cursor:
        cursor_date, cursor_id = decode_cursor(cursor)
        # Handle both str (ISO datetime) and datetime objects
        if isinstance(cursor_date, str):
            cursor_date = datetime.fromisoformat(cursor_date)
        query = query.where(
            # Composite key comparison: (created_at, id) < (:date, :id)
            # This handles the tiebreaker correctly
            or_(
                and_(
                    Model.created_at == cursor_date,
                    Model.id < cursor_id,  # id DESC: earlier UUIDs are "after"
                ),
                and_(
                    Model.created_at < cursor_date,
                ),
            )
        )

    # ── Ordering + Limit ────────────────────────────────────────
    query = query.order_by(
        Model.created_at.desc(),
        Model.id.desc(),
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

### 5.1 Ascending Order (Messages)

For messages ordered by `sequence_number ASC`:

```python
async def get_messages_paginated(
    self,
    session_id: UUID,
    limit: int = 50,
    cursor: Optional[str] = None,
) -> Tuple[List[Episode], Optional[str], bool]:
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

    # Ascending order for chronological message retrieval
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

## 6. `include_total` — Expensive COUNT Query

The `total` field is **null by default**. Only include it when the client explicitly requests it, because `COUNT(*)` queries on large tables are expensive (sequential scan on PostgreSQL).

```python
# In repository:
async def count_total(self, organization_id: UUID) -> int:
    """Expensive COUNT query.

    Performance notes:
      - On tables < 100k rows with appropriate indexes, this is fast (< 10ms)
      - On tables > 1M rows, COUNT can take 100ms+
      - For very large tables, consider an approximate count using
        PostgreSQL's pg_class estimate:
          SELECT reltuples::bigint FROM pg_class WHERE relname = 'users'
    """
    result = await self._db.execute(
        select(func.count()).where(
            Model.organization_id == organization_id,
            # ... same filters as the query
        )
    )
    return result.scalar() or 0


# In service:
if include_total:
    total = await self._repo.count_total(organization_id)
    # Otherwise, total stays None
```

### 6.1 Approximate Count for Large Tables

For databases with > 1M rows, use PostgreSQL's estimated row count instead of exact COUNT:

```python
async def count_total_approximate(
    self, table_name: str, organization_id: UUID,
) -> int:
    """Fast approximate count using PostgreSQL statistics.

    Returns an estimate, not an exact count. Good enough for pagination UIs.
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

All paginated endpoints share a common response structure:

```python
from pydantic import BaseModel, Field
from typing import Generic, TypeVar, List, Optional

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response wrapper.

    Usage:
        class UserListResponse(PaginatedResponse[UserResponseWithStats]):
            pass
    """
    data: List[T] = Field(..., description="List of items for this page.")
    next_cursor: Optional[str] = Field(
        None,
        description="Opaque cursor for the next page. "
                    "Pass as ?cursor= in the next request. "
                    "Null/absent means this is the last page.",
    )
    has_more: bool = Field(
        ...,
        description="True if there are more results beyond this page.",
    )
    total: Optional[int] = Field(
        None,
        description="Total number of items across all pages. "
                    "Only present when ?include_total=true is specified. "
                    "This field is expensive to compute for large datasets.",
    )
```

### 7.1 Concrete Response Types

```python
# users.py
class UserListResponse(PaginatedResponse[UserResponseWithStats]):
    pass

# sessions.py
class SessionListResponse(PaginatedResponse[SessionResponseWithStats]):
    pass

# messages.py
class MessageListResponse(PaginatedResponse[MessageResponse]):
    pass
```

---

## 8. Router Implementation

Each list endpoint follows the same pattern:

```python
@router.get("", response_model=UserListResponse)
async def list_users(
    service: UserService = Depends(get_user_service),
    org: Organization = Depends(get_current_organization),
    limit: int = Query(50, ge=1, le=200, description="Items per page (max 200)."),
    cursor: Optional[str] = Query(
        None,
        description="Opaque cursor from previous response. "
                    "Omit for first page.",
    ),
    include_total: bool = Query(
        False,
        description="Include total count. Expensive on large datasets.",
    ),
    # Additional filters...
) -> UserListResponse:
    """List users with cursor-based pagination."""
    return await service.list_users(
        organization_id=org.id,
        limit=limit,
        cursor=cursor,
        include_total=include_total,
    )
```

---

## 9. Client Usage Guide

### 9.1 Python (using `httpx`)

```python
import httpx


async def paginate_all_users(base_url: str, api_key: str) -> list[dict]:
    """Iterate over all users using cursor pagination."""
    all_users = []
    cursor = None
    client = httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
    )

    while True:
        params = {"limit": 200}
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

### 9.2 Handling Empty Results

```python
# First page with no results
GET /v1/users?limit=50
# Response:
{
    "data": [],
    "next_cursor": null,
    "has_more": false,
    "total": 0
}
```

---

## 10. Performance Characteristics

| Metric | Cursor Pagination | Offset Pagination |
|---|---|---|
| Page 1 latency | O(log N) | O(log N) |
| Page N latency | O(log N) | O(N) — sequential scan past N rows |
| Consistency under writes | ✅ Stable | ❌ Shifts (duplicate/miss items) |
| Total count cost | Optional, O(N) | Optional, O(N) |
| Random page access | ❌ Not supported | ✅ `?page=5` works |
| Works with real-time data | ✅ Yes | ❌ No |

### 10.1 When to Use Offset Pagination Instead

Cursor pagination does **not** support random page access (`?page=5`). If the admin dashboard needs "jump to page N" functionality, implement a separate offset-based endpoint with these guardrails:

```python
@router.get("/admin/users")
async def list_users_admin(
    page: int = Query(1, ge=1, le=1000),
    limit: int = Query(50, ge=1, le=100),
) -> UserListResponse:
    """Admin-only: offset-based pagination for dashboard.

    ⚠️ Only use this for admin UIs, not API clients.
    Offset pagination is unstable under write load.
    """
    offset = (page - 1) * limit
    query = select(User).where(...).order_by(User.created_at.desc()).offset(offset).limit(limit)
    # ...
```

---

## 11. Testing Pagination

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_pagination_no_duplicates_or_missing(
    async_client: AsyncClient,
    auth_headers: dict,
) -> None:
    """Verify that paginating through all items returns exactly
    the same set as fetching all items with a large limit.

    This is the definitive test for cursor pagination correctness.
    """
    # Create 55 users
    created_ids = set()
    for i in range(55):
        resp = await async_client.post(
            "/v1/users",
            json={"external_id": f"pagination_test_{i}"},
            headers=auth_headers,
        )
        created_ids.add(resp.json()["id"])

    # Paginate through with limit=10
    paginated_ids = set()
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
                assert item["id"] not in paginated_ids, "Duplicate detected!"
                paginated_ids.add(item["id"])

        if not data["has_more"]:
            break
        cursor = data["next_cursor"]

    # Verify we got all 55 users with no duplicates
    assert paginated_ids == created_ids, "Missing items in pagination!"
    assert len(paginated_ids) == 55


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pagination_limit_max(
    async_client: AsyncClient, auth_headers: dict,
) -> None:
    """Verify max limit is enforced."""
    response = await async_client.get(
        "/v1/users?limit=999", headers=auth_headers,
    )
    assert response.status_code == 422  # FastAPI validation catches this


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pagination_invalid_cursor(
    async_client: AsyncClient, auth_headers: dict,
) -> None:
    """Verify invalid cursor returns 400."""
    response = await async_client.get(
        "/v1/users?cursor=not-valid-base64", headers=auth_headers,
    )
    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pagination_empty_result(
    async_client: AsyncClient, auth_headers: dict,
) -> None:
    """Verify empty result set returns empty data array."""
    response = await async_client.get(
        "/v1/users?limit=50", headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["data"], list)
    assert data["has_more"] is False
    assert data["next_cursor"] is None


@pytest.mark.asyncio
@pytest.mark.integration
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


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pagination_include_total_default_null(
    async_client: AsyncClient, auth_headers: dict,
) -> None:
    """Verify total is null by default."""
    response = await async_client.get(
        "/v1/users?limit=10", headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] is None
```

---

## 12. Full End-to-End Example

```
Client                                                  MemGraph API
  │                                                         │
  │  GET /v1/users?limit=3                                  │
  │ ───────────────────────────────────────────────────────► │
  │                                                         │
  │  SELECT id, external_id, created_at                     │
  │  FROM users                                             │
  │  WHERE organization_id = 'org_abc'                      │
  │    AND is_deleted = false                               │
  │  ORDER BY created_at DESC, id DESC                      │
  │  LIMIT 4                                                │
  │                                                         │
  │ ◄── 200 OK                                             │
  │  {                                                      │
  │    "data": [                                            │
  │      { "id": "aaa", "created_at": "2026-06-05T12:00" }, │
  │      { "id": "bbb", "created_at": "2026-06-04T12:00" },  │
  │      { "id": "ccc", "created_at": "2026-06-03T12:00" }  │
  │    ],                                                   │
  │    "next_cursor": "c_eyIyMDI2LTA2LTAzVDEy..."           │
  │    "has_more": true,                                    │
  │    "total": null                                        │
  │  }                                                      │
  │                                                         │
  │  GET /v1/users?limit=3&cursor=c_eyIyMDI2LTA2LTAz...    │
  │ ───────────────────────────────────────────────────────► │
  │                                                         │
  │  SELECT ...                                             │
  │  WHERE organization_id = 'org_abc'                      │
  │    AND is_deleted = false                               │
  │    AND (                                                │
  │      (created_at = '2026-06-03T12:00' AND id < 'ccc')   │
  │      OR created_at < '2026-06-03T12:00'                 │
  │    )                                                    │
  │  ORDER BY created_at DESC, id DESC                      │
  │  LIMIT 4                                                │
  │                                                         │
  │ ◄── 200 OK                                             │
  │  {                                                      │
  │    "data": [                                            │
  │      { "id": "ddd", "created_at": "2026-06-02T12:00" }, │
  │      { "id": "eee", "created_at": "2026-06-01T12:00" }  │
  │    ],                                                   │
  │    "next_cursor": null,                                 │
  │    "has_more": false,                                   │
  │    "total": null                                        │
  │  }                                                      │
```

---

## 13. SQL Index Requirements

For cursor pagination to be O(log N), the composite index must match the query's WHERE + ORDER BY:

```sql
-- Primary listing index (used by cursor pagination):
-- Organization scope + created_at DESC + id DESC tiebreaker
CREATE INDEX ix_users_org_created_at
    ON users (organization_id, created_at DESC, id DESC);

-- For messages within a session:
CREATE INDEX ix_episodes_session_sequence
    ON episodes (session_id, sequence_number ASC, id ASC);
```

Without these composite indexes, PostgreSQL falls back to a sequential scan + sort, which is O(N log N) per page.

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
