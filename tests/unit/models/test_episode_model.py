"""Tests for ``Episode`` model."""
from __future__ import annotations

import uuid

import pytest

from models.episode import Episode


class TestEpisodeModel:
    """Cover Episode fields — role, content, metadata, enrichment, soft-delete."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        ep = Episode(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            role="user",
            content="Hello!",
        )
        assert ep.organization_id is not None
        assert ep.project_id is not None
        assert ep.session_id is not None
        assert ep.user_id is not None
        assert ep.role == "user"
        assert ep.content == "Hello!"

    @pytest.mark.unit
    def test_defaults_configured(self) -> None:
        """metadata_, token_count, sequence_number, enrichment_status, is_deleted have server_defaults."""
        for col_name in ["metadata", "token_count", "sequence_number", "enrichment_status", "is_deleted"]:
            col = Episode.__table__.columns[col_name]
            # 'metadata' column uses name="metadata" in the mapping
            assert col.server_default is not None, f"{col_name} missing server_default"

    @pytest.mark.unit
    def test_nullable_embedding(self) -> None:
        """embedding defaults to None (populated after enrichment)."""
        ep = Episode(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            role="system",
            content="System prompt.",
        )
        assert ep.embedding is None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is episodes."""
        assert Episode.__tablename__ == "episodes"

    @pytest.mark.unit
    def test_check_constraints(self) -> None:
        """CheckConstraints enforce role values and content length."""
        constraints = Episode.__table_args__
        names = {c.name for c in constraints if hasattr(c, "name")}
        assert "ck_episode_role" in names
        assert "ck_episode_content_length" in names

    @pytest.mark.unit
    def test_indices(self) -> None:
        """Indices exist on (session_id, sequence_number) and user_id."""
        constraints = Episode.__table_args__
        index_names = {
            c.name for c in constraints if hasattr(c, "name") and not c.name.startswith("ck_")
        }
        assert "ix_episode_session_sequence" in index_names
        assert "ix_episode_user_id" in index_names

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes session_id, sequence_number, role."""
        ep = Episode(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            role="user",
            content="test",
            sequence_number=5,
        )
        assert "Episode" in repr(ep)
        assert "seq=5" in repr(ep)
