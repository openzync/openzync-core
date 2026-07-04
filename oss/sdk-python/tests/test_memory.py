"""Tests for the OpenZync Python SDK — memory domain."""

from __future__ import annotations

import pytest
from httpx import Response

from openzync.models.memory import (
    IngestMemoryResponse,
    Message,
)
from tests.conftest import mock_error_response, mock_response


class TestMemoryClient:
    """Tests for ``AsyncMemoryClient``."""

    @pytest.mark.asyncio
    async def test_ingest_memory(self, async_client, mock_http):
        """POST /memory returns IngestMemoryResponse."""
        project_id = "p1"
        expected = {
            "job_id": "job-456",
            "episode_count": 2,
            "status": "accepted",
            "message": "Messages accepted for processing",
        }
        mock_http.post(f"/v1/projects/{project_id}/memory").respond(
            status_code=202, json=expected
        )

        messages = [
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi there"),
        ]
        result = await async_client.memory.ingest(project_id=project_id, messages=messages)

        assert isinstance(result, IngestMemoryResponse)
        assert result.job_id == "job-456"
        assert result.episode_count == 2
        assert result.status == "accepted"

    @pytest.mark.asyncio
    async def test_ingest_memory_with_session(self, async_client, mock_http):
        """POST /memory with session_id."""
        project_id = "p1"
        mock_http.post(f"/v1/projects/{project_id}/memory").respond(
            status_code=202, json={"job_id": "j1", "episode_count": 1, "status": "accepted"}
        )

        result = await async_client.memory.ingest(
            project_id=project_id,
            messages=[{"role": "user", "content": "test"}],
            session_id="s1",
        )
        assert result.episode_count == 1

    @pytest.mark.asyncio
    async def test_get_context(self, async_client, mock_http):
        """GET /context returns context text."""
        project_id = "p1"
        expected = {
            "context": "Recent Episodes (1):\n1. Hello world",
            "metadata": {"assembly_time_ms": 5.0, "source_counts": {}},
        }
        mock_http.get(f"/v1/projects/{project_id}/context").respond(json=expected)

        result = await async_client.memory.get_context(
            project_id=project_id, query="hello", limit=10
        )
        assert "Hello world" in result.context
        assert result.metadata["assembly_time_ms"] == 5.0

    @pytest.mark.asyncio
    async def test_delete_memory(self, async_client, mock_http):
        """DELETE /memory returns 204."""
        project_id = "p1"
        mock_http.delete(f"/v1/projects/{project_id}/memory").respond(status_code=204)

        await async_client.memory.delete(project_id=project_id)
        # No exception means success

    @pytest.mark.asyncio
    async def test_ingest_memory_validation_error(self, async_client, mock_http):
        """POST /memory with invalid data raises error."""
        project_id = "p1"
        mock_http.post(f"/v1/projects/{project_id}/memory").respond(
            status_code=422,
            json={"detail": "Validation error", "status": 422},
        )

        with pytest.raises(Exception):
            await async_client.memory.ingest(project_id=project_id, messages=[])
