"""LLM-as-judge evaluation for the LongMemEval benchmark.

Uses an LLM backend (e.g. OpenAI) to judge whether the retrieved context
contains the information needed to answer each LongMemEval question.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, Field

from core.llm import LLMStructuredOutputError, PromptCachingConfig

if TYPE_CHECKING:
    from core.llm import ChatResponse, LLMBackend

logger = logging.getLogger(__name__)

_NO_CACHE = PromptCachingConfig(enabled=False)
"""Explicit caching-disabled config for benchmark judge calls.

Benchmark processes never initialise ``Settings``, so leaving
``cache_config=None`` would make ``LLMBackend.chat()`` fall back to
``build_cache_config()`` → ``get_settings()`` → ``RuntimeError``.
Judge verdicts must not be cached anyway.
"""


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


RETRIEVAL_JUDGE_SYSTEM_PROMPT: str = (
    "You are an expert evaluator judging whether RETRIEVED CONTEXT contains "
    "the information needed to answer a question. This is a retrieval system "
    "under test, not a QA model — grade only the context, never a model's "
    "answer.\n\n"
    "Guidelines:\n"
    "- For factual questions, correct is true iff the retrieved context "
    "contains the ground-truth information (semantic match; minor phrasing "
    "differences are acceptable; numerical answers require exact match). "
    "Your reasoning MUST quote the supporting span from the context.\n"
    "- For abstention questions, correct is true iff the retrieved context "
    "does NOT contain the answer (the system correctly lacks the "
    "information). If the answer is present in the context, correct is "
    "false.\n\n"
    "Output ONLY valid JSON with the following keys:\n"
    '- "correct": a boolean (true if the verdict above holds, false '
    "otherwise)\n"
    '- "reasoning": a string explaining your judgement'
)


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════


async def evaluate_retrieval(
    backend: LLMBackend,
    question: str,
    expected_answer: str,
    context_text: str,
    is_abstention: bool,
    temperature: float = 0.0,
    **kwargs: Any,
) -> EvaluationResult:
    """Evaluate whether retrieved context contains the ground-truth answer.

    Uses the provided LLM backend as a judge, asking it to decide whether
    the retrieved context holds the information needed to answer a
    LongMemEval question. The judge grades the retrieval directly — no
    intermediate model answer is generated.

    For factual questions the context is correct iff it contains the
    ground-truth information (semantic match; minor phrasing differences
    are acceptable, but numerical answers require exact match). For
    abstention questions the context is correct iff it does NOT contain
    the answer (the system correctly lacks the information).

    Args:
        backend: An initialised LLM backend instance to use as the judge.
        question: The LongMemEval question text.
        expected_answer: The ground-truth answer from the benchmark dataset.
        context_text: The retrieved context text assembled for the question.
        is_abstention: Whether this question expects abstention. When True,
            ``correct`` is True iff ``context_text`` does NOT contain the
            answer; when False, ``correct`` is True iff ``context_text``
            DOES contain the ground-truth information.
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
            inside the backend's ``chat()`` method, or if the judge
            returned no parseable content at all.
    """
    question_type: str = "abstention" if is_abstention else "factual"

    user_prompt: str = (
        f"Question type: {question_type}\n\n"
        f"Question: {question}\n\n"
        f"Expected answer (ground truth): {expected_answer}\n\n"
        f"Retrieved context: {context_text}\n\n"
        "Decide whether the retrieved context contains the information needed "
        "to answer the question. "
        "Output ONLY valid JSON with 'correct' (bool) and 'reasoning' (string) keys."
    )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": RETRIEVAL_JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    response: ChatResponse = await backend.chat(
        messages=messages,
        response_model=EvaluationResult,
        temperature=temperature,
        cache_config=kwargs.pop("cache_config", _NO_CACHE),
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

    # No usable content from the judge — fail loudly so the caller can
    # mark this entry as judge infra failure rather than a retrieval miss.
    logger.error(
        "evaluator.no_valid_result",
        extra={
            "has_validated_data": response.validated_data is not None,
            "content_length": len(response.content) if response.content else 0,
            "model": response.model,
        },
    )
    raise LLMStructuredOutputError("Judge LLM returned no parseable result.")
