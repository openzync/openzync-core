"""Unit tests for context_formatter — pure formatting functions.

No mocks needed — these are pure functions that transform data.
"""

from __future__ import annotations

import pytest

from services.context_formatter import format_text, format_json


@pytest.mark.unit
class TestContextFormatter:
    """Context formatter tests."""

    def _sample_episode(self, **kwargs) -> dict:
        return {
            "id": kwargs.get("id", "ep-1"),
            "content": kwargs.get("content", "Hello world"),
            "role": kwargs.get("role", "user"),
            "score": kwargs.get("score", 0.95),
            "created_at": kwargs.get("created_at", "2026-01-01T00:00:00Z"),
        }

    def _sample_fact(self, **kwargs) -> dict:
        return {
            "id": kwargs.get("id", "fact-1"),
            "content": kwargs.get("content", "Python is great"),
            "subject": kwargs.get("subject", "Python"),
            "predicate": kwargs.get("predicate", "is"),
            "object": kwargs.get("object", "great"),
            "score": kwargs.get("score", 0.9),
        }

    def _sample_entity(self, **kwargs) -> dict:
        return {
            "id": kwargs.get("id", "ent-1"),
            "name": kwargs.get("name", "Python"),
            "type": kwargs.get("type", "Language"),
            "summary": kwargs.get("summary", "A programming language"),
            "distance": kwargs.get("distance", 0),
        }

    def test_format_text_empty(self) -> None:
        """Empty inputs produce a minimal context block."""
        result = format_text([], [], [], [])
        assert isinstance(result, str)
        assert len(result) > 0

    def test_format_text_includes_episodes(self) -> None:
        """Episodes appear in the formatted text."""
        result = format_text(
            [self._sample_episode(content="Test conversation")],
            [], [], [],
        )
        assert "Test conversation" in result
        assert "Episode" in result or "episode" in result.lower()

    def test_format_text_includes_facts(self) -> None:
        """Facts appear with subject-predicate-object."""
        result = format_text(
            [],
            [self._sample_fact(content="Python is great")],
            [], [],
        )
        assert "Python" in result
        assert "great" in result

    def test_format_text_includes_entities(self) -> None:
        """Entities appear with name and type."""
        result = format_text(
            [], [],
            [self._sample_entity(name="OpenZync", type="Project")],
            [],
        )
        assert "OpenZync" in result

    def test_format_json_empty(self) -> None:
        """Empty inputs produce a structured JSON dict."""
        result = format_json([], [], [], [])
        assert isinstance(result, dict)
        assert "episodes" in result
        assert "facts" in result
        assert "entities" in result

    def test_format_json_includes_data(self) -> None:
        """Non-empty inputs appear in the JSON output."""
        result = format_json(
            [self._sample_episode()],
            [self._sample_fact()],
            [self._sample_entity()],
            [],
        )
        assert len(result["episodes"]) == 1
        assert len(result["facts"]) == 1
        assert len(result["entities"]) == 1

    def test_format_text_multiple_sources(self) -> None:
        """All three sources appear in the final text when provided."""
        result = format_text(
            [self._sample_episode(content="Episode content")],
            [self._sample_fact(content="Fact content")],
            [self._sample_entity(name="Entity Name")],
            [],
        )
        assert "Episode content" in result
        assert "Fact content" in result
        assert "Entity Name" in result
