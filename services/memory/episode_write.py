"""Episode write helpers — row lock, sequence assignment, batch insert."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from core.exceptions import ConflictError

logger = logging.getLogger(__name__)


class EpisodeWriteMixin:
    """Serialize sequence assignment under the session row lock."""

    _db: Any
    _session_repo: Any
    _episode_repo: Any

    async def _lock_and_insert(
        self,
        org_id: UUID,
        project_id: UUID,
        session_id: UUID,
        created_by: UUID,
        episode_dicts: list[dict[str, Any]],
    ) -> list[Any]:
        """Lock the session row, assign sequences, batch-insert episodes.

        The FOR UPDATE lock serializes concurrent ingests into this
        session; it is held only across MAX(sequence_number)+1, seq
        assignment, and the batch INSERT (all local work + one
        round-trip — no network I/O under lock).

        Args:
            org_id: Tenant scope.
            project_id: Project scope.
            session_id: Target session.
            created_by: Attributing user.
            episode_dicts: Per-message dicts (sequence numbers assigned
                in place).

        Returns:
            The inserted ``Episode`` ORM rows.

        Raises:
            ConflictError: If the sequence race is lost despite the row
                lock (the unique index held — retry the request).
        """
        from schemas.memory import Message  # noqa: F401 — re-export anchor

        await self._session_repo.get_by_id_for_update(session_id)
        start_seq = await self._episode_repo.get_next_sequence(session_id)
        for i, msg_dict in enumerate(episode_dicts):
            msg_dict["sequence_number"] = start_seq + i

        try:
            episodes = await self._episode_repo.batch_create(
                organization_id=org_id,
                session_id=session_id,
                project_id=project_id,
                user_id=created_by,
                messages=episode_dicts,
                start_seq=start_seq,
            )
        except IntegrityError as exc:
            # Lost the seq race despite the row lock (or a colliding
            # legacy row) — the unique index held; retry the request.
            logger.warning(
                "memory.sequence_conflict",
                extra={
                    "session_id": str(session_id),
                    "project_id": str(project_id),
                    "org_id": str(org_id),
                },
            )
            raise ConflictError(
                "Sequence conflict during ingest — retry the request"
            ) from exc
        logger.info(
            "memory.episodes_created",
            extra={
                "count": len(episodes),
                "session_id": str(session_id),
                "project_id": str(project_id),
                "org_id": str(org_id),
            },
        )
        return episodes
