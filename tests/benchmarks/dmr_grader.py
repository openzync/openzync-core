"""DMR (Dynamic Memory Retrieval) grader — LLM-as-judge evaluation.

Each sample in the MSC dataset has a gold answer string.  We ask
an LLM judge to determine whether the benchmark answer captures the
same information as the gold answer, on a binary (pass/fail) scale.

Two modes:
    1. **OpenZep-graded** — OpenZep's context is used to answer the question,
       then the LLM judge evaluates correctness vs. gold.
    2. **Baseline** — Full conversation context is injected as plain text
       (no OpenZep), then the LLM answers and the judge evaluates.

Grading rubric (paper section 5.3)::

    Does the generated answer contain the same factual information
    as the gold answer?  Answer "YES" or "NO".  The generated answer
    may use different wording; focus on factual equivalence.
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

DMR_JUDGE_MODEL: str = LLM_JUDGE_MODEL
"""Model used for the judge LLM (from llm.py config)."""

DMR_JUDGE_TEMPERATURE: float = LLM_JUDGE_TEMPERATURE
"""Deterministic judge — no creative interpretation."""


# ── Judge prompt ───────────────────────────────────────────────────────────────

DMR_JUDGE_PROMPT: str = """You are evaluating a question-answering system. Your task is to determine whether the **generated answer** contains the same factual information as the **gold answer**.

Gold answer: {gold_answer}

Generated answer: {generated_answer}

Does the generated answer contain the same factual information as the gold answer? Respond with a JSON object with keys "decision" ("YES" or "NO") and "reason" (short explanation).
"""


# ── Public API ─────────────────────────────────────────────────────────────────


class DMRGrader:
    """LLM-as-judge grader for the DMR benchmark.

    Args:
        openai_client: An ``AsyncOpenAI`` instance.
        model: The OpenAI model ID for the judge.
        temperature: Judge temperature (default 0.0 for determinism).
    """

    def __init__(
        self,
        openai_client: AsyncOpenAI,
        model: str = DMR_JUDGE_MODEL,
        temperature: float = DMR_JUDGE_TEMPERATURE,
    ) -> None:
        self._client = openai_client
        self._model = model
        self._temperature = temperature

    async def grade(
        self,
        gold_answer: str,
        generated_answer: str,
        *,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """Grade a single (gold, generated) pair.

        Args:
            gold_answer: The reference answer from the dataset.
            generated_answer: The answer produced by the system under test.
            max_retries: Retries on malformed judge responses.

        Returns:
            Dict with keys:
                - ``decision``: ``"YES"`` or ``"NO"``.
                - ``reason``: Judge's explanation.
                - ``latency_s``: Judge response time.
                - ``error``: Error message if judge failed.
        """
        prompt = DMR_JUDGE_PROMPT.format(
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
                # Try JSON mode first; fall back to plain text for models
                # (like OpenRouter free) that don't support structured output.
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
                }

            except Exception as exc:
                logger.warning(
                    "dmr_judge.retry",
                    extra={"attempt": attempt + 1, "error": str(exc)},
                )
                if attempt == max_retries - 1:
                    return {
                        "decision": "ERROR",
                        "reason": "",
                        "latency_s": round(time.monotonic() - start, 2),
                        "error": str(exc),
                    }

    async def grade_batch(
        self,
        items: list[tuple[str, str]],
        *,
        concurrency: int = 10,
    ) -> list[dict[str, Any]]:
        """Grade a batch of (gold, generated) pairs concurrently.

        Args:
            items: List of ``(gold_answer, generated_answer)`` tuples.
            concurrency: Max concurrent judge calls.

        Returns:
            List of result dicts in the same order as ``items``.
        """
        import asyncio

        sem = asyncio.Semaphore(concurrency)

        async def _grade_one(gold: str, generated: str) -> dict[str, Any]:
            async with sem:
                return await self.grade(gold, generated)

        tasks = [_grade_one(g, a) for g, a in items]
        results = await asyncio.gather(*tasks)
        return results


# ── JSON extraction helpers ────────────────────────────────────────────────────


def _parse_json_response(text: str) -> dict[str, Any]:
    """Extract a JSON object from LLM response text, handling markdown fences."""
    # Try direct parse first
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown code fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    # Last resort: find the first { ... } block
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse JSON from response: {text[:200]}")
