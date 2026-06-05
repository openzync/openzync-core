# Session CRUD Implementation Guide

> **Phase:** Phase 1 — Core Memory (Week 3-4)
> **Priority:** P0
> **Requirements:** SES-01, SES-02, SES-03, SES-04, SES-05, ING-05
> **Handoff from:** Architect (ADR-003: User & Session Data Model)

---

## 1. Overview

Sessions group conversation messages (episodes) into logical units — typically a single conversation thread between a user and an agent. Every message optionally references a `session_id`. Messages without a session go to a default session per user.

This document covers session CRUD, auto-close on inactivity, default session logic, paginated message retrieval, and aggregated session stats.

---

## 2. Pydantic Schemas

Located at `services/api/schemas/sessions.py`.

### 2.1 CreateSessionRequest

```python
from pydantic import BaseModel, Field
from typing import Optional


class CreateSessionRequest(BaseModel):
    """Schema for creating a new conversation session.

    The caller provides an `external_id` (their own session identifier).
    Sessions are created within a user context.
    """
    external_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Caller-chosen unique identifier for this session, "
                    "scoped to the user.",
        examples=["session_abc123", "conv_001"],
    )
    metadata: Optional[dict] = Field(
        None,
        description="Arbitrary caller-defined metadata (JSON object). "
                    "Max depth: 5 levels. Max keys: 50.",
    )
```

### 2.2 SessionResponse

```python
from datetime import datetime
from uuid import UUID
from typing import Optional


class SessionResponse(BaseModel):
    """Public session representation."""
    id: UUID = Field(..., description="Internal MemGraph session UUID.")
    user_id: UUID = Field(..., description="Internal user UUID.")
    external_id: str = Field(..., description="Caller-chosen session identifier.")
    metadata: dict = Field(
        default_factory=dict,
        description="Arbitrary caller-defined metadata.",
    )
    is_active: bool = Field(
        ...,
        description="True if the session is open (no auto-close yet).",
    )
    created_at: datetime = Field(..., description="When the session was created.")
    closed_at: Optional[datetime] = Field(
        None,
        description="When the session was auto-closed due to inactivity.",
    )
    updated_at: datetime = Field(..., description="When the session was last updated.")

    model_config = ConfigDict(from_attributes=True)
```

### 2.3 SessionResponseWithStats

```python
class SessionStats(BaseModel):
    """Aggregated statistics for a session."""
    message_count: int = Field(..., description="Number of episodes in this session.")
    fact_count: int = Field(..., description="Number of facts extracted from this session.")
    classification_count: int = Field(
        ...,
        description="Number of dialog classifications in this session.",
    )


class SessionResponseWithStats(SessionResponse):
    """Session response with aggregate statistics."""
    stats: SessionStats = Field(..., description="Aggregated session statistics.")
```

### 2.4 SessionListResponse

```python
from typing import List, Optional


class SessionListResponse(BaseModel):
    """Cursor-paginated list response for sessions."""
    data: List[SessionResponseWithStats] = Field(..., description="List of sessions.")
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
        description="Total number of sessions (only if ?include_total=true).",
    )
```

### 2.5 MessageResponse (for message retrieval)

```python
class MessageResponse(BaseModel):
    """A single message within a session."""
    id: UUID = Field(..., description="Internal episode UUID.")
    role: str = Field(..., description="One of: user, assistant, system, tool.")
    content: str = Field(..., description="Message content.")
    metadata: dict = Field(default_factory=dict, description="Message-level metadata.")
    sequence_number: int = Field(
        ...,
        description="Monotonically increasing sequence number within the session. "
                    "Use this for ordering, not created_at.",
    )
    created_at: datetime = Field(..., description="When the message was ingested.")


class MessageListResponse(BaseModel):
    """Cursor-paginated list response for messages within a session."""
    data: List[MessageResponse] = Field(..., description="List of messages.")
    next_cursor: Optional[str] = Field(
        None,
        description="Cursor for next page (uses sequence_number, not created_at).",
    )
    has_more: bool = Field(
        ...,
        description="True if there are more messages beyond this page.",
    )
```

