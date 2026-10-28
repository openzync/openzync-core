"""CORS wildcard rejection at startup (no I/O)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from core.config import Settings

pytestmark = pytest.mark.unit


class TestCorsWildcardRejected:
    """CORS: ``"*"`` is a startup error while credentials are allowed."""

    def test_wildcard_cors_origin_raises(self) -> None:
        """``CORS_ORIGINS="*"`` → ``ValueError`` at ``Settings`` build."""
        with pytest.raises(PydanticValidationError, match=r"\*"):
            Settings(
                DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/t",
                REDIS_URL="redis://localhost:6379/1",
                SECRET_KEY="a" * 32,
                WEBHOOK_SIGNING_SECRET="b" * 32,
                CORS_ORIGINS="*",
            )
