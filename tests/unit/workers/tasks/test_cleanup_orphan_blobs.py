"""Unit tests for cleanup_orphan_blobs task."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())
_EPISODE_ID = str(uuid4())


@pytest.mark.unit
class TestCleanupOrphanBlobs:
    """cleanup_orphan_blobs task tests."""

    def _make_blob(self, blob_id: str | None = None) -> MagicMock:
        blob = MagicMock()
        blob.id = blob_id or str(uuid4())
        blob.key = f"blobs/{blob_id or uuid4()}"
        blob.episode_id = _EPISODE_ID
        return blob

    def _make_session_factory(self, db: AsyncMock | None = None) -> MagicMock:
        if db is None:
            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None
        factory = MagicMock()
        factory.return_value = db
        return factory

    def _ctx(self, db: AsyncMock) -> dict:
        return {"db_engine": MagicMock(), "db_session_factory": self._make_session_factory(db)}

    @pytest.mark.asyncio
    async def test_orphans_detected_and_deleted(self) -> None:
        """Orphaned blobs detected and deleted from S3."""
        blobs = [self._make_blob() for _ in range(3)]

        with (
            patch("workers.tasks.cleanup_orphan_blobs.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("services.blob_storage_service.BlobStorageService") as mock_svc_cls,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_repo = AsyncMock()
            mock_repo.get_orphaned_blobs.return_value = blobs
            mock_repo.delete_by_ids.return_value = blobs
            mock_repo_cls.return_value = mock_repo

            mock_svc = AsyncMock()
            mock_svc.delete_blobs = AsyncMock()
            mock_svc_cls.return_value = mock_svc

            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None

            from workers.tasks.cleanup_orphan_blobs import cleanup_orphan_blobs

            result = await cleanup_orphan_blobs(ctx=self._ctx(db), org_id=_ORG_ID)

            assert result == len(blobs)
            mock_svc.delete_blobs.assert_called_once()
            mock_repo.delete_by_ids.assert_called_once()
            db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_orphans(self) -> None:
        """No orphaned blobs → no-op returns 0."""
        with (
            patch("workers.tasks.cleanup_orphan_blobs.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
        ):
            mock_repo = AsyncMock()
            mock_repo.get_orphaned_blobs.return_value = []
            mock_repo_cls.return_value = mock_repo

            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None

            from workers.tasks.cleanup_orphan_blobs import cleanup_orphan_blobs

            result = await cleanup_orphan_blobs(ctx=self._ctx(db), org_id=_ORG_ID)

            assert result == 0

    @pytest.mark.asyncio
    async def test_specific_episode(self) -> None:
        """Specific episode scoping works."""
        blobs = [self._make_blob()]

        with (
            patch("workers.tasks.cleanup_orphan_blobs.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("services.blob_storage_service.BlobStorageService") as mock_svc_cls,
        ):
            mock_repo = AsyncMock()
            mock_repo.get_by_episode.return_value = blobs
            mock_repo.delete_by_ids.return_value = blobs
            mock_repo_cls.return_value = mock_repo

            mock_svc = AsyncMock()
            mock_svc_cls.return_value = mock_svc

            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None

            from workers.tasks.cleanup_orphan_blobs import cleanup_orphan_blobs

            result = await cleanup_orphan_blobs(
                ctx=self._ctx(db), org_id=_ORG_ID, episode_id=_EPISODE_ID,
            )

            assert result == 1
            mock_repo.get_by_episode.assert_called_once()

    @pytest.mark.asyncio
    async def test_s3_deletion_failure_logged(self) -> None:
        """S3 deletion failure propagates (with_retry handles retries)."""
        blobs = [self._make_blob()]

        with (
            patch("workers.tasks.cleanup_orphan_blobs.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("services.blob_storage_service.BlobStorageService") as mock_svc_cls,
        ):
            mock_repo = AsyncMock()
            mock_repo.get_orphaned_blobs.return_value = blobs
            mock_repo.delete_by_ids.return_value = blobs
            mock_repo_cls.return_value = mock_repo

            mock_svc = AsyncMock()
            mock_svc.delete_blobs.side_effect = Exception("S3 unavailable")
            mock_svc_cls.return_value = mock_svc

            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None

            from workers.tasks.cleanup_orphan_blobs import cleanup_orphan_blobs

            with pytest.raises(Exception, match="S3 unavailable"):
                await cleanup_orphan_blobs(ctx=self._ctx(db), org_id=_ORG_ID)

    @pytest.mark.asyncio
    async def test_blobs_refrenced_by_episodes_not_deleted(self) -> None:
        """Blobs referenced by non-deleted episodes are not returned as orphans."""
        with (
            patch("workers.tasks.cleanup_orphan_blobs.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
        ):
            mock_repo = AsyncMock()
            mock_repo.get_orphaned_blobs.return_value = []
            mock_repo_cls.return_value = mock_repo

            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None

            from workers.tasks.cleanup_orphan_blobs import cleanup_orphan_blobs

            result = await cleanup_orphan_blobs(ctx=self._ctx(db), org_id=_ORG_ID)

            assert result == 0
            mock_repo.get_orphaned_blobs.assert_called_once()

    @pytest.mark.asyncio
    async def test_db_error_propagates(self) -> None:
        """Database errors are not silently swallowed."""
        with patch("workers.tasks.cleanup_orphan_blobs.with_retry", lambda **kw: lambda f: f):
            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None
            db.execute.side_effect = Exception("DB error")

            from workers.tasks.cleanup_orphan_blobs import cleanup_orphan_blobs

            with pytest.raises(Exception):
                await cleanup_orphan_blobs(ctx=self._ctx(db), org_id=_ORG_ID)

    # ── Coverage gap: trace_id, engine, bao_client paths ───────────────────

    @pytest.mark.asyncio
    async def test_with_trace_id(self) -> None:
        """Trace ID is bound to logging context."""
        blobs = [self._make_blob()]

        with (
            patch("workers.tasks.cleanup_orphan_blobs.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("services.blob_storage_service.BlobStorageService") as mock_svc_cls,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_repo = AsyncMock()
            mock_repo.get_orphaned_blobs.return_value = blobs
            mock_repo.delete_by_ids.return_value = blobs
            mock_repo_cls.return_value = mock_repo

            mock_svc = AsyncMock()
            mock_svc_cls.return_value = mock_svc

            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None

            from workers.tasks.cleanup_orphan_blobs import cleanup_orphan_blobs

            result = await cleanup_orphan_blobs(
                ctx=self._ctx(db), org_id=_ORG_ID, trace_id="test-trace-001",
            )

            assert result == len(blobs)

    @pytest.mark.asyncio
    async def test_no_bao_client_uses_bootstrap(self) -> None:
        """No openbao_client → BootstrapSettings + OpenBaoClient succeed.

        Covers the else branch for bao_client (lines 126-137) where
        BootstrapSettings and OpenBaoClient are used to fetch org config.
        """
        blobs = [self._make_blob()]

        with (
            patch("workers.tasks.cleanup_orphan_blobs.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("services.blob_storage_service.BlobStorageService") as mock_svc_cls,
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("core.config.BootstrapSettings") as mock_bs,
            patch("core.openbao.OpenBaoClient") as mock_bao_cls,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_repo = AsyncMock()
            mock_repo.get_orphaned_blobs.return_value = blobs
            mock_repo.delete_by_ids.return_value = blobs
            mock_repo_cls.return_value = mock_repo

            mock_svc = AsyncMock()
            mock_svc_cls.return_value = mock_svc

            org_cfg = MagicMock()
            org_cfg.to_blob_storage_config.return_value = {"bucket_name": "custom-bucket"}
            mock_cfg.return_value = org_cfg

            mock_bao = MagicMock()
            mock_bao.__aenter__ = AsyncMock(return_value=mock_bao)
            mock_bao.__aexit__ = AsyncMock(return_value=None)
            mock_bao_cls.return_value = mock_bao

            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None

            # ctx WITHOUT openbao_client → triggers else branch
            ctx = {
                "db_engine": MagicMock(),
                "db_session_factory": self._make_session_factory(db),
            }

            from workers.tasks.cleanup_orphan_blobs import cleanup_orphan_blobs

            result = await cleanup_orphan_blobs(
                ctx=ctx, org_id=_ORG_ID,
            )

            assert result == len(blobs)
            mock_bs.assert_called_once()
            mock_bao_cls.assert_called_once()
            mock_cfg.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_db_engine_in_ctx(self) -> None:
        """Missing db_engine → creates own engine and disposes."""
        blobs = [self._make_blob()]

        with (
            patch("workers.tasks.cleanup_orphan_blobs.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("services.blob_storage_service.BlobStorageService") as mock_svc_cls,
            patch("core.db.init_db_engine") as mock_init_engine,
            patch("core.db.get_async_session") as mock_get_session,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_engine = AsyncMock()
            mock_engine.dispose = AsyncMock()
            mock_init_engine.return_value = mock_engine
            mock_session_factory = MagicMock()
            mock_get_session.return_value = mock_session_factory

            mock_repo = AsyncMock()
            mock_repo.get_orphaned_blobs.return_value = blobs
            mock_repo.delete_by_ids.return_value = blobs
            mock_repo_cls.return_value = mock_repo

            mock_svc = AsyncMock()
            mock_svc_cls.return_value = mock_svc

            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None
            mock_session_factory.return_value = db

            from workers.tasks.cleanup_orphan_blobs import cleanup_orphan_blobs

            result = await cleanup_orphan_blobs(
                ctx={}, org_id=_ORG_ID,
            )

            assert result == len(blobs)
            mock_init_engine.assert_called_once()
            mock_engine.dispose.assert_called_once()

    @pytest.mark.asyncio
    async def test_with_openbao_client_in_ctx(self) -> None:
        """OpenBao client in ctx → used for org config fetch."""
        blobs = [self._make_blob()]

        with (
            patch("workers.tasks.cleanup_orphan_blobs.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("services.blob_storage_service.BlobStorageService") as mock_svc_cls,
            patch("core.org_config.get_org_config") as mock_cfg,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_repo = AsyncMock()
            mock_repo.get_orphaned_blobs.return_value = blobs
            mock_repo.delete_by_ids.return_value = blobs
            mock_repo_cls.return_value = mock_repo

            mock_svc = AsyncMock()
            mock_svc_cls.return_value = mock_svc

            org_cfg = MagicMock()
            org_cfg.to_blob_storage_config.return_value = {"bucket_name": "custom-bucket"}
            mock_cfg.return_value = org_cfg

            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None

            ctx = {
                "db_engine": MagicMock(),
                "db_session_factory": self._make_session_factory(db),
                "openbao_client": MagicMock(),
            }

            from workers.tasks.cleanup_orphan_blobs import cleanup_orphan_blobs

            result = await cleanup_orphan_blobs(
                ctx=ctx, org_id=_ORG_ID,
            )

            assert result == len(blobs)
            mock_cfg.assert_called_once()

    @pytest.mark.asyncio
    async def test_config_fetch_failure_continues(self) -> None:
        """Config fetch failure is logged but does not block cleanup.

        Covers the else branch (bao_client is None) in the config fetch
        try/except — BootstrapSettings / OpenBaoClient failure logged and
        cleanup proceeds with default S3 config.
        """
        blobs = [self._make_blob()]

        with (
            patch("workers.tasks.cleanup_orphan_blobs.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("services.blob_storage_service.BlobStorageService") as mock_svc_cls,
            patch("core.config.BootstrapSettings", side_effect=Exception("Bootstrap failed")),
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_repo = AsyncMock()
            mock_repo.get_orphaned_blobs.return_value = blobs
            mock_repo.delete_by_ids.return_value = blobs
            mock_repo_cls.return_value = mock_repo

            mock_svc = AsyncMock()
            mock_svc_cls.return_value = mock_svc

            # Ensure init_db_engine is mocked so it doesn't try real DB
            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None
            ctx = {
                "db_engine": MagicMock(),
                "db_session_factory": self._make_session_factory(db),
            }

            from workers.tasks.cleanup_orphan_blobs import cleanup_orphan_blobs

            result = await cleanup_orphan_blobs(
                ctx=ctx, org_id=_ORG_ID,
            )

            assert result == len(blobs)

    # ── Org-discovery (scheduled cron) path ───────────────────────────────

    @pytest.mark.asyncio
    async def test_discovery_no_orgs_returns_skipped(self) -> None:
        """No orgs in DB → cron run reports skipped and cleans nothing."""
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        # Override the auto-AsyncMock child so `result.all()` is sync.
        db.execute.return_value.all = MagicMock(return_value=[])

        with patch(
            "workers.tasks.cleanup_orphan_blobs.with_retry",
            lambda **kw: lambda f: f,
        ):
            from workers.tasks.cleanup_orphan_blobs import cleanup_orphan_blobs

            result = await cleanup_orphan_blobs(ctx=self._ctx(db))

        assert result == {
            "status": "skipped",
            "reason": "No organizations found",
            "orgs_processed": 0,
            "orgs_failed": 0,
            "blobs_cleaned": 0,
        }
        db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_discovery_multiple_orgs_partial_on_failure(self) -> None:
        """A failure in one org does not abort cleanup of the others."""
        org_1 = uuid4()
        org_2 = uuid4()
        blobs_1 = [self._make_blob() for _ in range(2)]
        blobs_2 = [self._make_blob()]

        discovery = MagicMock()
        discovery.all.return_value = [(org_1,), (org_2,)]
        other = MagicMock()
        other.all.return_value = []

        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        db.execute.side_effect = [discovery, other, other]

        with (
            patch(
                "workers.tasks.cleanup_orphan_blobs.with_retry",
                lambda **kw: lambda f: f,
            ),
            patch(
                "repositories.episode_blob_repository.EpisodeBlobRepository"
            ) as mock_repo_cls,
            patch("services.blob_storage_service.BlobStorageService") as mock_svc_cls,
        ):
            mock_repo = AsyncMock()
            mock_repo.get_orphaned_blobs.side_effect = [blobs_1, blobs_2]
            mock_repo.delete_by_ids.side_effect = [
                blobs_1, Exception("S3 unavailable"),
            ]
            mock_repo_cls.return_value = mock_repo

            mock_svc = AsyncMock()
            mock_svc_cls.return_value = mock_svc

            from workers.tasks.cleanup_orphan_blobs import cleanup_orphan_blobs

            result = await cleanup_orphan_blobs(ctx=self._ctx(db))

        assert result["status"] == "partial"
        assert result["orgs_processed"] == 2
        assert result["orgs_failed"] == 1
        assert result["blobs_cleaned"] == len(blobs_1)
        assert mock_repo.get_orphaned_blobs.call_count == 2
        db.commit.assert_called_once()
