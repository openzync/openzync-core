"""Ingest orchestration — the primary entry point for persisting memory.

Steps (see :meth:`IngestMixin.ingest`): idempotency → session resolve →
atomic dedup claim → episode build/PII/lock/insert → blob upload →
commit → post-commit fan-out. Step implementations live in the sibling
mixins; this module only sequences them and owns the transaction boundary.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from core.exceptions import NotFoundError, ValidationError
from schemas.memory import IngestMemoryResponse, Message

if TYPE_CHECKING:
    from fastapi import UploadFile

    from models.session import Session
    from models.user import User

logger = logging.getLogger(__name__)


class IngestMixin:
    """Ingest orchestration plus session/user resolution."""

    _db: Any
    _redis: Any
    _webhook_service: Any
    _idem: Any
    _session_repo: Any
    _user_repo: Any
    _project_repo: Any

    async def ingest(
        self,
        org_id: UUID,
        project_id: UUID,
        created_by: UUID,
        session_external_id: str,
        messages: list[Message],
        uploaded_blobs: list[UploadFile] | None = None,
        idempotency_key: str | None = None,
        body_hash: str | None = None,
    ) -> IngestMemoryResponse:
        """Ingest messages into a project's memory.

        Flow:
        1. Reject archived/missing projects (404, before any state).
        2. Idempotency check (Redis) — replay or 409 on key reuse.
        3. Resolve the session (never auto-created).
        4. Atomically claim the batch (Redis Lua + ``ingest_dedup``
           UNIQUE backstop) — losers replay the winner.
        5. Build episode dicts, PII-redact (fail-closed, BEFORE the
           lock), lock the session row, assign sequences, batch-insert.
        6. Upload blobs + persist blob rows (same transaction;
           pre-commit failures compensate S3 inline).
        7. Commit — workers can now see episodes + blobs.
        8. Post-commit: enqueue ARQ tasks + blob extraction, store the
           idempotency key, purge context cache, emit webhooks
           (all best-effort — never fail a committed ingest).

        Args:
            org_id: The authenticated organization UUID.
            project_id: The project UUID for scoping.
            created_by: The authenticated user's UUID (attribution).
            session_external_id: The session external ID. The session must
                already exist — it is never auto-created.
            messages: List of validated message objects.
            uploaded_blobs: Optional uploaded files from a multipart
                request, indexed by ``BlobMetadata.blob_id``.
            idempotency_key: Optional ``Idempotency-Key`` header value.
            body_hash: Optional SHA-256 of the canonical request body,
                pre-computed by the router when ``idempotency_key`` is set.

        Returns:
            An ``IngestMemoryResponse`` with job_id, episode_count,
            and blob_count.

        Raises:
            ConflictError: Key reused with a different body, or a lost
                sequence-number race (retry the request).
            NotFoundError: Project missing/archived, or session unknown.
            PIIUnavailableError: PII policy fetch/redaction failed (503).
            ExternalServiceError: Blob storage config unreadable (502).
        """
        # ── Step 0: Block ingests to archived projects ───────────────────
        project = await self._project_repo.get_by_id(org_id, project_id)
        if project is None:
            raise NotFoundError(
                message=f"Project {project_id} not found",
                detail={"project_id": str(project_id)},
            )

        # ── Steps 1–2: Idempotency check + session resolve ───────────────
        if idempotency_key is not None:
            replay = await self._check_idempotency_key(
                idempotency_key, body_hash or "", org_id, project_id
            )
            if replay is not None:
                return replay
        session = await self._resolve_session(
            organization_id=org_id,
            project_id=project_id,
            session_external_id=session_external_id,
        )
        session_id = session.id
        logger.debug(
            "memory.session_resolved",
            extra={
                "session_id": str(session_id),
                "external_id": session.external_id,
                "project_id": str(project_id),
                "created_by": str(created_by),
            },
        )

        # ── Steps 3–4: Canonical msgs + atomic claim ─────────────────────
        msgs = [m.model_dump() for m in messages]
        job_id, replay = await self._claim_batch(
            org_id, created_by, session_id, msgs, project_id
        )
        if replay is not None:
            return replay

        # A won claim is held from here on — any failure below must
        # release it (fail-open) so a retry re-claims instead of
        # replaying this attempt's job_id. The ingest_dedup row stays
        # the arbiter either way.
        # ``uploaded_keys`` tracks S3 objects written by this attempt so a
        # pre-commit failure can compensate them (see except handlers).
        uploaded_keys: list[str] = []
        try:
            # ── Step 5: Build dicts, redact, lock + insert ───────────────
            episode_dicts = [
                {
                    "role": msg.role,
                    "content": msg.content,
                    "metadata": msg.metadata,
                    "created_at": msg.created_at,
                }
                for msg in messages
            ]
            await self._redact_episodes(
                org_id, episode_dicts, job_id
            )
            episodes = await self._lock_and_insert(
                org_id, project_id, session_id, created_by, episode_dicts
            )

            # ── Step 6: Blobs (same transaction) ─────────────────────────
            blob_count = 0
            blob_records: list[Any] = []
            if uploaded_blobs:
                blob_count, blob_records = await self._process_blobs(
                    org_id=org_id,
                    project_id=project_id,
                    session_id=session_id,
                    created_by=created_by,
                    episodes=episodes,
                    messages=messages,
                    uploaded_blobs=uploaded_blobs,
                )
                uploaded_keys = [
                    b.storage_key for b in blob_records if b.storage_key
                ]
            elif any(msg.blobs for msg in messages):
                # Fail loud — the router only validates blob_ids when files
                # are present, so metadata-without-files would otherwise
                # ingest as text-only and silently drop attachments.
                raise ValidationError(
                    "messages reference blobs but no files were uploaded"
                )

            # ── Commit so workers see episodes + blobs before tasks ──────
            # The error-handling block ends HERE. Everything below runs
            # post-commit: failures must never roll back, compensate blob
            # files, or release the content-hash claim for committed rows.
            await self._db.commit()
        except asyncio.CancelledError:
            await self._db.rollback()
            await self._compensate_blob_uploads(
                org_id, uploaded_keys
            )
            await self._idem.release_content_hash(
                str(org_id), str(created_by), str(session_id), msgs
            )
            raise
        except Exception:
            await self._db.rollback()
            await self._compensate_blob_uploads(
                org_id, uploaded_keys
            )
            await self._idem.release_content_hash(
                str(org_id), str(created_by), str(session_id), msgs
            )
            raise

        # ── Steps 7–8: Post-commit fan-out (best-effort) ─────────────────
        return await self._finalize_ingest(
            job_id=job_id,
            org_id=org_id,
            project_id=project_id,
            session_id=session_id,
            episodes=episodes,
            blob_records=blob_records,
            blob_count=blob_count,
            idempotency_key=idempotency_key,
            body_hash=body_hash,
        )

    async def _resolve_user(
        self,
        org_id: UUID,
        external_id: str,
    ) -> User:
        """Resolve a user by external_id, creating one if it does not exist.

        Thread-safe via the ``(organization_id, external_id)`` unique
        constraint — concurrent creates are handled with an IntegrityError
        retry in the repository layer.

        Args:
            org_id: The organization UUID.
            external_id: The caller-defined user identifier.

        Returns:
            A ``User`` ORM instance (existing or newly created).
        """
        return await self._user_repo.create_or_get_by_external_id(
            organization_id=org_id,
            external_id=external_id,
        )

    async def _resolve_session(
        self,
        organization_id: UUID,
        project_id: UUID,
        session_external_id: str,
    ) -> Session:
        """Resolve an existing session by its external ID.

        Look up the existing session and raise ``NotFoundError`` if it
        does not exist.  Sessions are NOT auto-created from arbitrary
        IDs — the SDK must call ``POST /sessions`` first.

        Args:
            organization_id: The organization UUID.
            project_id: The project UUID.
            session_external_id: The caller-defined session identifier.

        Returns:
            A ``Session`` ORM instance.

        Raises:
            NotFoundError: If no session with the given identifier exists.
        """
        # Try by external_id first (the canonical lookup).
        session = await self._session_repo.get_by_external_id(
            org_id=organization_id,
            project_id=project_id,
            external_id=session_external_id,
        )
        if session is None:
            # Fallback: try resolving as a raw UUID — the caller may
            # have passed the session's internal UUID rather than its
            # user-facing external_id.
            try:
                parsed = UUID(session_external_id)
            except ValueError:
                parsed = None
            if parsed is not None:
                session = await self._session_repo.get_by_uuid(
                    org_id=organization_id,
                    session_id=parsed,
                    project_id=project_id,
                )
        if session is None:
            raise NotFoundError(
                f"Session '{session_external_id}' not found in project {project_id}"
            )
        return session
