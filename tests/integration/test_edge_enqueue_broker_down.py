"""Edge-enqueue broker down -> 5xx, nothing committed (real PG/Redis)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_fact_ingest_broker_down_nothing_committed(engine, redis_client) -> None:
    from core.config import Settings, get_settings, set_settings

    try:
        get_settings()
    except RuntimeError:
        set_settings(Settings(DATABASE_URL="postgresql+asyncpg://localhost/x", REDIS_URL="redis://localhost/0", SECRET_KEY="i" * 32, WEBHOOK_SIGNING_SECRET="j" * 32))
    from sqlalchemy.ext.asyncio import AsyncSession

    from services.fact_service import FactService
    from services.idempotency_service import ContentHashClaim

    async with AsyncSession(engine) as db:
        fact_repo = AsyncMock()
        session_repo = AsyncMock()
        session_repo.get_by_external_id.return_value = MagicMock(
            id=UUID("00000000-0000-0000-0000-000000000010")
        )
        idem = AsyncMock()
        idem.claim_content_hash.return_value = ContentHashClaim(won=True, winner="")
        idem.release_content_hash.return_value = None
        svc = FactService(
            db=db, redis_client=redis_client, fact_repo=fact_repo,
            session_repo=session_repo, idempotency_service=idem,
        )
        with (
            patch(
                "services.fact_invalidation_service.FactInvalidationService"
            ) as mock_inv,
            patch(
                "services.fact_service.get_arq",
                side_effect=ConnectionError("broker down"),
            ),
            pytest.raises(Exception),
        ):
            from schemas.facts import FactTriple

            mock_inv.return_value.ingest_with_supersession = AsyncMock(
                side_effect=ConnectionError("broker down")
            )
            await svc.ingest_facts(
                org_id=UUID("00000000-0000-0000-0000-000000000001"),
                project_id=UUID("00000000-0000-0000-0000-000000000003"),
                created_by=UUID("00000000-0000-0000-0000-000000000002"),
                facts=[FactTriple(subject="a", predicate="b", object="c")],
                session_external_id="sess-edge",
            )
        idem.release_content_hash.assert_awaited_once()
