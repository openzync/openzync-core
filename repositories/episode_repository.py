"""Episode repository — all database access for the Episode model.

Episodes represent individual message turns within a conversation session.
Every query filters by ``is_deleted = False`` (soft-delete convention).
Cursor pagination uses ``sequence_number`` for deterministic ordering.

Key patterns:
- ``batch_create`` uses raw ``INSERT ... RETURNING`` SQL because the ORM
  model does not yet map ``user_id`` as a column (the column exists
  in the schema via migration 0001, but the model mixin is pending).
- All list queries use LIMIT + 1 to detect ``has_more`` without a COUNT.
- Cursor encoding uses ``{sequence_number}|{episode_id_hex}``, base64.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import orjson
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.cursor import decode_versioned_cursor, encode_versioned_cursor
from core.exceptions import CursorExpiredError
from models.episode import Episode

# ╠ This file contains NO business logic — only query construction.
# ╠ If you find yourself writing an ``if`` statement that makes a
# ╠ decision based on domain rules, it belongs in the service layer.


class EpisodeRepository:
    """All database access for episodes (messages).

    Every method accepts ``user_id`` to enforce tenant isolation.
    No business logic — pure query construction and execution.

    Args:
        db: An async SQLAlchemy session.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Batch Create ─────────────────────────────────────────────────────────

    async def batch_create(
        self,

        project_id: UUID,
        session_id: UUID,
        user_id: UUID,
        messages: list[dict[str, Any]],
        start_seq: int = 0,
    ) -> list[Episode]:
        """Insert multiple episodes in a single round-trip with RETURNING.

        Uses raw SQL to include ``user_id`` and ``project_id``
        (the schema requires them for RLS and project isolation).

        Sequence numbers are server-assigned as ``start_seq + i``
        (``MemoryService`` computes ``MAX+1`` under a session row lock);
        per-message ``sequence_number`` values are never read — the old
        ``msg.get("sequence_number", i)`` fallback restarted at 0 for
        every batch and interleaved old and new rows under
        ``ORDER BY seq, id``.

        Args:

            project_id: Project scope (included for project isolation).
            session_id: The parent session's UUID.
            user_id: The creating user's UUID.
            messages: List of message dicts, each containing ``role``,
                ``content``, ``metadata``, and optionally ``created_at``.
            start_seq: First sequence number for this batch (server-assigned
                ``MAX+1`` from the caller).

        Returns:
            A list of ``Episode`` ORM instances with generated fields
            populated (id, sequence_number, timestamps, etc.).

        Raises:
            sqlalchemy.exc.IntegrityError: On ``(session_id,
                sequence_number)`` collision — callers retry the request.
        """
        if not messages:
            return []

        values: list[str] = []
        params: dict[str, object] = {}
        now = datetime.now(UTC)

        for i, msg in enumerate(messages):
            episode_id = uuid4()
            seq = start_seq + i

            params[f"id_{i}"] = episode_id
            params[f"user_id_{i}"] = user_id
            params[f"project_id_{i}"] = project_id
            params[f"session_id_{i}"] = session_id
            params[f"role_{i}"] = msg["role"]
            params[f"content_{i}"] = msg["content"]
            params[f"metadata_{i}"] = orjson.dumps(msg.get("metadata", {})).decode("utf-8")
            params[f"created_at_{i}"] = msg.get("created_at") or now
            params[f"seq_{i}"] = seq

            placeholders = (
                f"(:id_{i}, :user_id_{i}, :project_id_{i}, :session_id_{i}, "
                f":role_{i}, :content_{i}, :metadata_{i}, "
                f":created_at_{i}, :seq_{i})"
            )
            values.append(placeholders)

        stmt = text(
            f"""
            INSERT INTO episodes (
                id, user_id, project_id, session_id,
                role, content, metadata, created_at, sequence_number
            )
            VALUES {', '.join(values)}
            RETURNING
                id, user_id, project_id, session_id,
                role, content, metadata, embedding, token_count,
                sequence_number, enrichment_status, is_deleted,
                created_at, updated_at
            """
        )

        result = await self._db.execute(stmt, params)
        rows = result.fetchall()

        # Convert raw rows to ORM models
        episodes: list[Episode] = []
        for row in rows:
            mapping = row._mapping  # type: ignore[attr-defined]
            episode = Episode(
                id=mapping["id"],

                session_id=mapping["session_id"],
                user_id=mapping["user_id"],
                role=mapping["role"],
                content=mapping["content"],
                metadata_=mapping["metadata"] if mapping["metadata"] is not None else {},
                embedding=mapping["embedding"],
                token_count=mapping["token_count"],
                sequence_number=mapping["sequence_number"],
                enrichment_status=mapping["enrichment_status"],
                is_deleted=mapping["is_deleted"],
            )
            # Manually set timestamps since we bypassed the ORM
            episode.created_at = mapping["created_at"]
            episode.updated_at = mapping["updated_at"]
            episodes.append(episode)

        return episodes

    # ── Read — Paginated by Session ──────────────────────────────────────────

    async def get_by_session_id(
        self,
        session_id: UUID,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[Episode], str | None]:
        """Get paginated episodes for a session, ordered by sequence_number.

        Args:
            session_id: The session's UUID.
            limit: Maximum results per page (capped at 500).
            cursor: Opaque base64 cursor from a previous page.

        Returns:
            A tuple of ``(episodes, next_cursor)``. ``next_cursor`` is
            ``None`` when there are no more pages.
        """
        effective_limit = min(limit, 500) + 1  # +1 to detect has_more

        query = select(Episode).where(
            Episode.session_id == session_id,
            Episode.is_deleted.is_(False),
        )

        if cursor is not None:
            cursor_seq, cursor_id = self._decode_cursor(cursor)
            query = query.where(
                or_(
                    and_(
                        Episode.sequence_number == cursor_seq,
                        Episode.id > cursor_id,
                    ),
                    Episode.sequence_number > cursor_seq,
                )
            )

        query = query.order_by(
            Episode.sequence_number.asc(), Episode.id.asc()
        ).limit(effective_limit)

        result = await self._db.execute(query)
        rows: list[Episode] = list(result.scalars().all())

        has_more = len(rows) == effective_limit
        episodes = rows[:limit] if has_more else rows

        next_cursor: str | None = None
        if has_more and episodes:
            last = episodes[-1]
            next_cursor = self._encode_cursor(last.sequence_number, last.id)

        return episodes, next_cursor

    # ── Read — Paginated by Project ─────────────────────────────────────────

    # ── Next Sequence Number ─────────────────────────────────────────────────

    async def get_next_sequence(self, session_id: UUID) -> int:
        """Get the next available sequence number for a session.

        Canonical ``MAX(sequence_number) + 1`` helper (over live rows).

        NOT race-safe on its own: callers must hold the parent Session
        row lock (``SessionRepository.get_by_id_for_update``) across this call and the
        subsequent INSERT.  The partial unique index
        ``uq_episodes_session_sequence`` is the final guard — a lost race
        surfaces as ``IntegrityError``, which callers convert to a
        retryable ``ConflictError``.

        Args:
            session_id: The session's UUID.

        Returns:
            The next ``sequence_number`` (0-based).
        """
        result = await self._db.execute(
            select(func.coalesce(func.max(Episode.sequence_number), -1) + 1).where(
                Episode.session_id == session_id,
                Episode.is_deleted.is_(False),
            )
        )
        return result.scalar() or 0

    # ── Batch Content Fetch ─────────────────────────────────────────────────

    async def get_content_batch(
        self, episode_ids: list[UUID], user_id: UUID | None = None
    ) -> dict[UUID, tuple[str, str]]:
        """Fetch ``(content, role)`` for a batch of episode IDs.

        Args:
            episode_ids: List of episode UUIDs to fetch content for.
            user_id: Optional tenant UUID for defense-in-depth filtering.

        Returns:
            Dict mapping ``{episode_id: (content, role)}``.
            Soft-deleted episodes are excluded from the result.
        """
        if not episode_ids:
            return {}

        query = select(Episode.id, Episode.content, Episode.role).where(
            Episode.id.in_(episode_ids), Episode.is_deleted.is_(False)
        )
        if user_id is not None:
            query = query.where(Episode.user_id == user_id)

        result = await self._db.execute(query)
        return {row.id: (row.content, row.role) for row in result.all()}

    # ── Get by ID ────────────────────────────────────────────────────────────

    async def get_by_id(self, episode_id: UUID) -> Episode | None:
        """Look up a single episode by its UUID.

        Args:
            episode_id: The episode's UUID.

        Returns:
            The Episode if found and not soft-deleted, ``None`` otherwise.
        """
        result = await self._db.execute(
            select(Episode).where(
                Episode.id == episode_id,
                Episode.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id_for_update(self, episode_id: UUID) -> Episode | None:
        """Lock and return an episode by ID, with ``SELECT ... FOR UPDATE``.

        Acquires a row-level lock that is held until the transaction commits.
        Use this before any read-modify-write cycle on ``enrichment_status``
        to prevent race conditions between concurrent workers.

        Args:
            episode_id: The episode's UUID.

        Returns:
            The Episode if found and not soft-deleted, ``None`` otherwise.
        """
        result = await self._db.execute(
            select(Episode)
            .where(Episode.id == episode_id, Episode.is_deleted.is_(False))
            .with_for_update()
        )
        return result.scalar_one_or_none()

    # ── Update Enrichment Status ─────────────────────────────────────────────

    async def apply_enrichment_bits(
        self, episode_id: UUID, bitmask: int
    ) -> None:
        """Atomically OR a bitmask into the enrichment_status.

        Uses ``SET enrichment_status = enrichment_status | :bitmask`` so
        that multiple workers can set independent flag bits without a
        read-modify-write race (e.g., bit 3 for entity linking, bit 6 for
        observation pass).  Does NOT clear existing bits.

        Args:
            episode_id: The episode's UUID.
            bitmask: Bit(s) to set (bitwise OR).
        """
        await self._db.execute(
            text(
                "UPDATE episodes "
                "SET enrichment_status = enrichment_status | :bitmask, "
                "updated_at = now() "
                "WHERE id = :id AND is_deleted = false"
            ),
            {"bitmask": bitmask, "id": episode_id},
        )

    # ── Soft Delete by User ──────────────────────────────────────────────────

    async def soft_delete_by_project(
        self, project_id: UUID, user_id: UUID
    ) -> int:
        """Soft-delete all episodes for a project.

        Sets ``is_deleted = True`` and ``updated_at = now()`` for every
        episode belonging to the project. The ``user_id`` predicate
        is defense-in-depth alongside the service-layer org gate.

        Args:
            project_id: The project's UUID.
            user_id: Tenant scope — only rows in this user are touched.

        Returns:
            Number of episodes soft-deleted.
        """
        result = await self._db.execute(
            text(
                "UPDATE episodes SET is_deleted = true, updated_at = now() "
                "WHERE project_id = :project_id "
                "AND user_id = :user_id "
                "AND is_deleted = false"
            ),
            {"project_id": project_id, "user_id": user_id},
        )
        return result.rowcount  # type: ignore[return-value]

    # ── Vector Search ─────────────────────────────────────────────────────────

    # ── BM25 Full-Text Search ─────────────────────────────────────────────────

    async def search_by_bm25(
        self, query: str, project_id: UUID, user_id: UUID, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Search episodes by BM25 full-text (PostgreSQL ``ts_rank``).

        Tokenises the query via ``plainto_tsquery`` and ranks results using
        ``ts_rank`` over an English text search configuration.

        Args:
            query: Raw search text (no special syntax needed).
            project_id: Scope results to this project.
            user_id: Tenant scope for multi-tenant isolation.
            limit: Maximum results (capped at 200).

        Returns:
            A list of dicts with keys ``id``, ``content``, ``role``,
            ``created_at``, and ``score`` (higher = more relevant).
        """
        effective_limit = min(limit, 200)
        result = await self._db.execute(
            text(
                """
                SELECT id, content, role, created_at,
                       ts_rank(
                           to_tsvector('english', content),
                           plainto_tsquery('english', :query)
                       ) AS score
                FROM episodes
                WHERE project_id = :project_id
                  AND user_id = :user_id
                  AND is_deleted = false
                  AND to_tsvector('english', content)
                      @@ plainto_tsquery('english', :query)
                ORDER BY score DESC
                LIMIT :limit
                """
            ),
            {"query": query, "project_id": project_id, "user_id": user_id, "limit": effective_limit},
        )
        return [
            {
                "id": str(r[0]),
                "content": r[1],
                "role": r[2],
                "created_at": str(r[3]),
                "score": float(r[4]),
            }
            for r in result.fetchall()
        ]

    # ── Count by User ────────────────────────────────────────────────────────

    # ── Cursor Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _encode_cursor(sequence_number: int, episode_id: UUID) -> str:
        """Encode ``(sequence_number, episode_id)`` into an opaque cursor.

        Format: versioned envelope ``v1:{sequence_number}|{episode_id_hex}``,
        then base64-encoded with padding stripped. Pre-versioning cursors
        are rejected on decode (BREAKING — restart pagination).

        Args:
            sequence_number: The sequence number of the last item.
            episode_id: The UUID of the last item.

        Returns:
            URL-safe base64 string.
        """
        return encode_versioned_cursor(f"{sequence_number}|{episode_id.hex}")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[int, UUID]:
        """Decode a cursor string back to ``(sequence_number, episode_id)``.

        Args:
            cursor: The opaque cursor string from a previous response.

        Returns:
            Tuple of ``(sequence_number, episode_id)``.

        Raises:
            CursorExpiredError: If the cursor is malformed or versioned
                differently (HTTP 400 ``cursor_expired``).
        """
        try:
            raw = decode_versioned_cursor(cursor)
            seq_str, id_hex = raw.split("|", 1)
            return int(seq_str), UUID(hex=id_hex)
        except CursorExpiredError:
            raise
        except (ValueError, TypeError) as e:
            # CursorExpiredError subclasses ValueError, so it is
            # re-raised above before this generic format-failure handler.
            raise CursorExpiredError(f"Invalid episode cursor: {e}") from e
