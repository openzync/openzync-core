"""Unit tests for MemoryService — ingestion logic with mocked dependencies."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from core.events import EventType
from core.exceptions import ConflictError, NotFoundError
from schemas.memory import IngestMemoryResponse, Message
from services.idempotency_service import (
    IdempotencyResult,
    IdempotencyService,
    IdempotencyStatus,
)
from services.memory_service import MemoryService
from services.webhook_service import WebhookService


def _mock_idempotency_service() -> AsyncMock:
    """Build a mock IdempotencyService with benign defaults.

    Defaults: key check returns NEW, content hash check returns None —
    every ingest proceeds down the happy path unless a test overrides.
    ``compute_content_hash`` is a sync mock (it is a staticmethod in the
    real service) so it returns a string, not an unawaited coroutine.
    """
    idem = AsyncMock()
    idem.check_idempotency_key.return_value = IdempotencyResult(
        status=IdempotencyStatus.NEW
    )
    idem.check_content_hash.return_value = None
    idem.compute_content_hash = MagicMock(return_value="abc123")
    return idem


@pytest.mark.unit
class TestMemoryService:
    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")

    @pytest.fixture
    def service(self) -> MemoryService:
        mock_db = AsyncMock(spec=AsyncSession)
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None  # no cached idempotency
        mock_redis.set.return_value = True
        mock_redis.setex.return_value = True
        mock_redis.scan.return_value = (0, [])
        mock_redis.delete.return_value = 0
        mock_episode_repo = AsyncMock()
        mock_episode_repo.batch_create.return_value = []
        mock_session_repo = AsyncMock()
        mock_user_repo = AsyncMock()
        mock_fact_repo = AsyncMock()

        return MemoryService(
            db=mock_db,
            redis_client=mock_redis,
            episode_repo=mock_episode_repo,
            session_repo=mock_session_repo,
            user_repo=mock_user_repo,
            fact_repo=mock_fact_repo,
            idempotency_service=_mock_idempotency_service(),
            dedup_repo=AsyncMock(),
        )

    def _sample_messages(self, count: int = 2) -> list[Message]:
        return [
            Message(role="user" if i % 2 == 0 else "assistant",
                    content=f"Message {i}")
            for i in range(count)
        ]

    @pytest.mark.asyncio
    async def test_ingest_resolves_user(self, service: MemoryService) -> None:
        """Ingest accepts a ``created_by`` UUID directly (no user look-up)."""
        service._session_repo.get_or_create_default.return_value = MagicMock(
            id=uuid4(), external_id="__default__",
        )

        with (
            patch.object(service, "_enqueue_arq_tasks"),
            patch.object(service, "_invalidate_context_cache"),
            patch.object(service, "_get_org_pii_config", return_value={}),
        ):
            result = await service.ingest(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                session_external_id=None,
                messages=self._sample_messages(),
            )
        assert result.status == "accepted"

    @pytest.mark.asyncio
    async def test_ingest_without_user_lookup_succeeds(
        self, service: MemoryService,
    ) -> None:
        """Ingest does not look up the user when ``created_by`` is a UUID.

        ``MemoryService.ingest`` passes ``created_by`` directly to the
        session resolver — it no longer calls ``user_repo.get_by_uuid``.
        """
        service._session_repo.get_or_create_default.return_value = MagicMock(
            id=uuid4(), external_id="__default__",
        )

        with (
            patch.object(service, "_enqueue_arq_tasks"),
            patch.object(service, "_invalidate_context_cache"),
            patch.object(service, "_get_org_pii_config", return_value={}),
        ):
            result = await service.ingest(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                session_external_id="test",
                messages=self._sample_messages(),
            )
        assert result.status == "accepted"
        service._user_repo.get_by_uuid.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_user_memory(self, service: MemoryService) -> None:
        """Delete memory soft-deletes episodes and facts."""
        service._episode_repo.soft_delete_by_project.return_value = 5
        service._fact_repo.soft_delete_by_project.return_value = 3

        with patch.object(service, "_invalidate_context_cache"):
            episodes, facts = await service.delete_project_memory(
                org_id=self.ORG_ID, project_id=self.PROJECT_ID,
            )
        assert episodes == 5
        assert facts == 3

    def test_compute_content_hash_is_deterministic(self) -> None:
        """Same inputs produce the same hash; metadata/blobs participate."""
        msgs = [
            {
                "role": "user",
                "content": "Message 0",
                "metadata": {"k": "v"},
                "blobs": [],
            },
            {"role": "assistant", "content": "Message 1"},
        ]
        h1 = IdempotencyService.compute_content_hash(
            str(self.ORG_ID), str(self.USER_ID), "session_1", msgs,
        )
        h2 = IdempotencyService.compute_content_hash(
            str(self.ORG_ID), str(self.USER_ID), "session_1", msgs,
        )
        assert h1 == h2

        msgs_with_other_meta = [
            {
                "role": "user",
                "content": "Message 0",
                "metadata": {"k": "other"},
                "blobs": [],
            },
            {"role": "assistant", "content": "Message 1"},
        ]
        h3 = IdempotencyService.compute_content_hash(
            str(self.ORG_ID), str(self.USER_ID), "session_1", msgs_with_other_meta,
        )
        assert h3 != h1  # different metadata → different hash

    @pytest.mark.asyncio
    async def test_commit_before_enqueue(self, service: MemoryService) -> None:
        """ingest() calls db.commit() before _enqueue_arq_tasks().

        This ordering is critical for the transaction-visibility fix:
        episodes must be visible to PostgreSQL *before* ARQ enrichment
        tasks are enqueued to Redis, otherwise workers may race ahead
        and fail with EpisodeNotFoundError.
        """
        service._session_repo.get_or_create_default.return_value = MagicMock(
            id=uuid4(), external_id="__default__",
        )

        # Make batch_create return a real-looking episode list
        mock_episode = MagicMock()
        mock_episode.id = uuid4()
        mock_episode.content = "test content"
        mock_episode.role = "user"
        mock_episode.metadata_ = {}
        service._episode_repo.batch_create.return_value = [mock_episode]

        # Track call order across two different mocks (db vs service method).
        # NOTE: We use a list as a side_effect and *do not* call the original
        # mock — doing so would re-trigger the side_effect (infinite recursion).
        call_order: list[str] = []

        async def _tracked_commit() -> None:  # type: ignore[misc]
            call_order.append("commit")

        service._db.commit.side_effect = _tracked_commit

        with patch.object(service, "_enqueue_arq_tasks") as mock_enqueue:
            def _tracked_enqueue(*args: object, **kwargs: object) -> None:
                call_order.append("enqueue")
            mock_enqueue.side_effect = _tracked_enqueue

            with (
                patch.object(service, "_invalidate_context_cache"),
                patch.object(service, "_get_org_pii_config", return_value={}),
            ):
                result = await service.ingest(
                    org_id=self.ORG_ID,
                    project_id=self.PROJECT_ID,
                    created_by=self.USER_ID,
                    session_external_id=None,
                    messages=self._sample_messages(),
                )

        assert result.status == "accepted"
        assert call_order == ["commit", "enqueue"], (
            f"Expected commit before enqueue, got {call_order}"
        )
        service._db.commit.assert_awaited_once()
        mock_enqueue.assert_awaited_once()

    # ------------------------------------------------------------------
    # Idempotency replay — cached response returned without pipeline
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_ingest_idempotency_replay(self, service: MemoryService) -> None:
        """Ingest returns cached response when key check reports REPLAY."""
        cached = IngestMemoryResponse(
            job_id="replayed-job", episode_count=2, blob_count=0,
            status="accepted", message="Replayed",
        )
        service._idem.check_idempotency_key.return_value = IdempotencyResult(
            status=IdempotencyStatus.REPLAY,
            response_data=cached.model_dump(),
        )

        with patch.object(service, "_enqueue_arq_tasks") as mock_enqueue, \
             patch.object(service, "_invalidate_context_cache") as mock_invalidate:
            result = await service.ingest(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                session_external_id=None,
                messages=self._sample_messages(),
                idempotency_key="dup-key",
            )
        assert result == cached
        service._idem.check_idempotency_key.assert_awaited_once_with(
            "dup-key", "", str(self.ORG_ID)
        )
        mock_enqueue.assert_not_called()
        mock_invalidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_ingest_idempotency_conflict(self, service: MemoryService) -> None:
        """Ingest raises ConflictError when key was used with a different body."""
        service._idem.check_idempotency_key.return_value = IdempotencyResult(
            status=IdempotencyStatus.CONFLICT,
        )

        with pytest.raises(ConflictError):
            await service.ingest(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                session_external_id=None,
                messages=self._sample_messages(),
                idempotency_key="dup-key",
                body_hash="other-hash",
            )

    # ------------------------------------------------------------------
    # Content-level dedup hit
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_ingest_content_dedup_hit(self, service: MemoryService) -> None:
        """Ingest returns existing job_id when content hash matches."""
        service._session_repo.get_or_create_default.return_value = MagicMock(
            id=uuid4(), external_id="__default__",
        )
        existing_job_id = "existing-job-123"
        service._idem.check_content_hash.return_value = existing_job_id

        with patch.object(service, "_enqueue_arq_tasks") as mock_enqueue:
            result = await service.ingest(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                session_external_id=None,
                messages=self._sample_messages(),
            )
        assert result.job_id == existing_job_id
        assert result.status == "accepted"
        # Redis fast-path pre-check short-circuits before the ingest_dedup
        # claim — the DB claim must not be reached on a hash hit.
        service._dedup_repo.insert_or_none.assert_not_awaited()
        service._idem.check_content_hash.assert_awaited_once()
        mock_enqueue.assert_not_called()

    # ------------------------------------------------------------------
    # PII redaction during ingest
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_ingest_with_pii_redaction(self, service: MemoryService) -> None:
        """Ingest redacts content when org has PII masking enabled."""
        service._session_repo.get_or_create_default.return_value = MagicMock(
            id=uuid4(), external_id="__default__",
        )
        mock_pii = AsyncMock()
        mock_pii.process_message.side_effect = [
            ("[REDACTED] Message 0", [], False),
            ("Message 1", [], False),
        ]

        with (
            patch.object(service, "_enqueue_arq_tasks"),
            patch.object(service, "_invalidate_context_cache"),
            patch.object(service, "_get_org_pii_config", return_value={"mode": "mask"}),
            patch("services.pii_service.PIIService", return_value=mock_pii),
        ):
            result = await service.ingest(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                session_external_id=None,
                messages=self._sample_messages(),
            )
        assert result.status == "accepted"
        assert mock_pii.process_message.call_count == 2

    # ------------------------------------------------------------------
    # Webhook events emission
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_ingest_webhook_events(self, service: MemoryService) -> None:
        """Ingest emits webhook events when webhook_service is configured."""
        mock_webhook = AsyncMock(spec=WebhookService)
        service._webhook_service = mock_webhook

        service._session_repo.get_or_create_default.return_value = MagicMock(
            id=uuid4(), external_id="__default__",
        )

        with patch.object(service, "_enqueue_arq_tasks"), \
             patch.object(service, "_invalidate_context_cache"), \
             patch.object(service, "_get_org_pii_config", return_value={}):
            result = await service.ingest(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                session_external_id=None,
                messages=self._sample_messages(),
            )
        assert result.status == "accepted"
        assert mock_webhook.emit.await_count == 2
        mock_webhook.emit.assert_any_await(
            organization_id=self.ORG_ID,
            event_type=EventType.INGEST_BATCH_COMPLETED,
            payload=ANY,
        )
        mock_webhook.emit.assert_any_await(
            organization_id=self.ORG_ID,
            event_type=EventType.MESSAGE_ADDED,
            payload=ANY,
        )

    @pytest.mark.asyncio
    async def test_ingest_survives_webhook_emit_failure(
        self,
        service: MemoryService,
    ) -> None:
        """A webhook emit failure never fails an already-committed ingest.

        Regression test for the live incident: ``emit()`` used to propagate
        DB/enqueue errors, 500ing an ingest that had already committed
        (``emit`` runs after ``db.commit()``).  ``emit()`` now absorbs
        fan-out failures (logs ``webhook.emit_failed``, counts, returns
        False) — so ingest must still return ``accepted`` even when the
        endpoint lookup inside the real ``emit`` raises.
        """
        webhook_repo = AsyncMock()
        webhook_repo.get_active_endpoints_for_event.side_effect = RuntimeError(
            "db unavailable",
        )
        service._webhook_service = WebhookService(repo=webhook_repo)

        service._session_repo.get_or_create_default.return_value = MagicMock(
            id=uuid4(),
            external_id="__default__",
        )

        with (
            patch.object(service, "_enqueue_arq_tasks"),
            patch.object(service, "_invalidate_context_cache"),
            patch.object(service, "_get_org_pii_config", return_value={}),
        ):
            result = await service.ingest(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                session_external_id=None,
                messages=self._sample_messages(),
            )
        assert isinstance(result, IngestMemoryResponse)
        assert result.status == "accepted"
        assert result.job_id is not None

    # ------------------------------------------------------------------
    # Blob processing during ingest
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_ingest_with_blob_processing(self, service: MemoryService) -> None:
        """Ingest processes blobs when uploaded_blobs is provided."""
        service._session_repo.get_or_create_default.return_value = MagicMock(
            id=uuid4(), external_id="__default__",
        )
        mock_blob_record = MagicMock()
        mock_blob_record.id = uuid4()
        mock_blob_record.storage_key = "test/key"
        mock_blob_record.mime_type = "text/plain"
        mock_blob_record.file_name = "test.txt"
        mock_blob_record.episode_id = uuid4()

        with (
            patch.object(service, "_enqueue_arq_tasks"),
            patch.object(service, "_invalidate_context_cache"),
            patch.object(service, "_get_org_pii_config", return_value={}),
            patch.object(
                service, "_process_blobs", return_value=(1, [mock_blob_record])
            ),
            patch.object(service, "_enqueue_blob_extraction_tasks"),
        ):
            result = await service.ingest(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                session_external_id=None,
                messages=self._sample_messages(),
                uploaded_blobs=[MagicMock(spec=UploadFile)],
            )
        assert result.status == "accepted"
        assert result.blob_count == 1

    # ------------------------------------------------------------------
    # Ingest without idempotency key
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_ingest_without_idempotency_key(self, service: MemoryService) -> None:
        """Ingest succeeds when idempotency_key is None (no Redis check)."""
        service._session_repo.get_or_create_default.return_value = MagicMock(
            id=uuid4(), external_id="__default__",
        )
        with patch.object(service, "_enqueue_arq_tasks"), \
             patch.object(service, "_invalidate_context_cache"), \
             patch.object(service, "_get_org_pii_config", return_value={}):
            result = await service.ingest(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                session_external_id=None,
                messages=self._sample_messages(),
            )
        assert result.status == "accepted"
        # key is None → idempotency check must be skipped entirely
        service._idem.check_idempotency_key.assert_not_called()
        service._idem.store_idempotency_key.assert_not_called()
        assert result.job_id is not None
        # DB claim (Step 4) is the authoritative dedup arbiter — the claim
        # must be awaited on every accepted ingest.
        service._dedup_repo.insert_or_none.assert_awaited_once()

    # ------------------------------------------------------------------
    # Blob extraction tasks are enqueued after blob processing
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_ingest_enqueues_blob_extraction_tasks(
        self, service: MemoryService,
    ) -> None:
        """Ingest enqueues blob extraction tasks when blobs are processed."""
        service._session_repo.get_or_create_default.return_value = MagicMock(
            id=uuid4(), external_id="__default__",
        )
        mock_blob = MagicMock()
        mock_blob.id = uuid4()

        with (
            patch.object(service, "_enqueue_arq_tasks"),
            patch.object(service, "_invalidate_context_cache"),
            patch.object(service, "_get_org_pii_config", return_value={}),
            patch.object(
                service,
                "_process_blobs",
                return_value=(2, [mock_blob, mock_blob]),
            ),
            patch.object(service, "_enqueue_blob_extraction_tasks") as mock_extract,
        ):
            result = await service.ingest(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                session_external_id=None,
                messages=self._sample_messages(),
                uploaded_blobs=[MagicMock(spec=UploadFile)],
            )
        assert result.status == "accepted"
        mock_extract.assert_awaited_once()


@pytest.mark.unit
class TestMemoryServiceInternal:
    """Tests for MemoryService private/internal methods."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")

    @pytest.fixture
    def service(self) -> MemoryService:
        mock_db = AsyncMock(spec=AsyncSession)
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_redis.set.return_value = True
        mock_redis.setex.return_value = True
        mock_redis.scan.return_value = (0, [])
        mock_redis.delete.return_value = 0
        mock_episode_repo = AsyncMock()
        mock_episode_repo.batch_create.return_value = []
        mock_session_repo = AsyncMock()
        mock_user_repo = AsyncMock()
        mock_fact_repo = AsyncMock()
        mock_org_repo = AsyncMock()

        return MemoryService(
            db=mock_db,
            redis_client=mock_redis,
            episode_repo=mock_episode_repo,
            session_repo=mock_session_repo,
            user_repo=mock_user_repo,
            fact_repo=mock_fact_repo,
            org_repo=mock_org_repo,
            idempotency_service=_mock_idempotency_service(),
            dedup_repo=AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_get_org_pii_config(self, service: MemoryService) -> None:
        """_get_org_pii_config delegates to org_repo.get_pii_config."""
        expected = {"mode": "mask", "patterns": ["email", "phone"]}
        service._org_repo.get_pii_config.return_value = expected
        result = await service._get_org_pii_config(self.ORG_ID)
        assert result == expected
        service._org_repo.get_pii_config.assert_awaited_once_with(self.ORG_ID)

    # ── _invalidate_context_cache ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_invalidate_context_cache_with_keys(
        self, service: MemoryService,
    ) -> None:
        """_invalidate_context_cache scans and deletes matching Redis keys."""
        service._redis.scan.return_value = (0, ["key1", "key2"])
        service._redis.delete.return_value = 2
        await service._invalidate_context_cache(
            str(self.ORG_ID), str(self.PROJECT_ID),
        )
        service._redis.scan.assert_awaited_once()
        service._redis.delete.assert_awaited_once_with("key1", "key2")

    @pytest.mark.asyncio
    async def test_invalidate_context_cache_empty(
        self, service: MemoryService,
    ) -> None:
        """_invalidate_context_cache does nothing when no keys match."""
        await service._invalidate_context_cache(
            str(self.ORG_ID), str(self.PROJECT_ID),
        )
        service._redis.scan.assert_awaited_once()
        service._redis.delete.assert_not_called()


