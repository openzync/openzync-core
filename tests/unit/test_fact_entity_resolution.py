"""Unit tests for fact entity resolution logic.

Tests the ``_match_entity`` and ``_resolve_fact_entities`` functions
from the fact extraction worker in isolation — no DB, no LLM, fast.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from workers.tasks.extract_facts import _match_entity, _resolve_fact_entities


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def known_entities() -> list[dict]:
    """Return a standard set of known entities for testing."""
    return [
        {"id": uuid.UUID("a60dc62c-6f20-4123-b704-dad184f1004f"), "name": "Rohan", "entity_type": "Person", "summary": None},
        {"id": uuid.UUID("e3d80c00-7ca3-4774-90fb-ecaad13ffd04"), "name": "Kolkata", "entity_type": "Location", "summary": None},
        {"id": uuid.UUID("bc064116-5312-4d2c-b0b2-eaa43bebd506"), "name": "ExampleOrg", "entity_type": "Organization", "summary": None},
        {"id": uuid.UUID("c9dd30ea-820e-4271-868f-f438d6141362"), "name": "AI Engineer", "entity_type": "Custom", "summary": None},
        {"id": uuid.UUID("f47ac10b-58cc-4372-a567-0e02b2c3d479"), "name": "Alice", "entity_type": "Person", "summary": None},
        {"id": uuid.UUID("9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"), "name": "Acme Corp", "entity_type": "Organization", "summary": None},
    ]


# ── _match_entity tests ────────────────────────────────────────────────────────


class TestMatchEntity:
    """Tests for the single-entity matching function."""

    def test_exact_match_case_insensitive(self, known_entities: list[dict]) -> None:
        """Exact match should work regardless of casing."""
        result = _match_entity("rohan", known_entities)
        assert result is not None
        assert result["name"] == "Rohan"

    def test_exact_match(self, known_entities: list[dict]) -> None:
        """Exact match returns the entity."""
        result = _match_entity("ExampleOrg", known_entities)
        assert result is not None
        assert result["name"] == "ExampleOrg"
        assert result["entity_type"] == "Organization"

    def test_entity_name_is_substring_of_candidate(self, known_entities: list[dict]) -> None:
        """Entity name embedded in candidate (e.g. 'Rohan' in 'Rohan's team')."""
        result = _match_entity("Rohan's team", known_entities)
        assert result is not None
        assert result["name"] == "Rohan"

    def test_first_person_pronoun_resolved(self, known_entities: list[dict]) -> None:
        """"I" should resolve to first Person entity via first-person pronoun rule."""
        result = _match_entity("I", known_entities)
        assert result is not None
        assert result["name"] == "Rohan"
        assert result["entity_type"] == "Person"

    def test_other_first_person_pronouns(self, known_entities: list[dict]) -> None:
        """"me", "my", "mine", "myself" should all resolve to first Person."""
        for pronoun in ("me", "my", "mine", "myself"):
            result = _match_entity(pronoun, known_entities)
            assert result is not None, f"{pronoun} should resolve"
            assert result["name"] == "Rohan"

    def test_short_candidate_no_match_when_not_pronoun(self, known_entities: list[dict]) -> None:
        """Short candidates (< 3 chars) that are NOT pronouns should NOT match.

        E.g., 'AI' should not match 'AI Engineer' (single-letter match is off).
        """
        result = _match_entity("AI", known_entities)
        assert result is None, "Should not match short non-pronoun candidates"

    def test_candidate_is_substring_of_entity_name_long(self, known_entities: list[dict]) -> None:
        """Long candidate (3+ chars) that is a substring of entity name."""
        result = _match_entity("Acme", known_entities)
        assert result is not None
        assert result["name"] == "Acme Corp"

    def test_no_match(self, known_entities: list[dict]) -> None:
        """Completely unrelated string returns None."""
        result = _match_entity("UnknownPerson", known_entities)
        assert result is None

    def test_empty_string(self, known_entities: list[dict]) -> None:
        """Empty string should not match anything."""
        result = _match_entity("", known_entities)
        assert result is None

    def test_whitespace_handling(self, known_entities: list[dict]) -> None:
        """Leading/trailing whitespace should be stripped."""
        result = _match_entity("  Rohan  ", known_entities)
        assert result is not None
        assert result["name"] == "Rohan"


# ── _resolve_fact_entities tests ────────────────────────────────────────────────


class TestResolveFactEntities:
    """Tests for the batch fact entity resolution function."""

    def test_resolves_first_person_pronoun(self, known_entities: list[dict]) -> None:
        """"I" in facts should resolve to first Person entity (Rohan)."""
        facts: list[dict[str, Any]] = [
            {
                "subject": "I",
                "predicate": "works_at",
                "object": "ExampleOrg",
                "confidence": 0.97,
                "subject_type": "literal",
                "object_type": "literal",
                "subject_entity_id": None,
                "object_entity_id": None,
            },
        ]
        resolved = _resolve_fact_entities(facts, known_entities)

        # "I" should now resolve to the first Person entity ("Rohan")
        assert resolved[0]["subject"] == "Rohan"
        assert resolved[0]["subject_type"] == "entity"
        assert resolved[0]["subject_entity_id"] == known_entities[0]["id"]

    def test_resolves_me_pronoun(self, known_entities: list[dict]) -> None:
        """"me" in facts should also resolve to first Person entity."""
        facts: list[dict[str, Any]] = [
            {
                "subject": "me",
                "predicate": "lives_in",
                "object": "Kolkata",
                "confidence": 0.95,
                "subject_type": "literal",
                "object_type": "literal",
                "subject_entity_id": None,
                "object_entity_id": None,
            },
        ]
        resolved = _resolve_fact_entities(facts, known_entities)
        assert resolved[0]["subject"] == "Rohan"
        assert resolved[0]["subject_type"] == "entity"
        assert resolved[0]["subject_entity_id"] == known_entities[0]["id"]

    def test_my_pronoun_in_object(self, known_entities: list[dict]) -> None:
        """"my" used as an object modifier should resolve to first Person."""
        facts: list[dict[str, Any]] = [
            {
                "subject": "Alice",
                "predicate": "knows",
                "object": "my",
                "confidence": 0.9,
                "subject_type": "literal",
                "object_type": "literal",
                "subject_entity_id": None,
                "object_entity_id": None,
            },
        ]
        resolved = _resolve_fact_entities(facts, known_entities)
        assert resolved[0]["object"] == "Rohan"
        assert resolved[0]["object_type"] == "entity"
        assert resolved[0]["object_entity_id"] == known_entities[0]["id"]

    def test_first_person_no_person_entity(self) -> None:
        """When no Person entity exists, first-person pronouns fall through to
        exact/substring matching — and remain literal if no match."""
        entities_no_person: list[dict] = [
            {"id": uuid.UUID("11111111-1111-4111-8111-111111111111"), "name": "ExampleOrg", "entity_type": "Organization", "summary": None},
            {"id": uuid.UUID("22222222-2222-4222-8222-222222222222"), "name": "Acme Corp", "entity_type": "Organization", "summary": None},
        ]
        facts: list[dict[str, Any]] = [
            {
                "subject": "I",
                "predicate": "works_at",
                "object": "ExampleOrg",
                "confidence": 0.97,
                "subject_type": "literal",
                "object_type": "literal",
                "subject_entity_id": None,
                "object_entity_id": None,
            },
        ]
        resolved = _resolve_fact_entities(facts, entities_no_person)
        # No Person entity → falls through to exact match → no match → literal
        assert resolved[0]["subject"] == "I"
        assert resolved[0]["subject_type"] == "literal"
        assert resolved[0]["subject_entity_id"] is None

    def test_resolves_object_when_known(self, known_entities: list[dict]) -> None:
        """Object 'ExampleOrg' should resolve to ExampleOrg entity."""
        facts: list[dict[str, Any]] = [
            {
                "subject": "Rohan",
                "predicate": "works_at",
                "object": "ExampleOrg",
                "confidence": 0.97,
                "subject_type": "literal",
                "object_type": "literal",
                "subject_entity_id": None,
                "object_entity_id": None,
            },
        ]
        resolved = _resolve_fact_entities(facts, known_entities)

        assert resolved[0]["subject"] == "Rohan"
        assert resolved[0]["subject_type"] == "entity"
        assert resolved[0]["subject_entity_id"] == known_entities[0]["id"]
        assert resolved[0]["object"] == "ExampleOrg"
        assert resolved[0]["object_type"] == "entity"
        assert resolved[0]["object_entity_id"] == known_entities[2]["id"]

    def test_resolves_both_subject_and_object(self, known_entities: list[dict]) -> None:
        """Both subject and object should resolve."""
        facts: list[dict[str, Any]] = [
            {
                "subject": "Rohan",
                "predicate": "resides_in",
                "object": "Kolkata",
                "confidence": 0.98,
                "subject_type": "literal",
                "object_type": "literal",
                "subject_entity_id": None,
                "object_entity_id": None,
            },
        ]
        resolved = _resolve_fact_entities(facts, known_entities)

        assert resolved[0]["subject_entity_id"] == known_entities[0]["id"]
        assert resolved[0]["object_entity_id"] == known_entities[1]["id"]

    def test_unknown_entity_stays_literal(self, known_entities: list[dict]) -> None:
        """An unknown entity should stay as literal with no entity_id."""
        facts: list[dict[str, Any]] = [
            {
                "subject": "Rohan",
                "predicate": "uses",
                "object": "Python",
                "confidence": 0.8,
                "subject_type": "literal",
                "object_type": "literal",
                "subject_entity_id": None,
                "object_entity_id": None,
            },
        ]
        resolved = _resolve_fact_entities(facts, known_entities)

        assert resolved[0]["subject_entity_id"] is not None  # Rohan is known
        assert resolved[0]["object_entity_id"] is None       # Python is not known
        assert resolved[0]["object_type"] == "literal"

    def test_empty_known_entities(self, known_entities: list[dict]) -> None:
        """When no known entities are provided, facts should pass through unchanged."""
        facts: list[dict[str, Any]] = [
            {
                "subject": "Rohan",
                "predicate": "works_at",
                "object": "ExampleOrg",
                "confidence": 0.97,
                "subject_type": "literal",
                "object_type": "literal",
                "subject_entity_id": None,
                "object_entity_id": None,
            },
        ]
        resolved = _resolve_fact_entities(facts, [])

        assert resolved[0]["subject"] == "Rohan"
        assert resolved[0]["subject_type"] == "literal"
        assert resolved[0]["subject_entity_id"] is None

    def test_multiple_facts_resolved(self, known_entities: list[dict]) -> None:
        """Multiple facts should all be resolved correctly."""
        facts: list[dict[str, Any]] = [
            {
                "subject": "Rohan",
                "predicate": "works_at",
                "object": "ExampleOrg",
                "confidence": 0.97,
                "subject_type": "literal",
                "object_type": "literal",
                "subject_entity_id": None,
                "object_entity_id": None,
            },
            {
                "subject": "Alice",
                "predicate": "works_at",
                "object": "Acme Corp",
                "confidence": 0.95,
                "subject_type": "literal",
                "object_type": "literal",
                "subject_entity_id": None,
                "object_entity_id": None,
            },
        ]
        resolved = _resolve_fact_entities(facts, known_entities)

        assert resolved[0]["subject_entity_id"] == known_entities[0]["id"]
        assert resolved[0]["object_entity_id"] == known_entities[2]["id"]
        assert resolved[1]["subject_entity_id"] == known_entities[4]["id"]
        assert resolved[1]["object_entity_id"] == known_entities[5]["id"]

    def test_partial_entity_name_match(self, known_entities: list[dict]) -> None:
        """Entity name should match even with some trailing context."""
        facts: list[dict[str, Any]] = [
            {
                "subject": "Rohan's expertise",
                "predicate": "includes",
                "object": "AI",
                "confidence": 0.7,
                "subject_type": "literal",
                "object_type": "literal",
                "subject_entity_id": None,
                "object_entity_id": None,
            },
        ]
        resolved = _resolve_fact_entities(facts, known_entities)

        # "Rohan" is in "Rohan's expertise" → should resolve
        assert resolved[0]["subject"] == "Rohan"
        assert resolved[0]["subject_type"] == "entity"
