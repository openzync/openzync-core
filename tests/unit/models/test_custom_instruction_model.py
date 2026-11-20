"""Tests for ``CustomInstruction`` model."""
from __future__ import annotations

import uuid

import pytest

from models.custom_instruction import CustomInstruction


class TestCustomInstructionModel:
    """Cover CustomInstruction fields — scope, name, text, target_id."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        inst = CustomInstruction(
            user_id=uuid.uuid4(),
            scope="extraction",
            name="legal_domain",
            text="Focus on legal terminology.",
        )
        assert inst.user_id is not None
        assert inst.scope == "extraction"
        assert inst.name == "legal_domain"
        assert inst.text == "Focus on legal terminology."

    @pytest.mark.unit
    def test_nullable_target_id(self) -> None:
        """target_id defaults to None (org-level instruction)."""
        inst = CustomInstruction(
            user_id=uuid.uuid4(),
            scope="user_summary",
            name="user_prefs",
            text="User prefers concise responses.",
        )
        assert inst.target_id is None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is custom_instructions."""
        assert CustomInstruction.__tablename__ == "custom_instructions"

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes name, scope, and target info."""
        inst = CustomInstruction(
            user_id=uuid.uuid4(),
            scope="extraction",
            name="test",
            text="hello",
        )
        assert "CustomInstruction" in repr(inst)
        assert "test" in repr(inst)
        assert "org-level" in repr(inst)
