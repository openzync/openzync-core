"""Structured extraction accuracy eval — golden dataset regression test.

Measures the structured extraction prompt + LLM pipeline against a curated
golden dataset of 20+ conversation turns with expected extracted data.

Gate: G3.2 — Accuracy must be ≥85%.

This test is slow (requires an LLM call per sample) and is skipped by default.
Run with::

    pytest tests/evals/ --run-eval -v
"""

from __future__ import annotations

import logging

import pytest

from services.worker.prompt_renderer import render_prompt
from tests.evals.conftest import (
    evaluate_structured_match,
    load_golden,
    load_prompt_text,
    parse_structured_response,
)

logger = logging.getLogger(__name__)

ACCURACY_THRESHOLD = 0.85


def _schemas_to_template_vars(schemas: list[dict]) -> dict:
    """Convert raw schema list to template variables.

    The prompt template expects ``schemas`` as a list of dicts with
    ``name`` and ``json_schema`` keys.
    """
    return {"schemas": schemas}


@pytest.mark.eval
@pytest.mark.asyncio
async def test_structured_extraction_accuracy() -> None:
    """Measure structured extraction accuracy against the golden dataset.

    Requires a running LLM backend (Ollama, OpenAI, etc.) configured via
    environment variables.
    """
    from core.llm import resolve_backend

    dataset = load_golden("structured_extraction.json")
    llm = await resolve_backend(provider="openai")

    total = len(dataset)
    correct = 0
    errors: list[dict] = []

    logger.info(
        "eval.structured_extraction.starting",
        extra={"samples": total, "threshold": ACCURACY_THRESHOLD},
    )

    template_text = load_prompt_text("extract_structured_v1")

    for i, item in enumerate(dataset):
        conversation = item["conversation"]
        schemas = item["schemas"]
        template_vars = {"schemas": schemas, "conversation": conversation}

        try:
            prompt = await render_prompt(
                "structured_extraction",
                template_text=template_text,
                **template_vars,
            )

            response = await llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a structured data extraction system. "
                            "Output ONLY valid JSON."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=2000,
            )

            parsed = parse_structured_response(response.content)

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

            is_match, detail = evaluate_structured_match(
                parsed, item["expected"]
            )
            if is_match:
                correct += 1
            else:
                errors.append(
                    {
                        "index": i,
                        "id": item["id"],
                        "conversation": conversation[:80],
                        "expected": item["expected"],
                        "got": parsed,
                        "detail": detail,
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

    accuracy = correct / total if total > 0 else 0.0
    logger.info(
        "eval.structured_extraction.completed",
        extra={
            "accuracy": round(accuracy, 4),
            "correct": correct,
            "total": total,
            "errors": len(errors),
        },
    )

    # Log first 10 errors for debugging
    for err in errors[:10]:
        logger.warning(
            "eval.structured_extraction.error",
            extra=err,
        )

    assert accuracy >= ACCURACY_THRESHOLD, (
        f"Structured extraction accuracy {accuracy:.2%} ({correct}/{total}) "
        f"below threshold {ACCURACY_THRESHOLD:.0%}. "
        f"{len(errors)} samples had errors (see warnings for details)."
    )
