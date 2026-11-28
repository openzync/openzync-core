"""Session service — business logic for session management.

Provides create, read, list, and delete operations for conversation
sessions.  All DB access is delegated to ``SessionRepository`` (sessions)
and ``EpisodeBlobRepository`` (message attachments).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from core.enrichment_bits import (
    ENRICHMENT_CLASSIFICATION,
    ENRICHMENT_ENTITIES,
    ENRICHMENT_ENTITY_LINKS,
    ENRICHMENT_FACTS,
    ENRICHMENT_STRUCTURED_EXTRACTION,
)
from core.events import EventType
from core.exceptions import (
    ConflictError,
    ExternalServiceError,
    GraphBackendUnavailableError,
    NotFoundError,
    ValidationError,
)
from core.sorting import SortSpec
from models.episode import Episode
from packages.graph_backend.interface import GraphBackend
from repositories.episode_blob_repository import EpisodeBlobRepository
from repositories.mappers import episode_to_dict, session_to_dict, session_to_list_dict
from repositories.session_repository import SessionRepository
from schemas.common import PaginatedResponse
from schemas.sessions import (
    MessageResponse,
    SessionListResponse,
    SessionResponse,
)
from services.webhook_service import WebhookService

logger = logging.getLogger(__name__)

# Bits proving the LLM enrichment pass completed-or-skipped for an
# episode.  Embedding (bit 1), blob text (bit 7), LLM invalidation
# (bit 8), and deferred observations (bit 6) are excluded — they are
# not independently gating LLM-section work.
SKIPPED_LLM_BITS_MASK = (
    ENRICHMENT_ENTITIES
    | ENRICHMENT_FACTS
    | ENRICHMENT_ENTITY_LINKS
    | ENRICHMENT_CLASSIFICATION
    | ENRICHMENT_STRUCTURED_EXTRACTION
)
"""LLM-section bits; all set + zero LLM rows ⇒ enrichment was skipped."""


class SessionService:
    """Business logic for session management.

    Args:
        repo: The session repository.
        blob_repo: The episode-blob repository for message attachments.
            Optional so unit tests can construct the service with only a
            session mock; production wiring always provides it. When
            ``None``, messages are returned without blob attachments.
        webhook_service: Optional webhook service for event emission.
        graph_backend_resolver: Optional lazy per-user graph-backend
            resolver ``(user_id) -> GraphBackend | None``.  Used only to
            check entity-link presence for ``is_enrichment_skipped``;
            when ``None`` (e.g. unit tests) links count as unknown and
            the flag stays ``False`` (fail closed).
    """

    def __init__(
        self,
        repo: SessionRepository,
        blob_repo: EpisodeBlobRepository | None = None,
        webhook_service: WebhookService | None = None,
        graph_backend_resolver: (
            Callable[[UUID], Awaitable[GraphBackend | None]] | None
        ) = None,
    ) -> None:
        self._repo = repo
        self._blob_repo = blob_repo
        self._webhook_service = webhook_service
        self._graph_resolver = graph_backend_resolver

    # ── Create ──────────────────────────────────────────────────────────────

    async def create_session(
        self,
        user_id: UUID,
        project_id: UUID,
        created_by: UUID,
        external_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> SessionResponse:
        """Create a new session within a project.

        Args:
            user_id: The user UUID for tenant isolation.
            project_id: The project UUID the session belongs to.
            created_by: The UUID of the user creating the session.
            external_id: Caller-defined session identifier (unique per project).
            metadata: Optional session metadata.

        Returns:
            The newly created session response.

        Raises:
            ConflictError: A session with this ``external_id`` already
                exists for the given project.
        """
        # Check for duplicates before inserting.
        existing = await self._repo.get_by_external_id(
            user_id, project_id, external_id
        )
        if existing is not None:
            raise ConflictError(
                f"Session '{external_id}' already exists in project {project_id}"
            )

        session = await self._repo.create(
            user_id=user_id,
            project_id=project_id,
            created_by=created_by,
            external_id=external_id,
            metadata=metadata,
        )

        logger.info(
            "session.created",
            extra={
                "session_id": str(session.id),
                "project_id": str(project_id),
                "created_by": str(created_by),
                "external_id": external_id,
            },
        )

        if self._webhook_service:
            await self._webhook_service.emit(
                user_id=user_id,
                event_type=EventType.SESSION_CREATED,
                payload={
                    "session_id": str(session.id),
                    "project_id": str(project_id),
                    "created_by": str(created_by),
                    "external_id": external_id,
                },
            )

        return SessionResponse.model_validate(
            session_to_dict(session, message_count=0, fact_count=0)
        )

    # ── Get ─────────────────────────────────────────────────────────────────

    async def get_session(
        self, user_id: UUID, session_id: UUID, project_id: UUID | None = None
    ) -> SessionResponse:
        """Get session by UUID with aggregate statistics.

        Args:
            user_id: The user UUID for tenant isolation.
            session_id: The session's UUID.
            project_id: Optional project UUID for intra-org isolation.

        Returns:
            The session response with message/fact counts.

        Raises:
            NotFoundError: Session not found or soft-deleted.
        """
        session = await self._repo.get_by_uuid(
            user_id, session_id, project_id=project_id
        )
        if session is None:
            raise NotFoundError(f"Session {session_id} not found")

        stats = await self._repo.get_stats(session_id)
        observation_count = await self._repo.get_observation_count(user_id, project_id) if project_id else 0

        return SessionResponse.model_validate(
            session_to_dict(
                session,
                message_count=stats["message_count"],
                fact_count=stats["fact_count"],
                pending_enrichment_count=stats.get("pending_enrichment_count", 0),
                observation_count=observation_count,
            )
        )

    # ── List ────────────────────────────────────────────────────────────────

    async def list_sessions(
        self,
        user_id: UUID,
        project_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
        include_closed: bool = False,
        sort: SortSpec | None = None,
    ) -> PaginatedResponse[SessionListResponse]:
        """List sessions for a project with cursor-based pagination.

        By default returns open (non-closed, non-deleted) sessions.
        Default order ``created_at DESC``.

        Args:
            user_id: The user UUID for tenant isolation.
            project_id: The project UUID.
            limit: Maximum items per page (1–200).
            cursor: Opaque base64 cursor from a previous page.
            include_closed: If ``True``, include closed sessions.
            sort: Validated sort spec (forwarded opaque to the repository).

        Returns:
            A paginated response with lightweight session items.

        Raises:
            ValidationError: If ``limit`` is out of range.
        """
        if limit < 1 or limit > 200:
            raise ValidationError("limit must be between 1 and 200")

        sessions, next_cursor = await self._repo.list(
            user_id=user_id,
            project_id=project_id,
            limit=limit,
            cursor=cursor,
            include_closed=include_closed,
            sort=sort,
        )

        # Batch-load message and fact counts — one query instead of N+1.
        session_ids = [s.id for s in sessions]
        stats = await self._repo.batch_get_stats(session_ids, user_id) if session_ids else {}

        items = [
            SessionListResponse.model_validate(
                session_to_list_dict(
                    s,
                    message_count=stats.get(s.id, {}).get("message_count", 0),
                    fact_count=stats.get(s.id, {}).get("fact_count", 0),
                )
            )
            for s in sessions
        ]

        return PaginatedResponse[SessionListResponse](
            data=items,
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    # ── Messages ────────────────────────────────────────────────────────────

    async def get_messages(
        self,
        user_id: UUID,
        session_id: UUID,
        limit: int = 100,
        cursor: str | None = None,
        project_id: UUID | None = None,
        sort: SortSpec | None = None,
    ) -> PaginatedResponse[MessageResponse]:
        """Get paginated messages for a session.

        Messages are ordered by ``sequence_number`` by default for
        deterministic, tie-free ordering. ``created_at`` is offered as
        an alt without breaking the default.

        Args:
            user_id: The user UUID for tenant isolation.
            session_id: The session's UUID.
            limit: Maximum items per page (1–500).
            cursor: Opaque base64 cursor from a previous page.
            project_id: Optional project UUID for intra-org isolation.
            sort: Validated sort spec (forwarded opaque to the repository).

        Returns:
            A paginated response with message items.  Each item carries
            the raw ``enrichment_status`` bitmask off its episode row —
            populated inline from the already-fetched rows, no extra query —
            plus ``is_enrichment_skipped``, derived from batched
            presence checks (see below).

        Raises:
            NotFoundError: If the session does not exist.
            ValidationError: If ``limit`` is out of range.
        """
        if limit < 1 or limit > 500:
            raise ValidationError("limit must be between 1 and 500")

        # Verify the session exists before fetching messages.
        session = await self._repo.get_by_uuid(
            user_id, session_id, project_id=project_id
        )
        if session is None:
            raise NotFoundError(f"Session {session_id} not found")

        messages, next_cursor = await self._repo.get_messages(
            user_id=user_id,
            session_id=session_id,
            limit=limit,
            cursor=cursor,
            sort=sort,
        )

        # Batch-load blob attachments for the page in one IN query via
        # EpisodeBlobRepository.get_by_episodes. Fail loud: a blob-store
        # failure must propagate (500) — silently returning a blob-less page
        # would hide storage outages. Skipped when no blob repo is wired
        # (unit-test construction) or the page is empty (no empty IN query).
        episode_blob_map: dict[UUID, list[dict]] = {}
        if self._blob_repo is not None and messages:
            blobs_by_episode = await self._blob_repo.get_by_episodes(
                [m.id for m in messages]
            )
            episode_blob_map = {
                ep_id: [
                    {
                        "id": b.id,
                        "file_name": b.file_name,
                        "mime_type": b.mime_type,
                        "file_size": b.file_size,
                        "download_url": None,
                    }
                    for b in blobs
                ]
                for ep_id, blobs in blobs_by_episode.items()
            }

        items = []
        skipped_ids = await self._find_skipped_episodes(
            user_id=user_id,
            project_id=project_id or session.project_id,
            messages=messages,
        )
        for m in messages:
            msg_dict = episode_to_dict(m)
            blobs = episode_blob_map.get(m.id)
            if blobs is not None:
                msg_dict["blobs"] = blobs
            msg_dict["is_enrichment_skipped"] = m.id in skipped_ids
            items.append(MessageResponse.model_validate(msg_dict))

        return PaginatedResponse[MessageResponse](
            data=items,
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def _find_skipped_episodes(
        self,
        user_id: UUID,
        project_id: UUID,
        messages: list[Episode],
    ) -> set[UUID]:
        """Return page episode IDs whose enrichment was mark-skipped.

        An episode flags only when every LLM-section bit is set on its
        row (in-flight episodes with partial bits never flag) AND no
        LLM-produced rows exist for it in ``dialog_classifications``,
        ``facts``, ``structured_extractions``, or the graph entity
        links.  Presence is checked batched: one ``IN`` query per
        Postgres table for the whole page plus one
        ``get_entities_for_episodes`` call.  No query runs when no
        episode has all bits set.

        The entity-link check is page-wide and conservative: the
        backend returns a flat entity list with no per-episode
        mapping, so any linked entity clears the flag for every
        candidate (an empty result proves zero links for all of
        them).  Unknown link state (no resolver, backend down)
        likewise never flags — fail closed with a loud log.

        Args:
            user_id: The user UUID for tenant isolation.
            project_id: The project UUID for graph scoping.
            messages: The page's episode rows.

        Returns:
            IDs of episodes to mark ``is_enrichment_skipped``.
        """
        candidates = [
            m
            for m in messages
            if (m.enrichment_status or 0) & SKIPPED_LLM_BITS_MASK
            == SKIPPED_LLM_BITS_MASK
        ]
        if not candidates:
            return set()
        candidate_ids = [m.id for m in candidates]

        with_rows = await self._repo.get_episodes_with_llm_rows(candidate_ids)
        if len(with_rows) == len(candidate_ids):
            return set()

        if not await self._has_no_entity_links(
            user_id, project_id, candidate_ids
        ):
            return set()
        return {m.id for m in candidates if m.id not in with_rows}

    async def _has_no_entity_links(
        self,
        user_id: UUID,
        project_id: UUID,
        episode_ids: list[UUID],
    ) -> bool:
        """Return ``True`` only when no episode has linked entities.

        Args:
            user_id: The user UUID for tenant isolation.
            project_id: The project UUID for graph scoping.
            episode_ids: Candidate episode UUIDs to scope the lookup to.

        Returns:
            ``True`` when the batched backend lookup returns zero
            entities; ``False`` on any unknown state (no resolver,
            unresolved backend, backend error) — fail closed.
        """
        if self._graph_resolver is None:
            return False
        try:
            backend = await self._graph_resolver(user_id)
        except GraphBackendUnavailableError as exc:
            logger.warning(
                "session.messages.entity_links_unresolved",
                extra={
                    "user_id": str(user_id),
                    "project_id": str(project_id),
                    "error": str(exc),
                },
            )
            return False
        if backend is None:
            return False
        try:
            entities = await backend.get_entities_for_episodes(
                user_id=user_id,
                project_id=project_id,
                episode_ids=episode_ids,
            )
        except ExternalServiceError as exc:
            logger.warning(
                "session.messages.entity_links_lookup_failed",
                extra={
                    "user_id": str(user_id),
                    "project_id": str(project_id),
                    "episode_count": len(episode_ids),
                    "error": str(exc),
                },
            )
            return False
        return len(entities) == 0

    # ── Delete ──────────────────────────────────────────────────────────────

    async def delete_session(
        self, user_id: UUID, session_id: UUID, project_id: UUID | None = None
    ) -> None:
        """Soft-delete a session, scoped to user and optionally project.

        Args:
            user_id: The user UUID for tenant isolation.
            session_id: The session's UUID.
            project_id: Optional project UUID for intra-org isolation.

        Raises:
            NotFoundError: Session not found or already deleted.
        """
        session = await self._repo.soft_delete(
            user_id, session_id, project_id=project_id
        )
        if session is None:
            raise NotFoundError(f"Session {session_id} not found")

        logger.info(
            "session.deleted",
            extra={
                "session_id": str(session_id),
                "project_id": str(session.project_id),
            },
        )

        if self._webhook_service:
            await self._webhook_service.emit(
                user_id=user_id,
                event_type=EventType.SESSION_CLOSED,
                payload={
                    "session_id": str(session_id),
                    "project_id": str(session.project_id),
                },
            )
