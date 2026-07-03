"""LLM-as-judge evaluation for the LongMemEval benchmark.

Uses an LLM backend (e.g. OpenRouter) to judge whether a model's answer
matches the expected ground truth for each LongMemEval question.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from core.llm import ChatResponse, LLMBackend

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════════


class EvaluationResult(BaseModel):
    """Result of an LLM-as-judge evaluation for LongMemEval.

    The judge LLM determines whether a model's answer matches the expected
    ground truth and provides reasoning for its decision.
    """

    correct: bool = Field(
        ...,
        description="Whether the model's answer matches the ground truth",
    )
    reasoning: str = Field(
        ...,
        description="Judge's explanation for the verdict",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════


EVALUATOR_SYSTEM_PROMPT: str = (
    "You are an expert evaluator judging the correctness of a model's answer "
    "against a provided ground truth. Be strict but fair — minor phrasing "
    "differences are acceptable if the meaning matches.\n\n"
    "Guidelines:\n"
    "- For numerical answers, exact match is required.\n"
    "- For abstention questions, the model should clearly indicate it does not "
    "know the answer or that the information is not available.\n"
    '- If the ground truth is "None" or "N/A", the model should output '
    "something semantically equivalent.\n"
    "- Consider the answer correct if it captures the essential information "
    "from the ground truth.\n\n"
    "Output ONLY valid JSON with the following keys:\n"
    '- "correct": a boolean (true if the answer matches the ground truth, '
    "false otherwise)\n"
    '- "reasoning": a string explaining your judgement'
)


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════


async def evaluate_answer(
    backend: LLMBackend,
    question: str,
    expected_answer: str,
    model_answer: str,
    is_abstention: bool,
    temperature: float = 0.0,
    **kwargs: Any,
) -> EvaluationResult:
    """Evaluate whether a model's answer matches the expected ground truth.

    Uses the provided LLM backend as a judge, asking it to compare the model's
    answer against the ground truth for a LongMemEval question.

    The judge is instructed to be strict but fair — minor phrasing differences
    are acceptable if the meaning matches, but numerical answers require exact
    match. For abstention questions, the model should indicate it does not know.

    Args:
        backend: An initialised LLM backend instance to use as the judge.
        question: The LongMemEval question text.
        expected_answer: The ground-truth answer from the benchmark dataset.
        model_answer: The answer produced by the model under evaluation.
        is_abstention: Whether this question expects the model to abstain
            (i.e. indicate it does not know the answer or the information is
            not available).
        temperature: LLM sampling temperature for the judge. Defaults to 0.0
            for deterministic, reproducible evaluation.
        **kwargs: Additional keyword arguments forwarded to ``backend.chat()``
            (e.g. ``max_tokens``).

    Returns:
        An ``EvaluationResult`` with the verdict (``correct``) and the
        judge's reasoning (``reasoning``).

    Raises:
        LLMStructuredOutputError: If the judge's response cannot be parsed
            into an ``EvaluationResult`` after exhausting validation retries
            inside the backend's ``chat()`` method.
    """
    question_type: str = "abstention" if is_abstention else "factual"

    user_prompt: str = (
        f"Question type: {question_type}\n\n"
        f"Question: {question}\n\n"
        f"Expected answer (ground truth): {expected_answer}\n\n"
        f"Model's answer: {model_answer}\n\n"
        "Evaluate the model's answer against the ground truth. "
        "Output ONLY valid JSON with 'correct' (bool) and 'reasoning' (string) keys."
    )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": EVALUATOR_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    response: ChatResponse = await backend.chat(
        messages=messages,
        response_model=EvaluationResult,
        temperature=temperature,
        **kwargs,
    )

    # Fast path: backend's structured-output validation succeeded.
    if response.validated_data is not None:
        return cast("EvaluationResult", response.validated_data)

    # Fallback: backend returned content but validated_data is None.
    # Attempt manual JSON parsing as a defence-in-depth measure.
    if response.content:
        try:
            parsed: dict[str, Any] = json.loads(response.content)
            result = EvaluationResult(
                correct=bool(parsed.get("correct", False)),
                reasoning=str(parsed.get("reasoning", "")),
            )
            logger.warning(
                "evaluator.fallback_parse_succeeded",
                extra={
                    "content_preview": response.content[:200],
                    "model": response.model,
                },
            )
            return result
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning(
                "evaluator.fallback_parse_failed",
                extra={
                    "content_preview": response.content[:300],
                    "error": str(exc),
                    "model": response.model,
                },
            )

    # No usable content from the judge — return a safe default.
    logger.error(
        "evaluator.no_valid_result",
        extra={
            "has_validated_data": response.validated_data is not None,
            "content_length": len(response.content) if response.content else 0,
            "model": response.model,
        },
    )
    return EvaluationResult(
        correct=False,
        reasoning=(
            "Judge LLM returned no parseable result. "
            "Defaulting to incorrect."
        ),
    )
