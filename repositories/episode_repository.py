"""Episode repository — all database access for the Episode model.

Episodes represent individual message turns within a conversation session.
Every query filters by ``is_deleted = False`` (soft-delete convention).
Cursor pagination uses ``sequence_number`` for deterministic ordering.

Key patterns:
- ``batch_create`` uses raw ``INSERT ... RETURNING`` SQL because the ORM
  model does not yet map ``organization_id`` as a column (the column exists
  in the schema via migration 0001, but the model mixin is pending).
- All list queries use LIMIT + 1 to detect ``has_more`` without a COUNT.
- Cursor encoding uses ``{sequence_number}|{episode_id_hex}``, base64.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.episode import Episode

# ╠ This file contains NO business logic — only query construction.
# ╠ If you find yourself writing an ``if`` statement that makes a
# ╠ decision based on domain rules, it belongs in the service layer.


class EpisodeRepository:
    """All database access for episodes (messages).

    Every method accepts ``organization_id`` to enforce tenant isolation.
    No business logic — pure query construction and execution.

    Args:
        db: An async SQLAlchemy session.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Batch Create ─────────────────────────────────────────────────────────

    async def batch_create(
        self,
        organization_id: UUID,
        session_id: UUID,
        user_id: UUID,
        messages: list[dict[str, Any]],
    ) -> list[Episode]:
        """Insert multiple episodes in a single round-trip with RETURNING.

        Uses raw SQL to include ``organization_id`` (the ORM model does not
        yet map this column, but the schema requires it for RLS).

        Args:
            organization_id: Tenant scope (included for RLS enforcement).
            session_id: The parent session's UUID.
            user_id: The owning user's UUID.
            messages: List of message dicts, each containing ``role``,
                ``content``, ``metadata``, and optionally ``created_at``.

        Returns:
            A list of ``Episode`` ORM instances with generated fields
            populated (id, sequence_number, timestamps, etc.).
        """
        if not messages:
            return []

        values: list[str] = []
        params: dict[str, object] = {}
        now = datetime.now(timezone.utc)

        for i, msg in enumerate(messages):
            episode_id = uuid4()
            seq = msg.get("sequence_number", i)

            params[f"id_{i}"] = episode_id
            params[f"org_id_{i}"] = organization_id
            params[f"session_id_{i}"] = session_id
            params[f"user_id_{i}"] = user_id
            params[f"role_{i}"] = msg["role"]
            params[f"content_{i}"] = msg["content"]
            params[f"metadata_{i}"] = json.dumps(msg.get("metadata", {}))
            params[f"created_at_{i}"] = msg.get("created_at") or now
            params[f"seq_{i}"] = seq

            placeholders = (
                f"(:id_{i}, :org_id_{i}, :session_id_{i}, :user_id_{i}, "
                f":role_{i}, :content_{i}, :metadata_{i}, "
                f":created_at_{i}, :seq_{i})"
            )
            values.append(placeholders)

        stmt = text(
            f"""
            INSERT INTO episodes (
                id, organization_id, session_id, user_id,
                role, content, metadata, created_at, sequence_number
            )
            VALUES {', '.join(values)}
            RETURNING
                id, organization_id, session_id, user_id,
                role, content, metadata, embedding, token_count,
                sequence_number, enrichment_status, is_deleted,
                graphiti_node_id, created_at, updated_at
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
                organization_id=mapping["organization_id"],
                session_id=mapping["session_id"],
                user_id=mapping["user_id"],
                role=mapping["role"],
                content=mapping["content"],
                metadata_=mapping["metadata"] or {},
                embedding=mapping["embedding"],
                token_count=mapping["token_count"],
                sequence_number=mapping["sequence_number"],
                enrichment_status=mapping["enrichment_status"],
                is_deleted=mapping["is_deleted"],
                graphiti_node_id=mapping["graphiti_node_id"],
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
                    Episode.sequence_number > cursor_seq,
                    Episode.sequence_number == cursor_seq,
                    Episode.id > cursor_id,
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

    # ── Read — Paginated by User ─────────────────────────────────────────────

    async def get_by_user_id(
        self,
        user_id: UUID,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[Episode], str | None]:
        """Get paginated episodes for a user, ordered by created_at DESC.

        Args:
            user_id: The user's UUID.
            limit: Maximum results per page (capped at 500).
            cursor: Opaque base64 cursor from a previous page.

        Returns:
            A tuple of ``(episodes, next_cursor)``.
        """
        effective_limit = min(limit, 500) + 1

        query = select(Episode).where(
            Episode.user_id == user_id,
            Episode.is_deleted.is_(False),
        )

        if cursor is not None:
            cursor_seq, cursor_id = self._decode_cursor(cursor)
            query = query.where(
                or_(
                    Episode.sequence_number > cursor_seq,
                    Episode.sequence_number == cursor_seq,
                    Episode.id > cursor_id,
                )
            )

        query = query.order_by(
            Episode.created_at.desc(), Episode.id.asc()
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

    # ── Next Sequence Number ─────────────────────────────────────────────────

    async def get_next_sequence(self, session_id: UUID) -> int:
        """Get the next available sequence number for a session.

        Uses ``SELECT COALESCE(MAX(sequence_number), -1) + 1``, which is
        safe within a transaction because the increment and subsequent
        INSERT share the same snapshot.

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

    # ── Update Enrichment Status ─────────────────────────────────────────────

    async def update_enrichment_status(
        self, episode_id: UUID, bitmask: int
    ) -> None:
        """Set the enrichment_status bitmask for an episode.

        Uses an atomic ``UPDATE ... SET enrichment_status = :bitmask``
        query to avoid read-modify-write race conditions.

        Args:
            episode_id: The episode's UUID.
            bitmask: The new enrichment status bitmask value.
        """
        await self._db.execute(
            text(
                "UPDATE episodes SET enrichment_status = :bitmask, "
                "updated_at = now() WHERE id = :id AND is_deleted = false"
            ),
            {"bitmask": bitmask, "id": episode_id},
        )

    # ── Soft Delete by User ──────────────────────────────────────────────────

    async def soft_delete_by_user(self, user_id: UUID) -> int:
        """Soft-delete all episodes for a user.

        Sets ``is_deleted = True`` and ``updated_at = now()`` for every
        episode belonging to the user. Returns the count of affected rows.

        Args:
            user_id: The user's UUID.

        Returns:
            Number of episodes soft-deleted.
        """
        result = await self._db.execute(
            text(
                "UPDATE episodes SET is_deleted = true, updated_at = now() "
                "WHERE user_id = :user_id AND is_deleted = false"
            ),
            {"user_id": user_id},
        )
        return result.rowcount  # type: ignore[return-value]

    # ── Vector Search ─────────────────────────────────────────────────────────

    async def search_by_vector(
        self, embedding: list[float], user_id: UUID, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Search episodes by vector similarity (pgvector cosine distance).

        Uses the ``<=>`` operator which computes cosine distance. The score
        is inverted (``1 - distance``) so that higher = more similar.

        Args:
            embedding: The query embedding vector.
            user_id: Scope results to this user.
            limit: Maximum results (capped at 200).

        Returns:
            A list of dicts with keys ``id``, ``content``, ``role``,
            ``created_at``, and ``score`` (0.0–1.0).
        """
        effective_limit = min(limit, 200)
        result = await self._db.execute(
            text(
                """
                SELECT id, content, role, created_at,
                       1 - (embedding <=> :embedding) AS score
                FROM episodes
                WHERE user_id = :user_id
                  AND is_deleted = false
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> :embedding
                LIMIT :limit
                """
            ),
            {"embedding": embedding, "user_id": user_id, "limit": effective_limit},
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

    # ── BM25 Full-Text Search ─────────────────────────────────────────────────

    async def search_by_bm25(
        self, query: str, user_id: UUID, org_id: UUID, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Search episodes by BM25 full-text (PostgreSQL ``ts_rank``).

        Tokenises the query via ``plainto_tsquery`` and ranks results using
        ``ts_rank`` over an English text search configuration.

        Args:
            query: Raw search text (no special syntax needed).
            user_id: Scope results to this user.
            org_id: Tenant scope for multi-tenant isolation.
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
                WHERE user_id = :user_id
                  AND organization_id = :org_id
                  AND is_deleted = false
                  AND to_tsvector('english', content)
                      @@ plainto_tsquery('english', :query)
                ORDER BY score DESC
                LIMIT :limit
                """
            ),
            {"query": query, "user_id": user_id, "org_id": org_id, "limit": effective_limit},
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

    async def count_by_user(self, user_id: UUID) -> int:
        """Count non-deleted episodes for a user.

        Args:
            user_id: The user's UUID.

        Returns:
            Total number of active episodes for the user.
        """
        result = await self._db.execute(
            select(func.count(Episode.id)).where(
                Episode.user_id == user_id,
                Episode.is_deleted.is_(False),
            )
        )
        return result.scalar() or 0

    # ── Cursor Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _encode_cursor(sequence_number: int, episode_id: UUID) -> str:
        """Encode ``(sequence_number, episode_id)`` into an opaque base64 cursor.

        Format: ``{sequence_number}|{episode_id_hex}``, then base64-encoded
        with padding stripped.

        Args:
            sequence_number: The sequence number of the last item.
            episode_id: The UUID of the last item.

        Returns:
            URL-safe base64 string.
        """
        raw = f"{sequence_number}|{episode_id.hex}"
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[int, UUID]:
        """Decode a cursor string back to ``(sequence_number, episode_id)``.

        Args:
            cursor: The opaque cursor string from a previous response.

        Returns:
            Tuple of ``(sequence_number, episode_id)``.

        Raises:
            ValueError: If the cursor is malformed.
        """
        try:
            # Restore padding stripped by rstrip("=")
            padding = 4 - len(cursor) % 4
            if padding != 4:
                cursor += "=" * padding
            raw = base64.urlsafe_b64decode(cursor.encode()).decode()
            seq_str, id_hex = raw.split("|", 1)
            return int(seq_str), UUID(hex=id_hex)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid episode cursor: {e}") from e