---

## 3. Data Model (SQLAlchemy)

Located at `packages/core/models/session.py`.

```python
import uuid
from datetime import datetime
from sqlalchemy import String, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func


class Session(TimestampMixin, Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "external_id",
            name="uq_sessions_user_external_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_id: Mapped[str] = mapped_column(
        String(255), nullable=False,
    )
    metadata: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
        comment="False after auto-close or explicit close.",
    )
    last_message_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="Timestamp of the last message in this session. "
                "Used for inactivity-based auto-close.",
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="When the session was closed (auto or manual).",
    )
```

**Episodes model** (referenced sections only — full model in memory ingestion doc):

```python
class Episode(TimestampMixin, Base):
    __tablename__ = "episodes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(nullable=False)
    metadata: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    sequence_number: Mapped[int] = mapped_column(
        nullable=False,
        comment="Monotonically increasing per-session. Used for ordering.",
    )
    # ...
```

### Indexes

```sql
CREATE INDEX ix_sessions_user_active
    ON sessions (user_id, is_active, created_at DESC);

CREATE INDEX ix_sessions_last_message_at
    ON sessions (user_id, last_message_at DESC);

CREATE INDEX ix_episodes_session_sequence
    ON episodes (session_id, sequence_number ASC);
```

---

## 4. Repository Layer

Located at `packages/core/repositories/session_repository.py`.

