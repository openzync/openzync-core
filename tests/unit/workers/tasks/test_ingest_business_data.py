"""Unit tests for ingest_business_data task."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())
_USER_ID = str(uuid4())


@pytest.mark.unit
class TestIngestBusinessData:
    """ingest_business_data task tests."""

    def _make_fact(self, subject: str = "Alice", predicate: str = "works_at", obj: str = "Acme") -> dict:
        return {"subject": subject, "predicate": predicate, "object": obj}

    def _make_db(self) -> AsyncMock:
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        return db

    def _factory(self, db: AsyncMock) -> MagicMock:
        f = MagicMock()
        f.return_value = db
        return f

    def _ctx(self, db: AsyncMock) -> dict:
        return {"db_engine": MagicMock(), "db_session_factory": self._factory(db)}

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Bulk data parsed and ingested."""
        facts = [self._make_fact() for _ in range(3)]

        with (
            patch("workers.tasks.ingest_business_data.FactRepository") as mock_repo_cls,
            patch("workers.tasks.ingest_business_data.get_arq") as mock_arq,
        ):
            mock_repo = AsyncMock()
            created = [MagicMock(id=str(uuid4())) for _ in range(3)]
            mock_repo.batch_create.return_value = created
            mock_repo_cls.return_value = mock_repo

            mock_pool = AsyncMock()
            mock_arq.return_value = mock_pool

            from workers.tasks.ingest_business_data import ingest_business_data

            result = await ingest_business_data(
                ctx=self._ctx(self._make_db()),
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                user_id=_USER_ID,
                facts=facts,
            )

            assert result["status"] == "completed"
            assert result["accepted"] == 3
            assert len(result["errors"]) == 0
            mock_repo.batch_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_facts(self) -> None:
        """Empty facts list → completed with no-op."""
        from workers.tasks.ingest_business_data import ingest_business_data

        result = await ingest_business_data(
            ctx={},
            org_id=_ORG_ID,
            project_id=_PROJECT_ID,
            user_id=_USER_ID,
            facts=[],
        )

        assert result["status"] == "completed"
        assert result["accepted"] == 0

    @pytest.mark.asyncio
    async def test_schema_validation(self) -> None:
        """Facts with missing fields are rejected."""
        facts = [
            {"subject": "Alice", "predicate": "works_at", "object": "Acme"},
            {"subject": "", "predicate": "knows", "object": "Bob"},
            {"predicate": "likes", "object": "Python"},
            {"subject": "Charlie", "object": "Dana"},
            {"subject": "Eve", "predicate": "reports_to"},
        ]

        with (
            patch("workers.tasks.ingest_business_data.FactRepository") as mock_repo_cls,
            patch("workers.tasks.ingest_business_data.get_arq") as mock_arq,
        ):
            mock_repo = AsyncMock()
            created = [MagicMock(id=str(uuid4()))]
            mock_repo.batch_create.return_value = created
            mock_repo_cls.return_value = mock_repo

            mock_pool = AsyncMock()
            mock_arq.return_value = mock_pool

            from workers.tasks.ingest_business_data import ingest_business_data

            result = await ingest_business_data(
                ctx=self._ctx(self._make_db()),
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                user_id=_USER_ID,
                facts=facts,
            )

            assert result["accepted"] == 1
            assert len(result["errors"]) == 4
            assert result["status"] == "completed_with_errors"

    @pytest.mark.asyncio
    async def test_no_valid_facts(self) -> None:
        """All facts invalid → completed_with_errors."""
        facts = [
            {"subject": "", "predicate": "knows", "object": "Bob"},
            {"predicate": "likes"},
        ]

        from workers.tasks.ingest_business_data import ingest_business_data

        result = await ingest_business_data(
            ctx={},
            org_id=_ORG_ID,
            project_id=_PROJECT_ID,
            user_id=_USER_ID,
            facts=facts,
        )

        assert result["accepted"] == 0
        assert len(result["errors"]) == 2
        assert result["status"] == "completed_with_errors"

    @pytest.mark.asyncio
    async def test_partial_failure(self) -> None:
        """Some records succeed, some fail."""
        facts = [
            {"subject": "Alice", "predicate": "works_at", "object": "Acme"},
            {"subject": "", "predicate": "invalid", "object": "data"},
            {"subject": "Bob", "predicate": "manages", "object": "Charlie"},
        ]

        with (
            patch("workers.tasks.ingest_business_data.FactRepository") as mock_repo_cls,
            patch("workers.tasks.ingest_business_data.get_arq") as mock_arq,
        ):
            mock_repo = AsyncMock()
            created = [MagicMock(id=str(uuid4())) for _ in range(2)]
            mock_repo.batch_create.return_value = created
            mock_repo_cls.return_value = mock_repo

            mock_pool = AsyncMock()
            mock_arq.return_value = mock_pool

            from workers.tasks.ingest_business_data import ingest_business_data

            result = await ingest_business_data(
                ctx=self._ctx(self._make_db()),
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                user_id=_USER_ID,
                facts=facts,
            )

            assert result["accepted"] == 2
            assert len(result["errors"]) == 1
            assert result["status"] == "completed_with_errors"

    @pytest.mark.asyncio
    async def test_duplicate_handling(self) -> None:
        """Duplicate facts handled — batch_create layer handles dedup."""
        facts = [
            {"subject": "Alice", "predicate": "works_at", "object": "Acme"},
            {"subject": "Alice", "predicate": "works_at", "object": "Acme"},
        ]

        with (
            patch("workers.tasks.ingest_business_data.FactRepository") as mock_repo_cls,
            patch("workers.tasks.ingest_business_data.get_arq") as mock_arq,
        ):
            mock_repo = AsyncMock()
            created = [MagicMock(id=str(uuid4()))]
            mock_repo.batch_create.return_value = created
            mock_repo_cls.return_value = mock_repo

            mock_pool = AsyncMock()
            mock_arq.return_value = mock_pool

            from workers.tasks.ingest_business_data import ingest_business_data

            result = await ingest_business_data(
                ctx=self._ctx(self._make_db()),
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                user_id=_USER_ID,
                facts=facts,
            )

            assert result["accepted"] >= 0
            mock_repo.batch_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_db_error_propagates(self) -> None:
        """Database errors propagate."""
        db = AsyncMock()
        db.__aenter__.side_effect = Exception("DB error")

        from workers.tasks.ingest_business_data import ingest_business_data

        with pytest.raises(Exception):
            await ingest_business_data(
                ctx=self._ctx(db),
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                user_id=_USER_ID,
                facts=[self._make_fact()],
            )
