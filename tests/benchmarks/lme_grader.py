"""LongMemEval grader — per-question-type evaluation rubrics.

LongMemEval has 6 question types, each with its own grading rubric:

1. **temporal-reasoning** — Tests ability to identify the time or sequence
   of events.  Answers are correct if within 1 day for dates, or the right
   ordering for sequences.
2. **knowledge-update** — Tests ability to track evolving facts.  Either
   the *current* value or *both old+new* are accepted (per paper).
3. **single-session-preference** — Preferences expressed in one session.
   Must match the stated preference exactly.
4. **cross-session-preference** — Preferences requiring information spread
   across sessions.  Must match the consolidated preference.
5. **cross-session-reasoning** — Multi-hop reasoning across sessions.
    Rubric: all named entities and relationships in the gold answer must
    appear in the generated answer.
6. **self-contained** — Non-temporal questions answerable from a single
    session.  Exact match to the gold answer (with lenience for wording).

Usage::

    grader = LongMemEvalGrader(openai_client)
    result = await grader.grade("temporal-reasoning", gold_answer, generated_answer)
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from openai import AsyncOpenAI

from tests.benchmarks.llm import LLM_JUDGE_MODEL, LLM_JUDGE_TEMPERATURE

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

LME_JUDGE_MODEL: str = LLM_JUDGE_MODEL
LME_JUDGE_TEMPERATURE: float = LLM_JUDGE_TEMPERATURE

# ── Per-type rubrics ───────────────────────────────────────────────────────────

RUBRICS: dict[str, str] = {
    "temporal-reasoning": """You are evaluating answers for temporal-reasoning questions. The question asks about when or in what order something happened.

Gold answer: {gold_answer}
Generated answer: {generated_answer}

The generated answer is correct if it identifies the same time/date (within 1 day for dates) or the same sequence ordering. Respond with a JSON object with keys "decision" ("YES" or "NO") and "reason" (short explanation).""",
    "knowledge-update": """You are evaluating answers for knowledge-update questions. These test whether the system tracks evolving facts — the answer may have changed over multiple sessions.

Gold answer: {gold_answer}
Generated answer: {generated_answer}

The generated answer is correct if it captures the *current* value of the fact, OR correctly describes both the old and updated value (showing awareness of the update). Respond with a JSON object with keys "decision" ("YES" or "NO") and "reason".""",
    "single-session-preference": """You are evaluating answers for single-session-preference questions. These test whether the system remembers a user's stated preference from one conversation.

Gold answer: {gold_answer}
Generated answer: {generated_answer}

The generated answer is correct if it matches the stated preference exactly. Respond with a JSON object with keys "decision" ("YES" or "NO") and "reason".""",
    "cross-session-preference": """You are evaluating answers for cross-session-preference questions. These test whether the system can consolidate preferences expressed across different conversations.

Gold answer: {gold_answer}
Generated answer: {generated_answer}

The generated answer is correct if it captures the consolidated preference (most recent or most consistent across sessions). Respond with a JSON object with keys "decision" ("YES" or "NO") and "reason".""",
    "cross-session-reasoning": """You are evaluating answers for cross-session-reasoning questions. These require multi-hop reasoning across multiple sessions.

Gold answer: {gold_answer}
Generated answer: {generated_answer}

The generated answer is correct if all named entities and relationships mentioned in the gold answer appear (possibly rephrased) in the generated answer. Respond with a JSON object with keys "decision" ("YES" or "NO") and "reason".""",
    "self-contained": """You are evaluating answers for self-contained questions. These are non-temporal questions answerable from a single session.

Gold answer: {gold_answer}
Generated answer: {generated_answer}

The generated answer is correct if it contains the same factual information as the gold answer (lenient with wording). Respond with a JSON object with keys "decision" ("YES" or "NO") and "reason".""",
}


class LongMemEvalGrader:
    """Per-type LLM-as-judge grader for LongMemEval.

    Args:
        openai_client: An ``AsyncOpenAI`` instance.
        model: Judge model ID.
        temperature: Judge temperature.
    """

    def __init__(
        self,
        openai_client: AsyncOpenAI,
        model: str = LME_JUDGE_MODEL,
        temperature: float = LME_JUDGE_TEMPERATURE,
    ) -> None:
        self._client = openai_client
        self._model = model
        self._temperature = temperature

    async def grade(
        self,
        question_type: str,
        gold_answer: str,
        generated_answer: str,
        *,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """Grade a single LongMemEval sample by question type.

        Args:
            question_type: One of the 6 types (e.g. ``"temporal-reasoning"``).
            gold_answer: Reference answer from the dataset.
            generated_answer: Answer produced by the system under test.
            max_retries: Retries on malformed judge JSON.

        Returns:
            Dict with ``decision``, ``reason``, ``latency_s``, ``error``.
        """
        rubric = RUBRICS.get(question_type)
        if rubric is None:
            raise ValueError(
                f"Unknown question type: {question_type!r}. "
                f"Valid types: {list(RUBRICS.keys())}"
            )

        prompt = rubric.format(
            gold_answer=gold_answer,
            generated_answer=generated_answer,
        )

        for attempt in range(max_retries):
            start = time.monotonic()
            try:
                kwargs: dict[str, Any] = dict(
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=self._temperature,
                )
                # Try JSON mode first; fall back for models without structured output support.
                if attempt == 0:
                    kwargs["response_format"] = {"type": "json_object"}

                response = await self._client.chat.completions.create(**kwargs)
                latency = time.monotonic() - start
                text = response.choices[0].message.content or ""

                result = _parse_json_response(text)
                decision = result.get("decision", "").strip().upper()
                if decision not in ("YES", "NO"):
                    raise ValueError(f"Invalid decision: {decision}")

                return {
                    "decision": decision,
                    "reason": result.get("reason", ""),
                    "latency_s": round(latency, 2),
                    "error": None,
                    "question_type": question_type,
                }

            except Exception as exc:
                logger.warning(
                    "lme_judge.retry",
                    extra={
                        "question_type": question_type,
                        "attempt": attempt + 1,
                        "error": str(exc),
                    },
                )
                if attempt == max_retries - 1:
                    return {
                        "decision": "ERROR",
                        "reason": "",
                        "latency_s": round(time.monotonic() - start, 2),
                        "error": str(exc),
                        "question_type": question_type,
                    }

    async def grade_batch(
        self,
        items: list[tuple[str, str, str]],
        *,
        concurrency: int = 10,
    ) -> list[dict[str, Any]]:
        """Grade a batch concurrently.

        Args:
            items: List of ``(question_type, gold_answer, generated_answer)``.
            concurrency: Max concurrent judge calls.

        Returns:
            List of result dicts.
        """
        import asyncio

        sem = asyncio.Semaphore(concurrency)

        async def _grade_one(
            qtype: str, gold: str, generated: str
        ) -> dict[str, Any]:
            async with sem:
                return await self.grade(qtype, gold, generated)

        tasks = [_grade_one(q, g, a) for q, g, a in items]
        return await asyncio.gather(*tasks)


# ── JSON extraction helpers ────────────────────────────────────────────────────


def _parse_json_response(text: str) -> dict[str, Any]:
    """Extract a JSON object from LLM response text, handling markdown fences."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse JSON from response: {text[:200]}")