```python
from uuid import UUID
from typing import Optional, Tuple, List
from datetime import datetime, timedelta
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession


class SessionRepository:
    """All DB access for sessions. No business logic."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Create ──────────────────────────────────────────────────────────

    async def create(
        self,
        user_id: UUID,
        external_id: str,
        metadata: Optional[dict] = None,
    ) -> Session:
        session = Session(
            user_id=user_id,
            external_id=external_id,
            metadata=metadata or {},
            is_active=True,
        )
        self._db.add(session)
        await self._db.flush()
        await self._db.refresh(session)
        return session

    # ── Read ────────────────────────────────────────────────────────────

    async def get_by_id(
        self, session_id: UUID, user_id: UUID,
    ) -> Optional[Session]:
        result = await self._db.execute(
            select(Session).where(
                Session.id == session_id,
                Session.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_external_id(
        self, external_id: str, user_id: UUID,
    ) -> Optional[Session]:
        result = await self._db.execute(
            select(Session).where(
                Session.external_id == external_id,
                Session.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    # ── Update ──────────────────────────────────────────────────────────

    async def update_metadata(
        self,
        session: Session,
        metadata: dict,
    ) -> Session:
        """Merge/patch metadata: provided keys overwrite, others preserved."""
        existing = dict(session.metadata or {})
        existing.update(metadata)
        session.metadata = existing
        session.updated_at = func.now()
        await self._db.flush()
        await self._db.refresh(session)
        return session

    async def touch_last_message_at(self, session: Session) -> None:
        """Update the last_message_at timestamp on message ingestion."""
        session.last_message_at = func.now()
        await self._db.flush()

    async def close_session(self, session: Session) -> None:
        """Mark session as inactive."""
        session.is_active = False
        session.closed_at = func.now()
        await self._db.flush()

    # ── Delete ──────────────────────────────────────────────────────────

    async def hard_delete(self, session: Session) -> None:
        """Permanently delete session. Episodes cascade."""
        await self._db.delete(session)
        await self._db.flush()

    # ── Listing ─────────────────────────────────────────────────────────

    async def list_paginated(
        self,
        user_id: UUID,
        limit: int = 50,
        cursor: Optional[str] = None,
        only_active: Optional[bool] = None,
    ) -> Tuple[List[Session], Optional[str], bool]:
        query = select(Session).where(
            Session.user_id == user_id,
        )

        if only_active is True:
            query = query.where(Session.is_active == True)  # noqa: E712
        elif only_active is False:
            query = query.where(Session.is_active == False)  # noqa: E712

        # Cursor-based: ordered by created_at DESC, id DESC
        if cursor:
            cursor_date, cursor_id = decode_cursor(cursor)
            query = query.where(
                or_(
                    and_(
                        Session.created_at == cursor_date,
                        Session.id < cursor_id,
                    ),
                    and_(
                        Session.created_at < cursor_date,
                    ),
                )
            )

        query = query.order_by(
            Session.created_at.desc(),
            Session.id.desc(),
        ).limit(limit + 1)

        result = await self._db.execute(query)
        sessions = list(result.scalars().all())

        has_more = len(sessions) > limit
        if has_more:
            sessions = sessions[:limit]

        next_cursor = None
        if has_more and sessions:
            last = sessions[-1]
            next_cursor = encode_cursor(last.created_at, last.id)

        return sessions, next_cursor, has_more

    # ── Messages ────────────────────────────────────────────────────────

    async def get_messages_paginated(
        self,
        session_id: UUID,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Episode], Optional[str], bool]:
        """Retrieve messages ordered by sequence_number ASC.

        Cursor format: base64("{sequence_number}:{id}").
        Uses sequence_number to avoid timestamp-tie issues.
        """
        from models.episode import Episode

        query = select(Episode).where(
            Episode.session_id == session_id,
        )

        if cursor:
            cursor_seq, cursor_id = decode_sequence_cursor(cursor)
            query = query.where(
                or_(
                    and_(
                        Episode.sequence_number == cursor_seq,
                        Episode.id > cursor_id,  # id ASC tiebreaker
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
            next_cursor = encode_sequence_cursor(
                last.sequence_number, last.id,
            )

        return episodes, next_cursor, has_more

    # ── Stats ───────────────────────────────────────────────────────────

    async def get_session_stats(
        self, session_id: UUID,
    ) -> dict:
        """Aggregate stats for a session — single query, no N+1."""
        from models.episode import Episode
        from models.fact import Fact
        from models.dialog_classification import DialogClassification

        message_count = (
            select(func.count(Episode.id))
            .where(Episode.session_id == session_id)
            .correlate(Session)
            .scalar_subquery()
        )
        fact_count = (
            select(func.count(Fact.id))
            .where(Fact.source_episode_id == Episode.id,
                   Episode.session_id == session_id)
            .correlate(Session)
            .scalar_subquery()
        )
        classification_count = (
            select(func.count(DialogClassification.id))
            .where(DialogClassification.episode_id == Episode.id,
                   Episode.session_id == session_id)
            .correlate(Session)
            .scalar_subquery()
        )

        result = await self._db.execute(
            select(message_count, fact_count, classification_count)
        )
        row = result.one()
        return {
            "message_count": row[0] or 0,
            "fact_count": row[1] or 0,
            "classification_count": row[2] or 0,
        }

    # ── Auto-close detection ───────────────────────────────────────────

    async def find_stale_active_sessions(
        self, inactivity_threshold: timedelta,
    ) -> List[Session]:
        """Find active sessions that haven't received messages
        beyond the inactivity threshold."""
        cutoff = datetime.utcnow() - inactivity_threshold
        result = await self._db.execute(
            select(Session).where(
                Session.is_active == True,  # noqa: E712
                Session.last_message_at < cutoff,
            )
        )
        return list(result.scalars().all())

    async def close_session(self, session: Session) -> None:
        session.is_active = False
        session.closed_at = func.now()
        await self._db.flush()

    # ── Cursor encoding/decoding for sequence_number ───────────────────

    async def get_default_session(
        self, user_id: UUID,
    ) -> Session:
        """Get or create the default session for a user.

        The default session has external_id = "__default__".
        Messages without session_id are routed here.
        """
        external_id = "__default__"
        existing = await self.get_by_external_id(external_id, user_id)
        if existing:
            return existing
        return await self.create(
            user_id=user_id,
            external_id=external_id,
            metadata={"type": "default"},
        )
```

