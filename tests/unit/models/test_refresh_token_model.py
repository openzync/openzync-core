"""Tests for ``RefreshToken`` model."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from models.refresh_token import RefreshToken


class TestRefreshTokenModel:
    """Cover RefreshToken fields — token_hash, expires_at, is_revoked, rotated_by."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        expires = datetime.now(timezone.utc)
        token = RefreshToken(
            user_id="admin-1",
            organization_id=uuid.uuid4(),
            token_hash="abc123hash",
            expires_at=expires,
        )
        assert token.user_id == "admin-1"
        assert token.organization_id is not None
        assert token.token_hash == "abc123hash"
        assert token.expires_at == expires

    @pytest.mark.unit
    def test_defaults_configured(self) -> None:
        """is_revoked has server_default='false'."""
        col = RefreshToken.__table__.columns["is_revoked"]
        assert col.server_default is not None
        assert "false" in str(col.server_default.arg)

    @pytest.mark.unit
    def test_nullable_rotated_by(self) -> None:
        """rotated_by defaults to None."""
        token = RefreshToken(
            user_id="admin-1",
            organization_id=uuid.uuid4(),
            token_hash="hash",
            expires_at=datetime.now(timezone.utc),
        )
        assert token.rotated_by is None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is refresh_tokens."""
        assert RefreshToken.__tablename__ == "refresh_tokens"

    @pytest.mark.unit
    def test_unique_token_hash(self) -> None:
        """token_hash has unique=True."""
        col = RefreshToken.__table__.columns["token_hash"]
        assert col.unique is True

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes id, is_revoked, expires_at."""
        expires = datetime.now(timezone.utc)
        token = RefreshToken(
            user_id="admin-1",
            organization_id=uuid.uuid4(),
            token_hash="hash",
            expires_at=expires,
            is_revoked=False,
        )
        assert "RefreshToken" in repr(token)
        assert "revoked=False" in repr(token)
