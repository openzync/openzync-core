"""Tests for ``EpisodeBlob`` model."""
from __future__ import annotations

import uuid

import pytest

from models.episode_blob import EpisodeBlob


class TestEpisodeBlobModel:
    """Cover EpisodeBlob fields — storage, file metadata, image dims, extracted text."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        blob = EpisodeBlob(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            episode_id=uuid.uuid4(),
            created_by=uuid.uuid4(),
            storage_key="uploads/abc123.pdf",
            file_name="report.pdf",
            mime_type="application/pdf",
            file_size=1024,
            content_hash="a" * 64,
        )
        assert blob.organization_id is not None
        assert blob.project_id is not None
        assert blob.session_id is not None
        assert blob.episode_id is not None
        assert blob.created_by is not None
        assert blob.storage_key == "uploads/abc123.pdf"
        assert blob.file_name == "report.pdf"
        assert blob.mime_type == "application/pdf"
        assert blob.file_size == 1024
        assert blob.content_hash == "a" * 64

    @pytest.mark.unit
    def test_defaults_configured(self) -> None:
        """storage_backend has default='s3', blob_index has default=0."""
        col = EpisodeBlob.__table__.columns["storage_backend"]
        assert col.default is not None
        idx_col = EpisodeBlob.__table__.columns["blob_index"]
        assert idx_col.default is not None

    @pytest.mark.unit
    def test_nullable_fields(self) -> None:
        """width, height, extracted_text default to None."""
        blob = EpisodeBlob(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            episode_id=uuid.uuid4(),
            created_by=uuid.uuid4(),
            storage_key="k",
            file_name="f",
            mime_type="t",
            file_size=1,
            content_hash="h",
        )
        assert blob.width is None
        assert blob.height is None
        assert blob.extracted_text is None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is episode_blobs."""
        assert EpisodeBlob.__tablename__ == "episode_blobs"

    @pytest.mark.unit
    def test_unique_constraint(self) -> None:
        """UniqueConstraint on (episode_id, blob_index)."""
        uq_name = "uq_episode_blob_index"
        constraints = EpisodeBlob.__table_args__
        names = {c.name for c in constraints if hasattr(c, "name")}
        assert uq_name in names

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes file_name, mime_type, episode_id."""
        blob = EpisodeBlob(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            episode_id=uuid.uuid4(),
            created_by=uuid.uuid4(),
            storage_key="k",
            file_name="photo.png",
            mime_type="image/png",
            file_size=2048,
            content_hash="h",
        )
        assert "EpisodeBlob" in repr(blob)
        assert "photo.png" in repr(blob)