### Cursor Encoding for Sequence-Based Pagination

```python
# In packages/core/utils/cursor.py

def encode_sequence_cursor(sequence_number: int, id: UUID) -> str:
    """Encode cursor using sequence_number (for message ordering)."""
    payload = json.dumps([sequence_number, str(id)])
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_sequence_cursor(cursor: str) -> tuple[int, UUID]:
    """Decode sequence-based cursor."""
    try:
        padding = 4 - (len(cursor) % 4)
        if padding != 4:
            cursor += "=" * padding
        payload = base64.urlsafe_b64decode(cursor.encode()).decode()
        seq_str, id_str = json.loads(payload)
        return int(seq_str), UUID(id_str)
    except (ValueError, json.JSONDecodeError, IndexError) as e:
        raise ValidationError(f"Invalid sequence cursor format: {e}")
```

---

## 5. Service Layer

Located at `services/api/services/session_service.py`.

```python
from uuid import UUID
from typing import Optional, Tuple, List
from datetime import datetime, timedelta


class SessionService:
    """All business logic for session operations."""

    def __init__(
        self,
        repo: SessionRepository,
        user_repo: UserRepository,
        cache: RedisCache,
        settings: Settings,
    ) -> None:
        self._repo = repo
        self._user_repo = user_repo
        self._cache = cache
        self._settings = settings
        self._inactivity_threshold = timedelta(
            hours=settings.SESSION_INACTIVITY_TIMEOUT_HOURS
        )

    # ── Create ──────────────────────────────────────────────────────────

    async def create_session(
        self,
        user_id: UUID,
        organization_id: UUID,
        request: CreateSessionRequest,
    ) -> SessionResponseWithStats:
        """Create a new session for a user.

        Raises:
            NotFoundError: If the user doesn't exist.
            ValidationError: If session external_id already exists for this user.
        """
        # Verify user exists
        user = await self._user_repo.get_by_id(user_id, organization_id)
        if not user:
            raise NotFoundError(f"User '{user_id}' not found.")

        # Check duplicate
        existing = await self._repo.get_by_external_id(
            request.external_id, user_id,
        )
        if existing:
            raise ValidationError(
                f"Session with external_id '{request.external_id}' "
                f"already exists for user '{user_id}'."
            )

        session = await self._repo.create(
            user_id=user_id,
            external_id=request.external_id,
            metadata=request.metadata,
        )
        return await self._build_response_with_stats(session)

    # ── Get ─────────────────────────────────────────────────────────────

    async def get_session(
        self,
        session_id: UUID,
        user_id: UUID,
        organization_id: UUID,
    ) -> SessionResponseWithStats:
        _verify_user(user_id, organization_id, self._user_repo)

        session = await self._repo.get_by_id(session_id, user_id)
        if not session:
            raise NotFoundError(
                f"Session '{session_id}' not found for user '{user_id}'."
            )
        return await self._build_response_with_stats(session)

    # ── List ────────────────────────────────────────────────────────────

    async def list_sessions(
        self,
        user_id: UUID,
        organization_id: UUID,
        limit: int = 50,
        cursor: Optional[str] = None,
        only_active: Optional[bool] = None,
        include_total: bool = False,
    ) -> SessionListResponse:
        _verify_user(user_id, organization_id, self._user_repo)

        sessions, next_cursor, has_more = await self._repo.list_paginated(
            user_id=user_id,
            limit=min(limit, 200),
            cursor=cursor,
            only_active=only_active,
        )

        session_responses = [
            await self._build_response_with_stats(s) for s in sessions
        ]

        total = None
        if include_total:
            total = await self._repo.count_total(user_id)

        return SessionListResponse(
            data=session_responses,
            next_cursor=next_cursor,
            has_more=has_more,
            total=total,
        )

    # ── Get Messages ────────────────────────────────────────────────────

    async def get_messages(
        self,
        session_id: UUID,
        user_id: UUID,
        organization_id: UUID,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> MessageListResponse:
        _verify_user(user_id, organization_id, self._user_repo)

        session = await self._repo.get_by_id(session_id, user_id)
        if not session:
            raise NotFoundError(
                f"Session '{session_id}' not found for user '{user_id}'."
            )

        episodes, next_cursor, has_more = await self._repo.get_messages_paginated(
            session_id=session_id,
            limit=min(limit, 200),
            cursor=cursor,
        )

        messages = [
            MessageResponse(
                id=ep.id,
                role=ep.role,
                content=ep.content,
                metadata=ep.metadata,
                sequence_number=ep.sequence_number,
                created_at=ep.created_at,
            )
            for ep in episodes
        ]

        return MessageListResponse(
            data=messages,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    # ── Delete ──────────────────────────────────────────────────────────

    async def delete_session(
        self,
        session_id: UUID,
        user_id: UUID,
        organization_id: UUID,
    ) -> None:
        _verify_user(user_id, organization_id, self._user_repo)

        session = await self._repo.get_by_id(session_id, user_id)
        if not session:
            raise NotFoundError(
                f"Session '{session_id}' not found for user '{user_id}'."
            )

        await self._repo.hard_delete(session)
        await self._cache.delete_pattern(f"session:{session_id}:*")

    # ── Auto-close ─────────────────────────────────────────────────────

    async def auto_close_stale_sessions(self) -> int:
        """Close all sessions that have exceeded the inactivity threshold.

        Called by a scheduled ARQ task (runs every 15 minutes).

        Returns:
            Number of sessions closed.
        """
        stale = await self._repo.find_stale_active_sessions(
            self._inactivity_threshold,
        )
        for session in stale:
            await self._repo.close_session(session)
        return len(stale)

    async def touch_session(
        self, session_id: UUID, user_id: UUID,
    ) -> None:
        """Update last_message_at after message ingestion.

        Prevents auto-close while the session is active.
        """
        session = await self._repo.get_by_id(session_id, user_id)
        if session and session.is_active:
            await self._repo.touch_last_message_at(session)

    # ── Default session ─────────────────────────────────────────────────

    async def get_or_create_default_session(
        self, user_id: UUID,
    ) -> Session:
        """Get or create the default session for messages without session_id."""
        return await self._repo.get_default_session(user_id)

    # ── Private helpers ─────────────────────────────────────────────────

    async def _build_response_with_stats(
        self, session: Session,
    ) -> SessionResponseWithStats:
        stats = await self._repo.get_session_stats(session.id)
        return SessionResponseWithStats(
            id=session.id,
            user_id=session.user_id,
            external_id=session.external_id,
            metadata=session.metadata,
            is_active=session.is_active,
            created_at=session.created_at,
            closed_at=session.closed_at,
            updated_at=session.updated_at,
            stats=SessionStats(**stats),
        )


async def _verify_user(
    user_id: UUID, organization_id: UUID, user_repo: UserRepository,
) -> None:
    """Ensure the user exists and belongs to the organization."""
    user = await user_repo.get_by_id(user_id, organization_id)
    if not user:
        raise NotFoundError(f"User '{user_id}' not found.")
```

