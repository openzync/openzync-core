"""Unit tests for merge_duplicate_entities task."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

_ORG_ID = str(uuid4())


@pytest.mark.unit
class TestMergeDuplicateEntities:
    """merge_duplicate_entities task tests."""

    def _make_db(self, org_ids: list | None = None, project_ids: list | None = None) -> AsyncMock:
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        org_id_tuples = [(oid,) for oid in (org_ids or [uuid4()])]
        proj_id_tuples = [(pid,) for pid in (project_ids or [uuid4()])]
        org_result = MagicMock()
        org_result.all.return_value = org_id_tuples
        project_result = MagicMock()
        project_result.all.return_value = proj_id_tuples
        db.execute.side_effect = [org_result, project_result]
        return db

    def _make_entities(self, count: int = 3, names: list[str] | None = None) -> list[dict]:
        if names is None:
            names = [f"Entity_{i}" for i in range(count)]
        now = datetime.now(timezone.utc).isoformat()
        return [
            {
                "id": str(uuid4()),
                "name": names[i] if i < len(names) else f"Entity_{i}",
                "entity_type": "test_type",
                "created_at": now,
            }
            for i in range(count)
        ]

    def _factory(self, db: AsyncMock) -> MagicMock:
        f = MagicMock()
        f.return_value = db
        return f

    def _ctx(self, db: AsyncMock) -> dict:
        return {"db_engine": MagicMock(), "db_session_factory": self._factory(db)}

    @pytest.mark.asyncio
    async def test_no_eligible_orgs(self) -> None:
        """No organizations exist → returns skipped."""
        with patch("workers.tasks.merge_duplicate_entities.with_retry", lambda **kw: lambda f: f):
            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None
            org_result = MagicMock()
            org_result.all.return_value = []
            db.execute.return_value = org_result

            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            result = await merge_duplicate_entities(ctx=self._ctx(db))

            assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_no_duplicates(self) -> None:
        """Entity dedup with no duplicates → no-op."""
        mock_backend = AsyncMock()
        mock_backend.get_all_entities.return_value = self._make_entities(3)
        mock_backend.bulk_search_entities.return_value = []

        with (
            patch("workers.tasks.merge_duplicate_entities.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.merge_duplicate_entities.resolve_graph_backend", return_value=mock_backend),
            patch("asyncio.sleep", AsyncMock()),
        ):
            db = self._make_db()
            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            result = await merge_duplicate_entities(ctx=self._ctx(db))

            assert result["status"] == "completed"
            assert result["clusters_merged"] == 0

    @pytest.mark.asyncio
    async def test_duplicates_merged(self) -> None:
        """Duplicate entities merged correctly."""
        mock_backend = AsyncMock()
        entities = self._make_entities(names=["DuplicateName", "DuplicateName"])
        mock_backend.get_all_entities.return_value = entities
        mock_backend.bulk_search_entities.return_value = []
        mock_backend.merge_entities.return_value = {
            "rewired_count": 2, "deleted_count": 1, "merged_count": 1,
        }

        with (
            patch("workers.tasks.merge_duplicate_entities.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.merge_duplicate_entities.resolve_graph_backend", return_value=mock_backend),
            patch("services.audit_log_service.AuditLogService") as mock_audit_cls,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_audit = AsyncMock()
            mock_audit_cls.return_value = mock_audit
            db = self._make_db()
            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            result = await merge_duplicate_entities(ctx=self._ctx(db))

            assert result["status"] == "completed"
            assert result["clusters_merged"] >= 1
            assert mock_backend.merge_entities.called
            mock_audit.log_action.assert_called_once()

    @pytest.mark.asyncio
    async def test_single_org_no_duplicates(self) -> None:
        """Entity with no duplicates → returns early."""
        mock_backend = AsyncMock()
        mock_backend.get_all_entities.return_value = self._make_entities(1)

        with (
            patch("workers.tasks.merge_duplicate_entities.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.merge_duplicate_entities.resolve_graph_backend", return_value=mock_backend),
            patch("asyncio.sleep", AsyncMock()),
        ):
            db = self._make_db()
            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            result = await merge_duplicate_entities(ctx=self._ctx(db), org_id=_ORG_ID)

            assert result["status"] == "completed"
            assert result["clusters_merged"] == 0

    @pytest.mark.asyncio
    async def test_backend_unavailable(self) -> None:
        """Graph backend unavailable → gracefully skips."""
        with (
            patch("workers.tasks.merge_duplicate_entities.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.merge_duplicate_entities.resolve_graph_backend", return_value=None),
        ):
            db = self._make_db()
            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            result = await merge_duplicate_entities(ctx=self._ctx(db), org_id=_ORG_ID)

            assert result["status"] == "completed"
            assert result["clusters_merged"] == 0

    @pytest.mark.asyncio
    async def test_all_orgs_fail(self) -> None:
        """All orgs fail → raises RuntimeError."""
        with (
            patch("workers.tasks.merge_duplicate_entities.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.merge_duplicate_entities.resolve_graph_backend", side_effect=Exception("Backend down")),
        ):
            db = self._make_db()
            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            with pytest.raises(RuntimeError, match="All .* orgs failed"):
                await merge_duplicate_entities(ctx=self._ctx(db))

    @pytest.mark.asyncio
    async def test_db_error_propagates(self) -> None:
        """Database error during org query propagates."""
        with patch("workers.tasks.merge_duplicate_entities.with_retry", lambda **kw: lambda f: f):
            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None
            db.execute.side_effect = Exception("DB error")

            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            with pytest.raises(Exception):
                await merge_duplicate_entities(ctx=self._ctx(db))

    @pytest.mark.asyncio
    async def test_transitive_relationships(self) -> None:
        """Merge with transitive relationships handles correctly."""
        mock_backend = AsyncMock()
        entities = self._make_entities(names=["Alpha", "Alpha", "Beta", "Beta"])
        mock_backend.get_all_entities.return_value = entities
        mock_backend.bulk_search_entities.return_value = []
        mock_backend.merge_entities.return_value = {
            "rewired_count": 3, "deleted_count": 2, "merged_count": 2,
        }

        with (
            patch("workers.tasks.merge_duplicate_entities.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.merge_duplicate_entities.resolve_graph_backend", return_value=mock_backend),
            patch("services.audit_log_service.AuditLogService") as mock_audit_cls,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_audit = AsyncMock()
            mock_audit_cls.return_value = mock_audit
            db = self._make_db()
            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            result = await merge_duplicate_entities(ctx=self._ctx(db))

            assert result["status"] == "completed"
            assert result["clusters_merged"] >= 1
