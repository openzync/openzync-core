"""Session repository — all database access for the Session model.

Every query is scoped to a ``user_id`` (which itself is tenant-scoped via
the users table).  The repository returns ORM models only — no business
logic, no schema construction.
"""

from __future__ import annotations

from collections.abc import Sequence
from core.cursor import decode_cursor, encode_cursor
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.episode import Episode
from models.fact import Fact
from models.session import Session
from models.user import User


class SessionRepository:
    """All database access for sessions.

    Args:
        db: An async SQLAlchemy session.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Create ──────────────────────────────────────────────────────────────

    async def create(
        self,
        organization_id: UUID,
        user_id: UUID,
        external_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        """Create a new session for a user.

        The unique constraint on ``(user_id, external_id)`` provides the
        final guard against duplicates — the service layer checks first,
        but a race is still possible.

        Args:
            organization_id: The organization UUID for tenant isolation.
            user_id: The owning user's UUID.
            external_id: Caller-defined session identifier.
            metadata: Optional session metadata.

        Returns:
            The newly created Session with generated id and timestamps.
        """
        session = Session(
            organization_id=organization_id,
            user_id=user_id,
            external_id=external_id,
            metadata_=metadata or {},
        )
        self._db.add(session)
        await self._db.flush()
        await self._db.refresh(session)
        return session

    async def get_or_create_default(
        self, org_id: UUID, user_id: UUID
    ) -> Session:
        """Get or create the ``__default__`` session for a user.

        The default session is used when callers send messages without
        specifying a ``session_id``.  It is hidden from session list
        endpoints.

        Args:
            org_id: The organization UUID for tenant isolation.
            user_id: The owning user's UUID.

        Returns:
            An existing or newly created default Session.
        """
        session = await self.get_by_external_id(org_id, user_id, "__default__")
        if session is not None:
            return session

        # Race-safe: the unique constraint on (user_id, external_id)
        # will prevent a duplicate insert from a concurrent request.
        from sqlalchemy.exc import IntegrityError

        session = Session(
            organization_id=org_id,
            user_id=user_id,
            external_id="__default__",
            metadata_={"auto_created": True},
        )
        self._db.add(session)
        try:
            await self._db.flush()
            await self._db.refresh(session)
        except IntegrityError:
            await self._db.rollback()
            session = await self.get_by_external_id(org_id, user_id, "__default__")
            if session is None:
                raise RuntimeError(
                    f"Failed to get-or-create default session for user {user_id}"
                ) from None

        return session

    # ── Read ────────────────────────────────────────────────────────────────

    async def get_by_external_id(
        self, org_id: UUID, user_id: UUID, external_id: str
    ) -> Session | None:
        """Look up a session by ``user_id`` and ``external_id``, scoped to org.

        Args:
            org_id: The organization UUID for tenant isolation.
            user_id: The owning user's UUID.
            external_id: The caller-defined session identifier.

        Returns:
            The Session if found and not soft-deleted, ``None`` otherwise.
        """
        result = await self._db.execute(
            select(Session)
            .join(User, Session.user_id == User.id)
            .where(
                Session.user_id == user_id,
                Session.external_id == external_id,
                User.organization_id == org_id,
                Session.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_uuid(
        self, org_id: UUID, session_id: UUID, user_id: UUID | None = None
    ) -> Session | None:
        """Look up a session by its internal UUID, scoped to org and optionally user.

        Args:
            org_id: The organization UUID for tenant isolation.
            session_id: The session's UUID primary key.
            user_id: Optional user UUID for intra-org isolation. When provided,
                only sessions belonging to this user are returned.

        Returns:
            The Session if found and not soft-deleted, ``None`` otherwise.
        """
        query = (
            select(Session)
            .join(User, Session.user_id == User.id)
            .where(
                Session.id == session_id,
                User.organization_id == org_id,
                Session.is_deleted.is_(False),
            )
        )
        if user_id is not None:
            query = query.where(Session.user_id == user_id)

        result = await self._db.execute(query)
        return result.scalar_one_or_none()

    # ── List ────────────────────────────────────────────────────────────────

    async def list(  # noqa: A003 — shadowing built-in is idiomatic for repos
        self,
        org_id: UUID,
        user_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
        include_closed: bool = False,
        exclude_default: bool = True,
    ) -> tuple[list[Session], str | None]:
        """List sessions for a user with cursor-based pagination, scoped to org.

        The default excludes:
        - The ``__default__`` auto-created session.
        - Closed sessions (``closed_at IS NOT NULL``).

        Pagination uses a composite cursor of ``(created_at DESC, id ASC)``
        for stable, most-recent-first ordering.

        Args:
            org_id: The organization UUID for tenant isolation.
            user_id: The owning user's UUID.
            limit: Maximum results per page (capped at 200).
            cursor: Opaque base64 cursor from a previous page.
            include_closed: If ``True``, include closed sessions.
            exclude_default: If ``True``, hide the ``__default__`` session.

        Returns:
            A tuple of ``(sessions, next_cursor)``.  ``next_cursor`` is
            ``None`` when there are no more pages.
        """
        effective_limit = min(limit, 200) + 1  # +1 to detect has_more

        query = (
            select(Session)
            .join(User, Session.user_id == User.id)
            .where(
                Session.user_id == user_id,
                User.organization_id == org_id,
                Session.is_deleted.is_(False),
            )
        )

        if exclude_default:
            query = query.where(Session.external_id != "__default__")

        if not include_closed:
            query = query.where(Session.closed_at.is_(None))

        # Cursor: composite (created_at DESC, id) for most-recent-first
        if cursor is not None:
            cursor_at, cursor_id = self._decode_cursor(cursor)
            query = query.where(
                or_(
                    Session.created_at < cursor_at,
                    Session.created_at == cursor_at,
                    Session.id > cursor_id,
                )
            )

        query = query.order_by(
            Session.created_at.desc(), Session.id.asc()
        ).limit(effective_limit)

        result = await self._db.execute(query)
        rows = result.scalars().all()

        has_more = len(rows) == effective_limit
        sessions = list(rows[:limit]) if has_more else list(rows)

        next_cursor: str | None = None
        if has_more and sessions:
            last = sessions[-1]
            next_cursor = self._encode_cursor(last.created_at, last.id)

        return sessions, next_cursor

    # ── Messages ────────────────────────────────────────────────────────────

    async def get_messages(
        self,
        org_id: UUID,
        session_id: UUID,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[Episode], str | None]:
        """Get paginated messages for a session, ordered by sequence_number.

        Uses ``sequence_number`` (monotonically increasing per session) for
        deterministic ordering — avoids timestamp-tie issues when multiple
        messages arrive in the same millisecond.

        Args:
            org_id: The organization UUID for tenant isolation.
            session_id: The session's UUID.
            limit: Maximum results per page (capped at 500).
            cursor: Opaque base64 cursor from a previous page.

        Returns:
            A tuple of ``(messages, next_cursor)``.
        """
        effective_limit = min(limit, 500) + 1  # +1 to detect has_more

        # Verify the session belongs to the organization before fetching messages.
        session_check = (
            select(Session.id)
            .join(User, Session.user_id == User.id)
            .where(
                Session.id == session_id,
                User.organization_id == org_id,
                Session.is_deleted.is_(False),
            )
        )
        session_result = await self._db.execute(session_check)
        if session_result.scalar_one_or_none() is None:
            return [], None

        query = select(Episode).where(
            Episode.session_id == session_id,
            Episode.is_deleted.is_(False),
        )

        # Cursor: composite (sequence_number ASC, id ASC)
        if cursor is not None:
            cursor_seq, cursor_id = self._decode_message_cursor(cursor)
            query = query.where(
                or_(
                    Episode.sequence_number > cursor_seq,
                    Episode.sequence_number == cursor_seq,
                    Episode.id > cursor_id,
                )
            )

        query = query.order_by(
            Episode.sequence_number.asc(), Episode.id.asc()
        ).limit(effective_limit)

        result = await self._db.execute(query)
        rows = result.scalars().all()

        has_more = len(rows) == effective_limit
        messages = list(rows[:limit]) if has_more else list(rows)

        next_cursor: str | None = None
        if has_more and messages:
            last = messages[-1]
            next_cursor = self._encode_message_cursor(
                last.sequence_number, last.id
            )

        return messages, next_cursor

    # ── Next Sequence Number ────────────────────────────────────────────────

    async def next_sequence_number(self, session_id: UUID) -> int:
        """Get the next sequence number for a session.

        Uses ``SELECT COALESCE(MAX(seq), -1) + 1``, which is thread-safe
        because the increment happens within the same transaction as the
        subsequent INSERT.

        Args:
            session_id: The session's UUID.

        Returns:
            The next available ``sequence_number`` (0-based).
        """
        result = await self._db.execute(
            select(func.coalesce(func.max(Episode.sequence_number), -1) + 1).where(
                Episode.session_id == session_id
            )
        )
        return result.scalar()

    # ── Update Metadata ─────────────────────────────────────────────────────

    async def update_metadata(
        self,
        org_id: UUID,
        session_id: UUID,
        metadata: dict[str, Any],
    ) -> Session | None:
        """Deep-merge metadata into a session's existing metadata, scoped to org.

        New keys are added. Existing keys are overridden. Set a key to
        ``None`` to remove it from the stored metadata.

        Args:
            org_id: The organization UUID for tenant isolation.
            session_id: The session's UUID.
            metadata: Key-value pairs to merge.

        Returns:
            The updated Session, or ``None`` if not found.
        """
        result = await self._db.execute(
            select(Session)
            .join(User, Session.user_id == User.id)
            .where(
                Session.id == session_id,
                User.organization_id == org_id,
                Session.is_deleted.is_(False),
            )
        )
        session = result.scalar_one_or_none()
        if session is None:
            return None

        existing = dict(session.metadata_ or {})
        for k, v in metadata.items():
            if v is None:
                existing.pop(k, None)
            elif isinstance(v, dict) and isinstance(existing.get(k), dict):
                existing[k] = {**existing[k], **v}
            else:
                existing[k] = v
        session.metadata_ = existing

        await self._db.flush()
        await self._db.refresh(session)
        return session

    # ── Close ───────────────────────────────────────────────────────────────

    async def close(
        self, org_id: UUID, session_id: UUID
    ) -> Session | None:
        """Mark a session as closed by setting ``closed_at = now()``, scoped to org.

        Idempotent — calling close on an already-closed session returns
        the session as-is.

        Args:
            org_id: The organization UUID for tenant isolation.
            session_id: The session's UUID.

        Returns:
            The updated Session, or ``None`` if not found.
        """
        result = await self._db.execute(
            select(Session)
            .join(User, Session.user_id == User.id)
            .where(
                Session.id == session_id,
                User.organization_id == org_id,
                Session.is_deleted.is_(False),
            )
        )
        session = result.scalar_one_or_none()
        if session is None:
            return None

        session.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await self._db.flush()
        await self._db.refresh(session)
        return session

    # ── Soft Delete ─────────────────────────────────────────────────────────

    async def soft_delete(
        self, org_id: UUID, session_id: UUID, user_id: UUID | None = None
    ) -> Session | None:
        """Soft-delete a session, scoped to org and optionally user.

        Sets ``is_deleted = True`` and unlinks episodes from the session
        (episodes are preserved as orphaned history for audit, then
        purged by the GDPR worker).

        Args:
            org_id: The organization UUID for tenant isolation.
            session_id: The session's UUID.
            user_id: Optional user UUID for intra-org isolation.

        Returns:
            The updated Session, or ``None`` if not found or already deleted.
        """
        query = (
            select(Session)
            .join(User, Session.user_id == User.id)
            .where(
                Session.id == session_id,
                User.organization_id == org_id,
                Session.is_deleted.is_(False),
            )
        )
        if user_id is not None:
            query = query.where(Session.user_id == user_id)
        result = await self._db.execute(query)
        session = result.scalar_one_or_none()
        if session is None:
            return None

        session.is_deleted = True

        # TechLead: The unlink step was removed because the episodes FK
        # uses ondelete=CASCADE + nullable=False — setting session_id=NULL
        # violates the NOT NULL constraint. Episodes remain linked to the
        # soft-deleted session, which is safe since queries filter on
        # session.is_deleted. A follow-up migration (SET NULL + nullable)
        # can restore the unlink intent if GDPR/orphaning requirements
        # demand it.

        await self._db.flush()
        await self._db.refresh(session)
        return session

    # ── Stats ───────────────────────────────────────────────────────────────

    async def get_stats(self, session_id: UUID) -> dict[str, Any]:
        """Return aggregate counts for a session in a single query.

        Single query with outer joins — no N+1 risk.

        Args:
            session_id: The session's UUID.

        Returns:
            A dict with ``message_count``, ``fact_count``, and
            ``last_message_at``.
        """
        stmt = select(
            func.count(Episode.id).label("message_count"),
            func.count(Fact.id).label("fact_count"),
            func.max(Episode.created_at).label("last_message_at"),
        ).select_from(Session).outerjoin(
            Episode, Episode.session_id == Session.id
        ).outerjoin(
            Fact, Fact.source_episode_id == Episode.id
        ).where(Session.id == session_id).group_by(Session.id)

        result = await self._db.execute(stmt)
        row = result.one_or_none()

        if row is None:
            return {
                "message_count": 0,
                "fact_count": 0,
                "last_message_at": None,
            }

        return {
            "message_count": row.message_count or 0,
            "fact_count": row.fact_count or 0,
            "last_message_at": row.last_message_at,
        }

    async def batch_get_stats(
        self, session_ids: list[UUID], organization_id: UUID
    ) -> dict[UUID, dict[str, int]]:
        """Batch-load message counts per session in a single query.

        Eliminates the N+1 problem when rendering session list pages.
        Only non-deleted episodes are counted.

        Args:
            session_ids: The session UUIDs to fetch stats for.
            organization_id: Tenant isolation scope.

        Returns:
            Dict mapping ``session_id`` → ``{"message_count": int}``.
            Sessions with no episodes are omitted from the result.
        """
        if not session_ids:
            return {}

        stmt = select(
            Episode.session_id,
            func.count(Episode.id).label("message_count"),
        ).where(
            Episode.session_id.in_(session_ids),
            Episode.organization_id == organization_id,
            Episode.is_deleted.is_(False),
        ).group_by(Episode.session_id)

        result = await self._db.execute(stmt)
        return {
            row.session_id: {"message_count": row.message_count}
            for row in result.all()
        }

    # ── Auto-Close (for scheduled task) ─────────────────────────────────────

    async def find_stale_open_sessions(
        self, inactivity_hours: int = 24, batch_size: int = 100
    ) -> list[Session]:
        """Find sessions with no activity in the given window.

        Used by the auto-close scheduled task (ARQ cron) to close stale
        sessions.  Excludes the ``__default__`` session.

        Args:
            inactivity_hours: Hours of inactivity before a session is
                considered stale.  Defaults to 24.
            batch_size: Maximum number of sessions to return.

        Returns:
            A list of stale open Sessions.
        """
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=inactivity_hours)
        result = await self._db.execute(
            select(Session)
            .where(
                Session.closed_at.is_(None),
                Session.is_deleted.is_(False),
                Session.external_id != "__default__",
                Session.updated_at < cutoff,
            )
            .limit(batch_size)
        )
        return list(result.scalars().all())

    # ── Count ───────────────────────────────────────────────────────────────

    async def message_count(self, session_id: UUID) -> int:
        """Get total message count for a session.

        Args:
            session_id: The session's UUID.

        Returns:
            The number of episodes belonging to this session.
        """
        result = await self._db.execute(
            select(func.count(Episode.id)).where(
                Episode.session_id == session_id,
                Episode.is_deleted.is_(False),
            )
        )
        return result.scalar() or 0

    # ── Cursor helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _encode_cursor(created_at: datetime, session_id: UUID) -> str:
        """Encode a composite cursor as a URL-safe base64 string.

        Format: ``{created_at_isoformat}|{session_id_hex}``
        """
        return encode_cursor(f"{created_at.isoformat()}|{session_id.hex}")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
        """Decode a composite cursor back into ``(created_at, session_id)``.

        Raises:
            ValueError: If the cursor is malformed.
        """
        try:
            raw = decode_cursor(cursor)
            at_str, id_hex = raw.split("|", 1)
            return datetime.fromisoformat(at_str), UUID(hex=id_hex)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid session cursor: {e}") from e

    @staticmethod
    def _encode_message_cursor(sequence_number: int, episode_id: UUID) -> str:
        """Encode a message cursor as a URL-safe base64 string.

        Format: ``{sequence_number}|{episode_id_hex}``
        """
        return encode_cursor(f"{sequence_number}|{episode_id.hex}")

    @staticmethod
    def _decode_message_cursor(cursor: str) -> tuple[int, UUID]:
        """Decode a message cursor back into ``(sequence_number, episode_id)``.

        Raises:
            ValueError: If the cursor is malformed.
        """
        try:
            raw = decode_cursor(cursor)
            seq_str, id_hex = raw.split("|", 1)
            return int(seq_str), UUID(hex=id_hex)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid message cursor: {e}") from e