---

## 6. Router Layer

Located at `services/api/routers/sessions.py`.

```python
from fastapi import APIRouter, Depends, Query, Path, status
from uuid import UUID
from typing import Optional

router = APIRouter(
    prefix="/v1/users/{user_id}/sessions",
    tags=["sessions"],
)


@router.post("", response_model=SessionResponseWithStats, status_code=201)
async def create_session(
    request: CreateSessionRequest,
    user_id: UUID = Path(...),
    service: SessionService = Depends(get_session_service),
    org: Organization = Depends(get_current_organization),
) -> SessionResponseWithStats:
    """Create a new conversation session for a user."""
    return await service.create_session(
        user_id=user_id,
        organization_id=org.id,
        request=request,
    )


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    user_id: UUID = Path(...),
    service: SessionService = Depends(get_session_service),
    org: Organization = Depends(get_current_organization),
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None),
    active_only: Optional[bool] = Query(None),
    include_total: bool = Query(False),
) -> SessionListResponse:
    """List all sessions for a user, ordered by created_at DESC."""
    return await service.list_sessions(
        user_id=user_id,
        organization_id=org.id,
        limit=limit,
        cursor=cursor,
        only_active=active_only,
        include_total=include_total,
    )


@router.get("/{session_id}", response_model=SessionResponseWithStats)
async def get_session(
    user_id: UUID = Path(...),
    session_id: UUID = Path(...),
    service: SessionService = Depends(get_session_service),
    org: Organization = Depends(get_current_organization),
) -> SessionResponseWithStats:
    """Get session detail including message count, fact count, and classifications."""
    return await service.get_session(
        session_id=session_id,
        user_id=user_id,
        organization_id=org.id,
    )


@router.get("/{session_id}/messages", response_model=MessageListResponse)
async def get_messages(
    user_id: UUID = Path(...),
    session_id: UUID = Path(...),
    service: SessionService = Depends(get_session_service),
    org: Organization = Depends(get_current_organization),
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None),
) -> MessageListResponse:
    """Get paginated messages for a session.

    Messages are ordered by sequence_number (ascending), not created_at,
    to avoid ambiguous ordering when timestamps are identical.
    """
    return await service.get_messages(
        session_id=session_id,
        user_id=user_id,
        organization_id=org.id,
        limit=limit,
        cursor=cursor,
    )


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    user_id: UUID = Path(...),
    session_id: UUID = Path(...),
    service: SessionService = Depends(get_session_service),
    org: Organization = Depends(get_current_organization),
) -> None:
    """Delete a session and all its data.

    This permanently removes all episodes (messages), classifications,
    and extractions associated with this session. Graph nodes are
    unlinked but not deleted (the agent's knowledge graph is preserved).
    """
    await service.delete_session(
        session_id=session_id,
        user_id=user_id,
        organization_id=org.id,
    )
```

