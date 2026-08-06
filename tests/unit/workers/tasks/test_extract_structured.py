"""Unit tests for the structured-extraction post-processing helper.

The standalone ``extract_structured`` ARQ task was retired in favour of the
combined ``enrich_episode`` worker (which calls ``process_structured_output``
as its structured-extraction section).  These tests exercise the helper
directly — the caller-owned orchestration (LLM call, idempotency, episode
not found, graph-backend behaviour) is covered by ``test_enrich_episode.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

_EPISODE_ID = str(uuid4())
_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())
_SESSION_ID = str(uuid4())


@pytest.mark.unit
class TestProcessStructuredOutput:
    """process_structured_output edge cases."""

    @pytest.mark.asyncio
    async def test_process_empty_parsed(self) -> None:
        """Empty parsed dict → returns early."""
        db = AsyncMock()
        from workers.tasks.extract_structured import process_structured_output

        await process_structured_output(
            db=db, org_id=_ORG_ID, episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID, session_id=_SESSION_ID,
            parsed={}, schemas=[{"name": "test", "id": str(uuid4()), "json_schema": {}}],
        )
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_unknown_schema(self) -> None:
        """Unknown schema name → warning and skip."""
        db = AsyncMock()
        from workers.tasks.extract_structured import process_structured_output

        await process_structured_output(
            db=db, org_id=_ORG_ID, episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID, session_id=_SESSION_ID,
            parsed={"unknown_name": {"field": "value"}},
            schemas=[{"name": "known_schema", "id": str(uuid4()), "json_schema": {}}],
        )
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_none_data(self) -> None:
        """None data value → skipped."""
        db = AsyncMock()
        from workers.tasks.extract_structured import process_structured_output

        await process_structured_output(
            db=db, org_id=_ORG_ID, episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID, session_id=_SESSION_ID,
            parsed={"test_schema": None},
            schemas=[{"name": "test_schema", "id": str(uuid4()), "json_schema": {}}],
        )
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_non_dict_data(self) -> None:
        """Non-dict data → warning and skip."""
        db = AsyncMock()
        from workers.tasks.extract_structured import process_structured_output

        await process_structured_output(
            db=db, org_id=_ORG_ID, episode_id=_EPISODE_ID,
            project_id=_PROJECT_ID, session_id=_SESSION_ID,
            parsed={"test_schema": "string_data"},
            schemas=[{"name": "test_schema", "id": str(uuid4()), "json_schema": {}}],
        )
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_missing_required_fields(self) -> None:
        """Missing required fields → filled with defaults."""
        db = AsyncMock()
        from workers.tasks.extract_structured import process_structured_output

        with patch(
            "workers.tasks.extract_structured._validate_against_schema",
        ) as mock_validate:
            schema_id = str(uuid4())
            await process_structured_output(
                db=db, org_id=_ORG_ID, episode_id=_EPISODE_ID,
                project_id=_PROJECT_ID, session_id=_SESSION_ID,
                parsed={"test_schema": {"present": "value"}},
                schemas=[{
                    "name": "test_schema", "id": schema_id,
                    "json_schema": {
                        "type": "object",
                        "required": ["required_field"],
                        "properties": {
                            "required_field": {"type": "string"},
                            "present": {"type": "string"},
                        },
                    },
                }],
            )
            mock_validate.assert_called_once()
            cleaned = mock_validate.call_args[0][0]
            assert cleaned.get("required_field") == "unknown"
            assert cleaned.get("present") == "value"

    @pytest.mark.asyncio
    async def test_process_validation_failure(self) -> None:
        """Schema validation failure → warning and continue."""
        db = AsyncMock()
        from workers.tasks.extract_structured import process_structured_output

        with patch(
            "workers.tasks.extract_structured._validate_against_schema",
            side_effect=Exception("invalid data"),
        ):
            schema_id = str(uuid4())
            await process_structured_output(
                db=db, org_id=_ORG_ID, episode_id=_EPISODE_ID,
                project_id=_PROJECT_ID, session_id=_SESSION_ID,
                parsed={"test_schema": {"field": "value"}},
                schemas=[{"name": "test_schema", "id": schema_id, "json_schema": {}}],
            )
            db.execute.assert_not_called()
