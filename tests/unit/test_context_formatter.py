"""Unit tests for context_formatter — pure formatting functions.

No mocks needed — these are pure functions that transform data.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.context_formatter import format_json, format_text


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

    def _valid_fact(self, **kwargs) -> dict:
        """Sample fact with validity keys and no score for clean line rendering."""
        fact = self._sample_fact(score=None)
        for key in ("valid_from", "valid_to", "invalid_at"):
            if key in kwargs:
                fact[key] = kwargs[key]
        return fact

    # ── Fact validity date-range suffix ───────────────────────────────────

    def test_format_text_fact_validity_both_dates(self) -> None:
        """Facts with valid_from and valid_to render a full date-range suffix."""
        result = format_text(
            [],
            [
                self._valid_fact(
                    valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                    valid_to=datetime(2026, 6, 1, tzinfo=UTC),
                ),
            ],
            [],
            [],
        )
        assert "Python is great (valid: 2026-01-01 to 2026-06-01)" in result

    def test_format_text_fact_validity_iso_strings(self) -> None:
        """ISO-8601 string validity values render like datetime values."""
        result = format_text(
            [],
            [
                self._valid_fact(
                    valid_from="2026-01-01T00:00:00Z",
                    valid_to="2026-06-01T00:00:00+00:00",
                ),
            ],
            [],
            [],
        )
        assert "Python is great (valid: 2026-01-01 to 2026-06-01)" in result

    def test_format_text_fact_validity_from_only(self) -> None:
        """Facts with only valid_from render a 'from' suffix."""
        result = format_text(
            [],
            [self._valid_fact(valid_from=datetime(2026, 1, 1, tzinfo=UTC))],
            [],
            [],
        )
        assert "Python is great (valid: from 2026-01-01)" in result

    def test_format_text_fact_validity_to_only(self) -> None:
        """Facts with only valid_to render an 'until' suffix."""
        result = format_text(
            [],
            [self._valid_fact(valid_to="2026-06-01T00:00:00Z")],
            [],
            [],
        )
        assert "Python is great (valid: until 2026-06-01)" in result

    def test_format_text_fact_without_validity_unchanged(self) -> None:
        """Facts without validity keys render without any date suffix."""
        result = format_text(
            [],
            [self._sample_fact(score=None)],
            [],
            [],
        )
        assert "Python is great" in result
        assert "valid:" not in result

    def test_format_text_fact_unparseable_validity_skipped(self) -> None:
        """Unparseable validity strings are skipped without raising."""
        result = format_text(
            [],
            [self._valid_fact(valid_from="not-a-date", valid_to="also-bad")],
            [],
            [],
        )
        assert "Python is great" in result
        assert "valid:" not in result

    def test_format_text_fact_unparseable_from_valid_to(self) -> None:
        """Unparseable valid_from is skipped while a valid valid_to still renders."""
        result = format_text(
            [],
            [self._valid_fact(valid_from="garbage", valid_to="2026-06-01T00:00:00Z")],
            [],
            [],
        )
        assert "Python is great (valid: until 2026-06-01)" in result

    # ── format_json validity keys ─────────────────────────────────────────

    def test_format_json_facts_include_validity_keys(self) -> None:
        """format_json fact dicts carry valid_from, valid_to, and invalid_at."""
        result = format_json(
            [],
            [
                self._valid_fact(
                    valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                    valid_to=datetime(2026, 6, 1, tzinfo=UTC),
                    invalid_at=None,
                ),
            ],
            [],
            [],
        )
        fact = result["facts"][0]
        assert "valid_from" in fact
        assert "valid_to" in fact
        assert "invalid_at" in fact
        assert fact["valid_from"] == datetime(2026, 1, 1, tzinfo=UTC)
        assert fact["valid_to"] == datetime(2026, 6, 1, tzinfo=UTC)
        assert fact["invalid_at"] is None

    def test_format_json_facts_keep_validity_strip_ranking(self) -> None:
        """Whitelist keeps validity keys while still stripping ranking fields."""
        fact = self._sample_fact(score=0.9)
        fact["rrf_score"] = 0.5
        fact["valid_from"] = datetime(2026, 1, 1, tzinfo=UTC)

        result = format_json([], [fact], [], [])

        cleaned = result["facts"][0]
        assert cleaned["valid_from"] == datetime(2026, 1, 1, tzinfo=UTC)
        assert "score" not in cleaned
        assert "rrf_score" not in cleaned