---

## 7. Auto-Close Mechanism

### 7.1 Configuration

```python
# core/config.py
class Settings(BaseSettings):
    SESSION_INACTIVITY_TIMEOUT_HOURS: int = Field(
        default=24,
        description="Sessions auto-close after this many hours "
                    "without a new message.",
    )
    SESSION_AUTO_CLOSE_INTERVAL_MINUTES: int = Field(
        default=15,
        description="How often the auto-close scheduled task runs.",
    )
```

### 7.2 Scheduled Worker Task

```python
# services/worker/tasks/session_cleanup.py
from arq.connections import RedisSettings


async def auto_close_sessions(ctx):
    """Scheduled task: close sessions inactive for > 24h.

    Runs every 15 minutes (configured in ARQ cron schedule).
    """
    service: SessionService = ctx["session_service"]
    closed_count = await service.auto_close_stale_sessions()
    if closed_count > 0:
        logger.info(
            "session.auto_close",
            extra={"closed_count": closed_count},
        )


# In ARQ WorkerSettings:
class WorkerSettings:
    cron_jobs = [
        {
            "cron": "*/15 * * * *",  # Every 15 minutes
            "func": auto_close_sessions,
            "timeout": 300,
        },
    ]
```

### 7.3 Auto-Close Sequence

```
Message ingested ──► session.last_message_at updated to now()
                         │
                    [15 min check]
                         │
                    session.last_message_at < (now - 24h)?
                         │
                    YES ──► session.is_active = False
                           session.closed_at = now()
```

