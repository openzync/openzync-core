"""Classification accuracy eval — golden dataset regression test.

Measures the classification prompt + LLM pipeline against a curated golden
dataset of 50+ conversation turns with expected labels.

Gate: G3.1 — Accuracy must be ≥85%.

Migrated from the deleted ``classify_dialog_v1`` / ``classification``
single-task prompt to the combined ``enrich_episode`` prompt (startup cache
+ ``build_enrichment_prompt``); assertions target the ``classification``
section of ``CombinedLLMOutput``. Threshold unchanged (was 0.85); lower it
if an eval run shows the combined prompt scores below the single-task
baseline.

This test is slow (requires an LLM call per sample) and is skipped by default.
Run with::

    pytest tests/evals/ --run-eval -v
"""

from __future__ import annotations

import logging

import pytest

from schemas.llm_outputs import CombinedLLMOutput
from services.worker.prompt_renderer import build_enrichment_prompt, render_prompt
from tests.evals.conftest import (
    evaluate_classification_match,
    load_golden,
)

logger = logging.getLogger(__name__)

ACCURACY_THRESHOLD = 0.85


@pytest.mark.eval
@pytest.mark.asyncio
async def test_classification_accuracy() -> None:
    """Measure classification accuracy against the golden dataset.

    Requires a running LLM backend (Ollama, OpenAI, etc.) configured via
    environment variables.
    """
    from core.llm import resolve_backend

    dataset = load_golden("classification.json")
    llm = await resolve_backend(provider="openai")

    total = len(dataset)
    correct = 0
    errors: list[dict] = []

    logger.info(
        "eval.classification.starting",
        extra={"samples": total, "threshold": ACCURACY_THRESHOLD},
    )

    for i, item in enumerate(dataset):
        conversation = item["conversation"]
        tv = item["template_vars"]

        try:
            system_prompt, ctx = await render_prompt(
                "enrich_episode",
                conversation=conversation,
                intent_labels=tv["intent_labels"],
                emotion_labels=tv["emotion_labels"],
                valence_options=tv["valence_options"],
                arousal_options=tv["arousal_options"],
                return_context=True,
            )
            prompt = build_enrichment_prompt(system_prompt, ctx)

            response = await llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a dialog classification system. "
                            "Output ONLY valid JSON."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=300,
            )

            try:
                parsed = CombinedLLMOutput.model_validate_json(
                    response.content
                ).classification.model_dump()
            except Exception:
                errors.append(
                    {
                        "index": i,
                        "id": item["id"],
                        "error": "Failed to parse LLM response",
                        "raw": response.content[:200],
                    }
                )
                continue

            is_match, detail = evaluate_classification_match(
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
                        "got": {
                            "intent": parsed.get("intent"),
                            "emotion": parsed.get("emotion"),
                            "valence": parsed.get("valence"),
                            "arousal": parsed.get("arousal"),
                        },
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
        "eval.classification.completed",
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
            "eval.classification.error",
            extra=err,
        )

    assert accuracy >= ACCURACY_THRESHOLD, (
        f"Classification accuracy {accuracy:.2%} ({correct}/{total}) "
        f"below threshold {ACCURACY_THRESHOLD:.0%}. "
        f"{len(errors)} samples had errors (see warnings for details)."
    )
