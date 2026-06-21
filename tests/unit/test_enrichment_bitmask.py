"""Unit tests for the enrichment_status bitmask (G1.6).

Verifies that the bitmask constants in ``workers/tasks/base.py`` correctly
map to distinct bits, compose properly, and can be checked with ``&``
operations.

Exit criterion G1.6:
    ``enrichment_status`` bitmask correctly tracks progress.
"""

from __future__ import annotations

import pytest

from workers.tasks.base import (
    ENRICHMENT_CLASSIFICATION,
    ENRICHMENT_EMBEDDING,
    ENRICHMENT_ENTITIES,
    ENRICHMENT_FACTS,
    ENRICHMENT_STRUCTURED_EXTRACTION,
    ENRICHMENT_ENTITY_LINKS,
)


class TestEnrichmentBitmask:
    """Bitmask constant correctness and composition."""

    # ── Individual bits are correct powers of 2 ───────────────────────────────

    def test_entities_is_bit_0(self) -> None:
        """ENRICHMENT_ENTITIES must be ``1 << 0 = 1``."""
        assert ENRICHMENT_ENTITIES == 1

    def test_embedding_is_bit_1(self) -> None:
        """ENRICHMENT_EMBEDDING must be ``1 << 1 = 2``."""
        assert ENRICHMENT_EMBEDDING == 2

    def test_facts_is_bit_2(self) -> None:
        """ENRICHMENT_FACTS must be ``1 << 2 = 4``."""
        assert ENRICHMENT_FACTS == 4

    def test_entity_links_is_bit_3(self) -> None:
        """ENRICHMENT_ENTITY_LINKS must be ``1 << 3 = 8``."""
        assert ENRICHMENT_ENTITY_LINKS == 8

    def test_classification_is_bit_4(self) -> None:
        """ENRICHMENT_CLASSIFICATION must be ``1 << 4 = 16``."""
        assert ENRICHMENT_CLASSIFICATION == 16

    def test_structured_extraction_is_bit_5(self) -> None:
        """ENRICHMENT_STRUCTURED_EXTRACTION must be ``1 << 5 = 32``."""
        assert ENRICHMENT_STRUCTURED_EXTRACTION == 32

    # ── No two constants share the same bit ──────────────────────────────────

    def test_all_bits_are_distinct(self) -> None:
        """Every enrichment constant must occupy a unique bit position.

        If any two constants share the same bit, ORing them together
        will not increase the population count.
        """
        all_bits = ENRICHMENT_ENTITIES | ENRICHMENT_EMBEDDING | ENRICHMENT_FACTS
        all_bits |= ENRICHMENT_ENTITY_LINKS | ENRICHMENT_CLASSIFICATION
        all_bits |= ENRICHMENT_STRUCTURED_EXTRACTION

        # With 6 distinct bits the combined mask must have exactly 6 bits set
        assert all_bits.bit_count() == 6, (
            f"Expected 6 distinct bits, got {all_bits.bit_count()}. "
            "Two or more constants overlap."
        )

    # ── Bitmask composition and checking ─────────────────────────────────────

    def test_single_bit_check(self) -> None:
        """A status containing only a single bit must match that bit."""
        status = ENRICHMENT_ENTITIES
        assert status & ENRICHMENT_ENTITIES != 0
        assert status & ENRICHMENT_EMBEDDING == 0
        assert status & ENRICHMENT_FACTS == 0
        assert status & ENRICHMENT_ENTITY_LINKS == 0
        assert status & ENRICHMENT_CLASSIFICATION == 0
        assert status & ENRICHMENT_STRUCTURED_EXTRACTION == 0

    def test_multi_bit_composition(self) -> None:
        """Multiple bits can be ORed together and each is independently
        checkable.
        """
        status = ENRICHMENT_ENTITIES | ENRICHMENT_EMBEDDING | ENRICHMENT_FACTS
        assert status & ENRICHMENT_ENTITIES != 0
        assert status & ENRICHMENT_EMBEDDING != 0
        assert status & ENRICHMENT_FACTS != 0
        # Ensure bits we did NOT set are still 0
        assert status & ENRICHMENT_ENTITY_LINKS == 0
        assert status & ENRICHMENT_CLASSIFICATION == 0
        assert status & ENRICHMENT_STRUCTURED_EXTRACTION == 0

    def test_all_bits_set(self) -> None:
        """When all 6 bits are set, every check must pass."""
        all_bits = (
            ENRICHMENT_ENTITIES
            | ENRICHMENT_EMBEDDING
            | ENRICHMENT_FACTS
            | ENRICHMENT_ENTITY_LINKS
            | ENRICHMENT_CLASSIFICATION
            | ENRICHMENT_STRUCTURED_EXTRACTION
        )
        assert all_bits & ENRICHMENT_ENTITIES != 0
        assert all_bits & ENRICHMENT_EMBEDDING != 0
        assert all_bits & ENRICHMENT_FACTS != 0
        assert all_bits & ENRICHMENT_ENTITY_LINKS != 0
        assert all_bits & ENRICHMENT_CLASSIFICATION != 0
        assert all_bits & ENRICHMENT_STRUCTURED_EXTRACTION != 0

    # ── Worker progression pattern ──────────────────────────────────────────

    def test_progression_accumulates(self) -> None:
        """Simulate the real worker progression: each step ORs its bit.

        After step 1 (entities):   0b000001
        After step 2 (embedding):  0b000011
        After step 3 (facts):      0b000111
        ...
        After all 6 steps:         0b111111 = 63
        """
        status = 0

        # Worker 1 completes
        status |= ENRICHMENT_ENTITIES
        assert status == 0b000001

        # Worker 2 completes
        status |= ENRICHMENT_EMBEDDING
        assert status == 0b000011

        # Worker 3 completes
        status |= ENRICHMENT_FACTS
        assert status == 0b000111

        # Worker 4 completes
        status |= ENRICHMENT_ENTITY_LINKS
        assert status == 0b001111

        # Worker 5 completes
        status |= ENRICHMENT_CLASSIFICATION
        assert status == 0b011111

        # Worker 6 completes
        status |= ENRICHMENT_STRUCTURED_EXTRACTION
        assert status == 0b111111  # == 63

    def test_already_done_check(self) -> None:
        """The pattern ``status & BIT != 0`` correctly identifies already-done
        workers.  This is the exact check used in every worker's ``should_skip``
        guard.
        """
        status = ENRICHMENT_ENTITIES | ENRICHMENT_FACTS

        # Should skip: entities and facts are already set
        assert status & ENRICHMENT_ENTITIES != 0
        assert status & ENRICHMENT_FACTS != 0

        # Should NOT skip: embedding, sync_graph, classification,
        # structured_extraction are still 0
        assert status & ENRICHMENT_EMBEDDING == 0
        assert status & ENRICHMENT_ENTITY_LINKS == 0
        assert status & ENRICHMENT_CLASSIFICATION == 0
        assert status & ENRICHMENT_STRUCTURED_EXTRACTION == 0

    def test_full_status_value(self) -> None:
        """When all 6 enrichment workers have completed, the status value
        must be 63.
        """
        expected = (
            ENRICHMENT_ENTITIES
            | ENRICHMENT_EMBEDDING
            | ENRICHMENT_FACTS
            | ENRICHMENT_ENTITY_LINKS
            | ENRICHMENT_CLASSIFICATION
            | ENRICHMENT_STRUCTURED_EXTRACTION
        )
        assert expected == 63, f"All 6 bits set should equal 63, got {expected}"
