"""Tests for the OpenZync Python SDK — facts domain."""

from __future__ import annotations

import pytest

from openzync.models.facts import FactBatchResponse
from tests.conftest import mock_response


class TestFactsClient:
    """Tests for ``AsyncFactsClient``."""

    @pytest.mark.asyncio
    async def test_add_facts(self, async_client, mock_http):
        """POST /facts returns FactBatchResponse."""
        project_id = "p1"
        expected = {
            "job_id": "job-789",
            "accepted_count": 2,
            "status": "accepted",
            "message": "2 facts accepted",
        }
        mock_http.post(f"/v1/projects/{project_id}/facts").respond(
            status_code=202, json=expected
        )

        result = await async_client.facts.add(
            project_id=project_id,
            facts=[
                {"subject": "Alice", "predicate": "works_at", "object": "Acme"},
                {"subject": "Alice", "predicate": "likes", "object": "hiking"},
            ],
        )

        assert isinstance(result, FactBatchResponse)
        assert result.accepted_count == 2
        assert result.job_id == "job-789"

    @pytest.mark.asyncio
    async def test_add_facts_with_session(self, async_client, mock_http):
        """POST /facts with session_id."""
        project_id = "p1"
        mock_http.post(f"/v1/projects/{project_id}/facts").respond(
            status_code=202, json={"job_id": "j1", "accepted_count": 1, "status": "accepted"}
        )

        result = await async_client.facts.add(
            project_id=project_id,
            facts=[{"subject": "X", "predicate": "y", "object": "z"}],
            session_id="s1",
        )
        assert result.accepted_count == 1
