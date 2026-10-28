"""Fact-path context purge: purge retires keys and bumps the generation.

The post-ingest purge both SCAN+DELs legacy keys AND bumps the ``ctxver``
generation, so versioned ``ctx:...:v{gen}`` keys minted after the purge
cannot reuse the pre-purge generation and serve stale context until TTL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from services.fact_invalidation_service import _purge_project_context

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class TestFactPurgeBumpsVersion:
    """The fact-path purge retires versioned ``ctx:...:v{gen}`` keys too."""

    @pytest.mark.asyncio
    async def test_purge_invalidates_and_bumps_generation(self) -> None:
        cache = AsyncMock()

        await _purge_project_context(cache, ORG_ID, PROJECT_ID)

        cache.invalidate_project_context.assert_awaited_once_with(
            str(ORG_ID), str(PROJECT_ID)
        )
        # Without this bump, post-purge keys would reuse the pre-purge
        # generation and serve stale context until TTL expiry.
        cache.bump_context_version.assert_awaited_once_with(
            str(ORG_ID), str(PROJECT_ID)
        )