---

## 8. Default Session

Messages ingested without a `session_id` automatically go to the default session.

### 8.1 Behavior

```python
# Inside the memory ingestion service:

async def ingest_messages(
    self,
    user_id: UUID,
    organization_id: UUID,
    messages: list[dict],
    session_id: Optional[str] = None,
):
    """Ingest messages, routing to the appropriate session."""
    # Resolve session
    if session_id:
        session = await self._session_repo.get_by_external_id(
            session_id, user_id,
        )
        if not session:
            raise NotFoundError(
                f"Session '{session_id}' not found for user '{user_id}'."
            )
    else:
        # Route to default session
        session = await self._session_service.get_or_create_default_session(
            user_id,
        )

    # Update session freshness
    await self._session_service.touch_session(session.id, user_id)

    # Store episodes...
```

### 8.2 Default Session External ID

The default session uses `external_id = "__default__"`. It is created lazily on first message ingestion without a session_id.

---

## 9. Metadata Merge/Patch Semantics

Session metadata supports partial updates. When `PATCH /v1/users/{user_id}/sessions/{session_id}` is called with a `metadata` object, the top-level keys are merged:

```python
# Existing metadata
{
    "source": "web",
    "tags": ["support"],
    "language": "en",
}

# Patch with:
{
    "tags": ["support", "billing"],  # overwrite "tags"
    "priority": "high",              # add new key
}

# Result:
{
    "source": "web",
    "tags": ["support", "billing"],  # replaced
    "language": "en",                # preserved
    "priority": "high",              # added
}
```

---

## 10. Error Scenarios

| Scenario | HTTP | Code | Detail |
|---|---|---|---|
| Session not found | 404 | `RESOURCE_NOT_FOUND` | "Session 'uuid' not found for user 'user_id'" |
| Duplicate session external_id | 409 | `RESOURCE_CONFLICT` | "Session with external_id 'X' already exists" |
| User not found (in path) | 404 | `RESOURCE_NOT_FOUND` | "User 'uuid' not found" |
| Invalid sequence cursor | 400 | `INVALID_CURSOR` | "Invalid sequence cursor format" |
| Session already closed | 409 | `SESSION_CLOSED` | "Session 'uuid' is already closed" |

---

