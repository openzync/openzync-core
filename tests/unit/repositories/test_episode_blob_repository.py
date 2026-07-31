"""Unit tests for EpisodeBlobRepository — blob CRUD for episodes."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.episode_blob_repository import EpisodeBlobRepository


pytestmark = pytest.mark.unit

FIXED_NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


class TestEpisodeBlobRepository:
    """EpisodeBlobRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
    SESSION_ID = UUID("00000000-0000-0000-0000-000000000003")
    EPISODE_ID = UUID("00000000-0000-0000-0000-000000000010")
    BLOB_ID = UUID("00000000-0000-0000-0000-000000000020")
    USER_ID = UUID("00000000-0000-0000-0000-000000000030")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def repo(self, mock_db: AsyncMock) -> EpisodeBlobRepository:
        return EpisodeBlobRepository(db=mock_db)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _mock_blob(self, **overrides: object) -> MagicMock:
        blob = MagicMock()
        blob.id = overrides.get("id", self.BLOB_ID)
        blob.organization_id = overrides.get("organization_id", self.ORG_ID)
        blob.project_id = overrides.get("project_id", self.PROJECT_ID)
        blob.session_id = overrides.get("session_id", self.SESSION_ID)
        blob.episode_id = overrides.get("episode_id", self.EPISODE_ID)
        blob.created_by = overrides.get("created_by", self.USER_ID)
        blob.storage_backend = overrides.get("storage_backend", "s3")
        blob.storage_key = overrides.get("storage_key", "key-123")
        blob.file_name = overrides.get("file_name", "photo.jpg")
        blob.mime_type = overrides.get("mime_type", "image/jpeg")
        blob.file_size = overrides.get("file_size", 1024)
        blob.content_hash = overrides.get("content_hash", "hash123")
        blob.extracted_text = overrides.get("extracted_text", None)
        blob.blob_index = overrides.get("blob_index", 0)
        blob.width = overrides.get("width", None)
        blob.height = overrides.get("height", None)
        return blob

    # ── batch_create ───────────────────────────────────────────────────────────

    async def test_batch_create_empty_returns_empty(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """batch_create returns empty list when no blobs provided."""
        result = await repo.batch_create(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            session_id=self.SESSION_ID,
            episode_id=self.EPISODE_ID,
            created_by=self.USER_ID,
            blobs=[],
        )

        assert result == []
        mock_db.execute.assert_not_called()

    async def test_batch_create_inserts_blobs(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """batch_create inserts blob records and returns ORM instances."""
        blobs_data = [
            {
                "storage_backend": "s3",
                "storage_key": "key-1",
                "file_name": "img1.jpg",
                "mime_type": "image/jpeg",
                "file_size": 1024,
                "content_hash": "hash1",
                "blob_index": 0,
            },
            {
                "storage_backend": "s3",
                "storage_key": "key-2",
                "file_name": "img2.jpg",
                "mime_type": "image/png",
                "file_size": 2048,
                "content_hash": "hash2",
                "blob_index": 1,
            },
        ]
        mock_db.execute.return_value = MagicMock()
        mock_db.execute.return_value.fetchall.return_value = [
            MagicMock(_mapping={
                "id": uuid4(),
                "organization_id": self.ORG_ID,
                "project_id": self.PROJECT_ID,
                "session_id": self.SESSION_ID,
                "episode_id": self.EPISODE_ID,
                "created_by": self.USER_ID,
                "storage_backend": "s3",
                "storage_key": "key-1",
                "file_name": "img1.jpg",
                "mime_type": "image/jpeg",
                "file_size": 1024,
                "content_hash": "hash1",
                "width": None,
                "height": None,
                "extracted_text": None,
                "blob_index": 0,
                "created_at": FIXED_NOW,
                "updated_at": FIXED_NOW,
            }),
            MagicMock(_mapping={
                "id": uuid4(),
                "organization_id": self.ORG_ID,
                "project_id": self.PROJECT_ID,
                "session_id": self.SESSION_ID,
                "episode_id": self.EPISODE_ID,
                "created_by": self.USER_ID,
                "storage_backend": "s3",
                "storage_key": "key-2",
                "file_name": "img2.jpg",
                "mime_type": "image/png",
                "file_size": 2048,
                "content_hash": "hash2",
                "width": None,
                "height": None,
                "extracted_text": None,
                "blob_index": 1,
                "created_at": FIXED_NOW,
                "updated_at": FIXED_NOW,
            }),
        ]

        result = await repo.batch_create(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            session_id=self.SESSION_ID,
            episode_id=self.EPISODE_ID,
            created_by=self.USER_ID,
            blobs=blobs_data,
        )

        assert len(result) == 2
        mock_db.execute.assert_awaited_once()
        # RETURNING now maps created_at/updated_at onto returned ORM objects
        assert result[0].created_at == FIXED_NOW
        assert result[0].updated_at == FIXED_NOW

    # ── get_by_episode ─────────────────────────────────────────────────────────

    async def test_get_by_episode(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_episode returns blobs for an episode."""
        blobs = [self._mock_blob(), self._mock_blob()]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = blobs
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_episode(episode_id=self.EPISODE_ID)

        assert result == blobs

    async def test_get_by_episode_empty(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_episode returns empty list when no blobs."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_episode(episode_id=self.EPISODE_ID)

        assert result == []

    # ── get_by_session ─────────────────────────────────────────────────────────

    async def test_get_by_session(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_session returns blobs for a session."""
        blobs = [self._mock_blob()]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = blobs
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_session(session_id=self.SESSION_ID)

        assert result == blobs

    # ── get_by_id ──────────────────────────────────────────────────────────────

    async def test_get_by_id_found(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id returns blob when found."""
        blob = self._mock_blob()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = blob
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id(blob_id=self.BLOB_ID)

        assert result == blob

    async def test_get_by_id_not_found(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id returns None when not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id(blob_id=self.BLOB_ID)

        assert result is None

    # ── get_by_content_hash ────────────────────────────────────────────────────

    async def test_get_by_content_hash(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_content_hash returns blobs with matching hash."""
        blobs = [self._mock_blob(content_hash="abc123")]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = blobs
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_content_hash(
            organization_id=self.ORG_ID, content_hash="abc123"
        )

        assert result == blobs

    async def test_get_by_content_hash_empty(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_content_hash returns empty list when no match."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_content_hash(
            organization_id=self.ORG_ID, content_hash="nonexistent"
        )

        assert result == []

    # ── update_extracted_text ──────────────────────────────────────────────────

    async def test_update_extracted_text(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """update_extracted_text updates and returns the blob."""
        blob = self._mock_blob(extracted_text=None)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = blob
        mock_db.execute.return_value = mock_result

        result = await repo.update_extracted_text(
            blob_id=self.BLOB_ID, extracted_text="Extracted content"
        )

        assert result is not None
        mock_db.flush.assert_awaited_once()

    async def test_update_extracted_text_not_found(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """update_extracted_text returns None when blob not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.update_extracted_text(
            blob_id=self.BLOB_ID, extracted_text="content"
        )

        assert result is None

    # ── count_by_episode ───────────────────────────────────────────────────────

    async def test_count_by_episode(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """count_by_episode returns the blob count."""
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 3
        mock_db.execute.return_value = mock_result

        count = await repo.count_by_episode(episode_id=self.EPISODE_ID)

        assert count == 3

    async def test_count_by_episode_zero(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """count_by_episode returns 0 when no blobs."""
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        mock_db.execute.return_value = mock_result

        count = await repo.count_by_episode(episode_id=self.EPISODE_ID)

        assert count == 0

    # ── get_orphaned_blobs ─────────────────────────────────────────────────────

    async def test_get_orphaned_blobs(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """get_orphaned_blobs returns blobs with soft-deleted episodes."""
        blobs = [self._mock_blob()]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = blobs
        mock_db.execute.return_value = mock_result

        result = await repo.get_orphaned_blobs(
            organization_id=self.ORG_ID, limit=100
        )

        assert result == blobs

    async def test_get_orphaned_blobs_empty(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """get_orphaned_blobs returns empty when no orphans."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.get_orphaned_blobs(
            organization_id=self.ORG_ID, limit=100
        )

        assert result == []

    # ── delete_by_episode ──────────────────────────────────────────────────────

    async def test_delete_by_episode(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """delete_by_episode removes and returns blobs for an episode."""
        blobs = [self._mock_blob(), self._mock_blob()]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = blobs
        mock_db.execute.return_value = mock_result
        mock_db.delete.return_value = None

        result = await repo.delete_by_episode(episode_id=self.EPISODE_ID)

        assert result == blobs
        assert mock_db.delete.await_count == 2
        mock_db.flush.assert_awaited_once()

    async def test_delete_by_episode_empty(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """delete_by_episode returns empty list when no blobs."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.delete_by_episode(episode_id=self.EPISODE_ID)

        assert result == []
        mock_db.flush.assert_awaited_once()

    # ── delete_by_ids ──────────────────────────────────────────────────────────

    async def test_delete_by_ids(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """delete_by_ids removes and returns blobs by their IDs."""
        blobs = [self._mock_blob()]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = blobs
        mock_db.execute.return_value = mock_result

        result = await repo.delete_by_ids(blob_ids=[self.BLOB_ID])

        assert result == blobs
        mock_db.flush.assert_awaited_once()

    async def test_delete_by_ids_empty(
        self, repo: EpisodeBlobRepository, mock_db: AsyncMock
    ) -> None:
        """delete_by_ids returns empty when no blobs match."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.delete_by_ids(blob_ids=[])

        assert result == []
        mock_db.flush.assert_awaited_once()
