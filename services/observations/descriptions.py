"""Observation descriptions — LLM-optional observation text."""

from __future__ import annotations

import structlog

from services.observations.patterns import (
    BehavioralPattern,
    CoOccurrencePattern,
    TemporalGapPattern,
)

logger = structlog.get_logger(__name__)


class DescriptionMixin:
    """Composed into :class:`ObservationService`."""

    # ═══════════════════════════════════════════════════════════════════════════
    # Description generation (LLM-optional)
    # ═══════════════════════════════════════════════════════════════════════════
    def build_co_occurrence_description(
        self,
        pattern: CoOccurrencePattern,
        llm_content: str | None = None,
    ) -> str:
        """Generate a description for a co-occurrence observation.

        If ``llm_content`` is provided (from an LLM call), it is used.
        Otherwise, a template-based description is built.

        Args:
            pattern: The detected co-occurrence pattern.
            llm_content: Optional LLM-generated content.

        Returns:
            A natural-language description string.
        """
        if llm_content:
            return llm_content
        ratio = round(pattern.co_count / pattern.total_episodes * 100)
        return (
            f"'{pattern.entity_a_name}' appears alongside "
            f"'{pattern.entity_b_name}' in {pattern.co_count} out of "
            f"{pattern.total_episodes} episodes ({ratio}% co-occurrence rate)."
        )

    def build_temporal_description(
        self,
        pattern: TemporalGapPattern,
        llm_content: str | None = None,
    ) -> str:
        """Generate a description for a temporal pattern observation.

        Args:
            pattern: The detected temporal gap pattern.
            llm_content: Optional LLM-generated content.

        Returns:
            A natural-language description string.
        """
        if llm_content:
            return llm_content

        pattern_labels = {
            "periodic": "regular intervals",
            "widening": "increasing gaps",
            "narrowing": "decreasing gaps",
            "burst": "clustered appearances with long gaps between",
            "irregular": "irregular intervals",
        }
        label = pattern_labels.get(pattern.pattern_type, pattern.pattern_type)

        return (
            f"'{pattern.entity_name}' appears at {label} "
            f"(mean gap: {pattern.mean_gap_hours:.1f}h, "
            f"span: {pattern.span_days:.1f}d, "
            f"{pattern.appearance_count} appearances)."
        )

    def build_behavioral_description(
        self,
        pattern: BehavioralPattern,
        llm_content: str | None = None,
    ) -> str:
        """Generate a description for a behavioral pattern observation.

        Args:
            pattern: The detected behavioral pattern.
            llm_content: Optional LLM-generated content.

        Returns:
            A natural-language description string.
        """
        if llm_content:
            return llm_content

        if not pattern.frequent_predicates:
            return (
                f"'{pattern.entity_name}' has no notable behavior patterns "
                f"detected ({pattern.total_facts} facts analyzed)."
            )

        top_pred, top_count = next(iter(pattern.frequent_predicates.items()))
        return (
            f"'{pattern.entity_name}' most frequently exhibits "
            f"the predicate '{top_pred}' ({top_count} out of "
            f"{pattern.total_facts} facts). "
            f"Additional predicates: "
            f"{', '.join(f'{p}({c})' for p, c in list(pattern.frequent_predicates.items())[1:4])}."
        )