## 11. Testing

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_session(
    async_client: AsyncClient,
    auth_headers: dict,
    existing_user: User,
) -> None:
    response = await async_client.post(
        f"/v1/users/{existing_user.id}/sessions",
        json={"external_id": "session_001", "metadata": {"topic": "support"}},
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.json()
    assert data["external_id"] == "session_001"
    assert data["is_active"] is True
    assert data["stats"]["message_count"] == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_messages_ordered_by_sequence(
    async_client: AsyncClient,
    auth_headers: dict,
    existing_session_with_messages: Session,
) -> None:
    session_id = existing_session_with_messages.id
    user_id = existing_session_with_messages.user_id

    response = await async_client.get(
        f"/v1/users/{user_id}/sessions/{session_id}/messages?limit=100",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    messages = data["data"]
    # Verify ordered by sequence_number ascending
    seq_nums = [m["sequence_number"] for m in messages]
    assert seq_nums == sorted(seq_nums)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_auto_close_after_inactivity(
    session_service: SessionService,
    existing_session: Session,
) -> None:
    # Manually set last_message_at to 25 hours ago
    session = existing_session
    session.last_message_at = datetime.utcnow() - timedelta(hours=25)

    closed = await session_service.auto_close_stale_sessions()
    assert closed == 1

    # Verify session is now inactive
    refreshed = await session_service.get_session(
        session.id, session.user_id, org_id,
    )
    assert refreshed.is_active is False
    assert refreshed.closed_at is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_default_session_auto_creation(
    async_client: AsyncClient,
    auth_headers: dict,
    existing_user: User,
) -> None:
    # Ingest a message without session_id
    response = await async_client.post(
        f"/v1/users/{existing_user.id}/memory",
        json={"messages": [{"role": "user", "content": "Hello"}]},
        headers=auth_headers,
    )
    assert response.status_code == 202

    # Default session should now exist
    sessions_resp = await async_client.get(
        f"/v1/users/{existing_user.id}/sessions",
        headers=auth_headers,
    )
    sessions = sessions_resp.json()["data"]
    default_sessions = [s for s in sessions if s["external_id"] == "__default__"]
    assert len(default_sessions) == 1
```

---

## 12. Migration

```python
"""add_sessions_and_episodes

Revision ID: 002
Revises: 001_users
Create Date: 2026-06-05

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "002"
down_revision = "001_users"


def upgrade() -> None:
    # -- Sessions --
    op.create_table(
        "sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("metadata", JSONB, server_default="{}", nullable=False),
        sa.Column("is_active", sa.Boolean, server_default="true", nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "external_id", name="uq_sessions_user_external_id"),
    )
    op.create_index("ix_sessions_user_active", "sessions", ["user_id", "is_active", sa.text("created_at DESC")])
    op.create_index("ix_sessions_last_message_at", "sessions", ["user_id", sa.text("last_message_at DESC")])

    # -- Episodes (messages) --
    op.create_table(
        "episodes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("metadata", JSONB, server_default="{}", nullable=False),
        sa.Column("sequence_number", sa.Integer, nullable=False),
        sa.Column("embedding", sa.Vector(1536), nullable=True),
        sa.Column("graphiti_node_id", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_episodes_session_sequence", "episodes", ["session_id", sa.text("sequence_number ASC")])
    op.create_index("ix_episodes_user_id", "episodes", ["user_id"])
    op.create_index("ix_episodes_embedding", "episodes",
                    [sa.text("embedding vector_cosine_ops")],
                    postgresql_using="ivfflat", postgresql_with={"lists": 100})
    op.create_index("ix_episodes_content_gin", "episodes",
                    [sa.text("to_tsvector('english', content)")],
                    postgresql_using="gin")


def downgrade() -> None:
    op.drop_table("episodes")
    op.drop_table("sessions")
```

---

## 13. Sequence Diagram

```
Caller                    FastAPI              SessionService         SessionRepository      PostgreSQL
  │                         │                       │                      │                    │
  │ POST /v1/users/{uid}    │                       │                      │                    │
  │      /sessions          │                       │                      │                    │
  │ {external_id, metadata} │                       │                      │                    │
  │ ──────────────────────► │                       │                      │                    │
  │                         │ verify user           │                      │                    │
  │                         │ ────────────────────► │                      │                    │
  │                         │                       │ get_by_external_id() │                    │
  │                         │                       │ ───────────────────► │                    │
  │                         │                       │                      │ ── SELECT ────────► │
  │                         │                       │                      │ ◄─── None ──────── │
  │                         │                       │ ◄── None ────────── │                    │
  │                         │                       │                      │                    │
  │                         │                       │ create()             │                    │
  │                         │                       │ ───────────────────► │                    │
  │                         │                       │                      │ ── INSERT ────────► │
  │                         │                       │                      │ ◄── new Session ── │
  │                         │                       │ ◄── Session ─────── │                    │
  │                         │                       │                      │                    │
  │                         │                       │ get_session_stats()  │                    │
  │                         │                       │ ───────────────────► │                    │
  │                         │                       │                      │ ── SELECT COUNT ──► │
  │                         │                       │                      │ ◄─── stats ─────── │
  │                         │                       │ ◄── stats ───────── │                    │
  │                         │                       │                      │                    │
  │ ◄── 201 Created ─────── │ ◄─ ResponseWithStats  │                      │                    │
  │    {id, is_active,      │                       │                      │                    │
  │     stats: {...}}       │                       │                      │                    │
```

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
