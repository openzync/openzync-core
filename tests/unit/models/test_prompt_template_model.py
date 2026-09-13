"""Tests for ``PromptTemplate`` model."""
from __future__ import annotations

import uuid

import pytest

from models.prompt_template import PromptTemplate


class TestPromptTemplateModel:
    """Cover PromptTemplate fields — name, text, version, is_active, is_system_default."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        pt = PromptTemplate(
            template_name="memory_summary",
            template_text="Summarize the following: {content}",
        )
        assert pt.template_name == "memory_summary"
        assert pt.template_text == "Summarize the following: {content}"

    @pytest.mark.unit
    def test_defaults_configured(self) -> None:
        """version, is_active, is_default_for_type have server_defaults."""
        for col_name in ["version", "is_active", "is_default_for_type"]:
            col = PromptTemplate.__table__.columns[col_name]
            assert col.server_default is not None, f"{col_name} missing server_default"

    @pytest.mark.unit
    def test_nullable_fields(self) -> None:
        """organization_id and description default to None."""
        pt = PromptTemplate(
            template_name="test",
            template_text="test",
        )
        assert pt.organization_id is None
        assert pt.description is None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is prompt_templates."""
        assert PromptTemplate.__tablename__ == "prompt_templates"

    @pytest.mark.unit
    def test_is_system_default_property(self) -> None:
        """is_system_default returns False when organization_id is set."""
        pt = PromptTemplate(
            organization_id=uuid.uuid4(),
            template_name="test",
            template_text="test",
        )
        assert pt.is_system_default is False

    @pytest.mark.unit
    def test_is_system_default_true_when_org_none_and_active(self) -> None:
        """is_system_default returns True only when org is None AND is_active."""
        pt = PromptTemplate(
            template_name="test",
            template_text="test",
            is_active=True,
        )
        assert pt.is_system_default is True

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes name, version, is_active."""
        pt = PromptTemplate(
            template_name="summary",
            template_text="text",
            version=3,
        )
        assert "PromptTemplate" in repr(pt)
        assert "summary" in repr(pt)
        assert "v3" in repr(pt)