@pytest.mark.unit
class TestMemoryServiceInfrastructure:
    """Tests for infrastructure-heavy private methods with patched internals."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")

    @pytest.fixture
    def service(self) -> MemoryService:
        mock_db = AsyncMock(spec=AsyncSession)
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_redis.set.return_value = True
        mock_redis.setex.return_value = True
        mock_redis.scan.return_value = (0, [])
        mock_redis.delete.return_value = 0
        mock_episode_repo = AsyncMock()
        mock_episode_repo.batch_create.return_value = []
        mock_session_repo = AsyncMock()
        mock_user_repo = AsyncMock()
        mock_fact_repo = AsyncMock()
        mock_org_repo = AsyncMock()

        return MemoryService(
            db=mock_db,
            redis_client=mock_redis,
            episode_repo=mock_episode_repo,
            session_repo=mock_session_repo,
            user_repo=mock_user_repo,
            fact_repo=mock_fact_repo,
            org_repo=mock_org_repo,
            idempotency_service=_mock_idempotency_service(),
            dedup_repo=AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_enqueue_arq_tasks(self, service: MemoryService) -> None:
        """_enqueue_arq_tasks enqueues enrichment/embedding/linking for each episode."""
        mock_pool = AsyncMock()
        mock_pool.enqueue = AsyncMock()

        with patch("services.memory_service.get_arq", return_value=mock_pool):
            await service._enqueue_arq_tasks(
                job_id="job-1",
                org_id=str(self.ORG_ID),
                project_id=str(self.PROJECT_ID),
                session_id=str(uuid4()),
                episodes=[
                    {"id": uuid4(), "content": "Hello", "role": "user", "metadata": {}},
                    {
                        "id": uuid4(),
                        "content": "World",
                        "role": "assistant",
                        "metadata": {},
                    },
                ],
            )

        assert mock_pool.enqueue.await_count == 6  # 3 tasks × 2 episodes

    @pytest.mark.asyncio
    async def test_enqueue_arq_tasks_pool_unavailable(
        self, service: MemoryService,
    ) -> None:
        """_enqueue_arq_tasks re-raises when ARQ pool is unavailable."""
        with (
            patch(
                "services.memory_service.get_arq",
                side_effect=ConnectionError("ARQ down"),
            ),
            pytest.raises(ConnectionError),
        ):
            await service._enqueue_arq_tasks(
                job_id="job-2",
                org_id=str(self.ORG_ID),
                project_id=str(self.PROJECT_ID),
                session_id=str(uuid4()),
                episodes=[
                    {
                        "id": uuid4(),
                        "content": "Test",
                        "role": "user",
                        "metadata": {},
                    }
                ],
            )

    @pytest.mark.asyncio
    async def test_enqueue_blob_extraction_tasks(
        self, service: MemoryService,
    ) -> None:
        """_enqueue_blob_extraction_tasks enqueues extraction for each blob."""
        mock_pool = AsyncMock()
        mock_pool.enqueue = AsyncMock()
        blob = MagicMock()
        blob.id = uuid4()
        blob.episode_id = uuid4()
        blob.storage_key = "blobs/doc.pdf"
        blob.mime_type = "application/pdf"
        blob.file_name = "doc.pdf"

        with patch("services.memory_service.get_arq", return_value=mock_pool):
            await service._enqueue_blob_extraction_tasks(
                blob_records=[blob, blob],
                org_id=str(self.ORG_ID),
                project_id=str(self.PROJECT_ID),
            )

        assert mock_pool.enqueue.await_count == 2

    @pytest.mark.asyncio
    async def test_process_blobs_empty(self, service: MemoryService) -> None:
        """_process_blobs returns early when no messages have blobs."""
        count, records = await service._process_blobs(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            session_id=uuid4(),
            created_by=self.USER_ID,
            episodes=[MagicMock(id=uuid4()), MagicMock(id=uuid4())],
            messages=[
                Message(role="user", content="No blobs here", blobs=[]),
                Message(role="assistant", content="No blobs either"),
            ],
            uploaded_blobs=[],
        )
        assert count == 0
        assert records == []

    @pytest.mark.asyncio
    async def test_ingest_with_idempotency_key(
        self, service: MemoryService,
    ) -> None:
        """Happy path with key: stores idempotency entry and content hash."""
        service._session_repo.get_or_create_default.return_value = MagicMock(
            id=uuid4(), external_id="__default__",
        )
        service._episode_repo.batch_create.return_value = [
            MagicMock(id=uuid4(), content="msg"),
        ]

        with (
            patch.object(service, "_enqueue_arq_tasks"),
            patch.object(service, "_invalidate_context_cache"),
            patch.object(service, "_get_org_pii_config", return_value={}),
        ):
            result = await service.ingest(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                session_external_id=None,
                idempotency_key="my-key",
                messages=[Message(role="user", content="Test")],
            )

        assert result.status == "accepted"
        # Step 1: key check ran (NEW → proceed)
        service._idem.check_idempotency_key.assert_awaited_once_with(
            "my-key", "", str(self.ORG_ID)
        )
        # Step 9: response cached under the key, content hash stored with payload
        service._idem.store_idempotency_key.assert_awaited_once_with(
            "my-key", "", ANY, str(self.ORG_ID),
        )
        service._idem.store_content_hash.assert_awaited_once()
        _args, kwargs = service._idem.store_content_hash.call_args
        assert kwargs["payload"] == result.job_id

    @pytest.mark.asyncio
    async def test_resolve_session_with_uuid_fallback(
        self, service: MemoryService,
    ) -> None:
        """_resolve_session falls back to UUID lookup for valid UUID strings."""
        session_id = uuid4()
        mock_session = MagicMock(id=session_id, external_id="ext-1")
        service._session_repo.get_by_external_id.return_value = None
        service._session_repo.get_by_uuid.return_value = mock_session

        result = await service._resolve_session(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            session_external_id=str(session_id),
            created_by=self.USER_ID,
        )

        assert result == mock_session
        service._session_repo.get_by_uuid.assert_awaited_once_with(
            org_id=self.ORG_ID, session_id=session_id, project_id=self.PROJECT_ID,
        )

    @pytest.mark.asyncio
    async def test_resolve_session_uuid_not_found_raises(
        self, service: MemoryService,
    ) -> None:
        """_resolve_session raises NotFoundError when session UUID lookup fails."""
        session_id = uuid4()
        service._session_repo.get_by_external_id.return_value = None
        service._session_repo.get_by_uuid.return_value = None

        with pytest.raises(NotFoundError, match="not found"):
            await service._resolve_session(
                organization_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                session_external_id=str(session_id),
                created_by=self.USER_ID,
            )
