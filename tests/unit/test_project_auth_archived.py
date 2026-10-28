"""Project auth on archived project returns 404 (mocked repo, no I/O)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from dependencies.project_auth import require_project_membership

pytestmark = pytest.mark.unit

ORG_A = UUID("00000000-0000-0000-0000-0000000000a1")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")


class TestApiKeyArchivedProject:
    """API-key auth on an archived project → 404 (parity with JWT)."""

    @pytest.mark.asyncio
    async def test_api_key_archived_project_404(self) -> None:
        """Org-scoped lookup misses archived rows → 404, same as JWT."""
        request = MagicMock()
        request.state.org_id = str(ORG_A)
        request.state.auth_type = "api_key"
        request.state.api_key_project_id = str(PROJECT_ID)

        with patch(
            "dependencies.project_auth.ProjectRepository"
        ) as repo_cls:
            repo_cls.return_value.get_by_id = AsyncMock(return_value=None)
            with pytest.raises(HTTPException) as exc_info:
                await require_project_membership(
                    request, project_id=PROJECT_ID, db=AsyncMock()
                )

        assert exc_info.value.status_code == 404
