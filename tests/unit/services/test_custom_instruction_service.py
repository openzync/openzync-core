"""Unit tests for custom_instruction_service — pure formatting function."""
from __future__ import annotations

from services.custom_instruction_service import format_custom_instructions


class TestFormatCustomInstructions:
    """Tests for the standalone format_custom_instructions()."""

    def test_empty_list_returns_empty_string(self) -> None:
        assert format_custom_instructions([]) == ""

    def test_single_instruction_formats_correctly(self) -> None:
        """A single {name, text} dict becomes a markdown heading + body."""
        result = format_custom_instructions([
            {"name": "legal", "text": "Use legal terminology."},
        ])
        assert result == "### legal\nUse legal terminology."

    def test_multiple_instructions_separated_by_blank_line(self) -> None:
        """Multiple instructions are joined by a blank line separator."""
        result = format_custom_instructions([
            {"name": "legal", "text": "Use legal terms."},
            {"name": "tone", "text": "Be concise and professional."},
        ])
        assert result == (
            "### legal\nUse legal terms.\n\n"
            "### tone\nBe concise and professional."
        )

    def test_special_characters_in_name_and_text(self) -> None:
        """Names/text with special chars are passed through verbatim."""
        result = format_custom_instructions([
            {"name": "code/style", "text": "Use **bold** and `inline code`."},
        ])
        assert "### code/style" in result
        assert "**bold**" in result
        assert "`inline code`" in result

    def test_triple_quotes_in_text(self) -> None:
        """Triple quotes and newlines inside text are preserved."""
        result = format_custom_instructions([
            {"name": "multi", "text": "Line one\nLine two\nLine three"},
        ])
        assert result == "### multi\nLine one\nLine two\nLine three"
