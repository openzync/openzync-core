"""Entity ontology injection eval — golden dataset regression test.

Measures the ontology-injected entity extraction prompt + LLM pipeline against
a curated golden dataset of 30 test cases across 5 domain ontologies.

Gates:
    G3.2a — Output entity types must be within the allowed set (F1 ≥ 80%).
    G3.2b — Expected entities must appear with correct names (recall ≥ 80%).

This test is slow (requires an LLM call per sample) and is skipped by default.
Run with::

    pytest tests/evals/ --run-eval -v
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from services.worker.prompt_renderer import render_prompt
from tests.evals.conftest import load_golden, load_prompt_text
from workers.tasks.extract_entities import _parse_entity_response

logger = logging.getLogger(__name__)

ACCURACY_THRESHOLD = 0.80
"""Minimum F1 score for type validity and entity name recall."""


@pytest.mark.eval
@pytest.mark.asyncio
async def test_entity_ontology_accuracy() -> None:
    """Measure entity extraction accuracy with ontology injection.

    For each golden test case:
    1. Render the ``extract_entities_v1`` prompt with the conversation
       and the domain-specific entity_types injected.
    2. Call the LLM backend.
    3. Parse the JSON response (reuses the existing
       ``_parse_entity_response`` parser).
    4. Validate every output entity type is in the allowed set.
    5. Check that expected entities appear with correct names.

    Raises:
        AssertionError: If accuracy is below the threshold.
    """
    from core.llm import resolve_backend

    dataset = load_golden("entity_ontology.json")
    llm = await resolve_backend(provider="openai")

    total = len(dataset)
    type_valid_count = 0
    entity_recall_total = 0
    entity_recall_correct = 0
    errors: list[dict] = []

    logger.info(
        "eval.entity_ontology.starting",
        extra={"samples": total, "threshold": ACCURACY_THRESHOLD},
    )

    template_text = load_prompt_text("extract_entities_v1")

    for i, item in enumerate(dataset):
        conversation = item["conversation"]
        entity_types: list[str] = item.get("entity_types", [])
        expected_entities: list[dict[str, str]] = item.get("expected_entities", [])

        try:
            prompt = await render_prompt(
                "entity_extraction",
                template_text=template_text,
                conversation=conversation,
                entity_types=entity_types if entity_types else None,
            )

            response = await llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are an entity extraction system. "
                            "Output ONLY valid JSON."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )

            parsed = _parse_entity_response(response.content)

            if parsed is None:
                errors.append(
                    {
                        "index": i,
                        "id": item["id"],
                        "error": "Failed to parse LLM response",
                        "raw": response.content[:200],
                    }
                )
                continue

            entities: list[dict[str, Any]] = parsed.get("entities", [])

            # ── Validate: all output entity types are in the allowed set ──
            allowed_types: set[str] = (
                set(entity_types) | {"Custom"}
                if entity_types
                else {"Person", "Organization", "Product", "Location", "Date", "Custom"}
            )
            all_types_valid = True
            for entity in entities:
                etype = entity.get("type", "Custom")
                if etype not in allowed_types:
                    all_types_valid = False
                    errors.append(
                        {
                            "index": i,
                            "id": item["id"],
                            "error": f"Invalid entity type '{etype}' for '{entity.get('name')}'",
                            "allowed": sorted(allowed_types),
                        }
                    )

            if all_types_valid:
                type_valid_count += 1

            # ── Validate: expected entities appear with correct names ─────
            entity_names_lower = {e.get("name", "").lower().strip() for e in entities}

            for expected in expected_entities:
                entity_recall_total += 1
                exp_name = expected["name"].lower().strip()
                if exp_name in entity_names_lower:
                    entity_recall_correct += 1
                else:
                    errors.append(
                        {
                            "index": i,
                            "id": item["id"],
                            "error": f"Expected entity '{expected['name']}' not found",
                            "got_names": [e.get("name") for e in entities],
                        }
                    )

        except Exception as exc:
            errors.append(
                {
                    "index": i,
                    "id": item["id"],
                    "error": str(exc),
                    "conversation": conversation[:80],
                }
            )

    # ── Compute metrics ─────────────────────────────────────────────────────
    type_accuracy = type_valid_count / total if total > 0 else 0.0
    entity_recall = (
        entity_recall_correct / entity_recall_total
        if entity_recall_total > 0
        else 0.0
    )

    logger.info(
        "eval.entity_ontology.completed",
        extra={
            "type_accuracy": round(type_accuracy, 4),
            "entity_recall": round(entity_recall, 4),
            "type_valid": type_valid_count,
            "total": total,
            "entity_correct": entity_recall_correct,
            "entity_total": entity_recall_total,
            "errors": len(errors),
        },
    )

    # Log first 10 errors for debugging
    for err in errors[:10]:
        logger.warning(
            "eval.entity_ontology.error",
            extra=err,
        )

    # Gate G3.2a: Output entity types must be within the allowed set
    assert type_accuracy >= ACCURACY_THRESHOLD, (
        f"Entity type validity {type_accuracy:.2%} ({type_valid_count}/{total}) "
        f"below threshold {ACCURACY_THRESHOLD:.0%}. "
        f"{len(errors)} samples had type validation errors."
    )

    # Gate G3.2b: Expected entities appear with correct names
    assert entity_recall >= ACCURACY_THRESHOLD, (
        f"Entity name recall {entity_recall:.2%} "
        f"({entity_recall_correct}/{entity_recall_total}) "
        f"below threshold {ACCURACY_THRESHOLD:.0%}."
    )
