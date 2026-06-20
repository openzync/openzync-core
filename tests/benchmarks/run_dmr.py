"""DMR benchmark runner — 500 multi-session conversations.

Usage::

    # Prerequisites
    python -m tests.benchmarks.setup                     # one-time
    export LLM_API_KEY="..."                            # optional, OpenRouter free models may not need it
    export LLM_MODEL="openai/gpt-oss-120b:free"         # default, or override

    # Run benchmark
    python -m tests.benchmarks.run_dmr                   # full run
    python -m tests.benchmarks.run_dmr --samples 10      # quick smoke test (10 samples)
    python -m tests.benchmarks.run_dmr --baseline-only   # skip OpenZep, baseline only
    python -m tests.benchmarks.run_dmr --oz-only          # skip baseline

Output is written to ``tests/benchmarks/results/dmr/`` as JSON and Markdown.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

# ── Add project root to path for imports ───────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.benchmarks.context_composer import compose_prompt
from tests.benchmarks.dmr_grader import DMRGrader
from tests.benchmarks.llm import (
    LLM_MODEL,
    LLM_DEFAULT_TEMPERATURE,
    create_llm_client,
)
from tests.benchmarks.oz_adapter import OpenZepBenchAdapter

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

DMR_DATASET_NAME: str = "MemGPT/MSC-Self-Instruct"
"""HuggingFace dataset for DMR evaluation."""

ANSWER_MODEL: str = LLM_MODEL
"""Model used to *answer* questions given context (from llm.py)."""

ANSWER_TEMPERATURE: float = LLM_DEFAULT_TEMPERATURE
"""Default temperature for answer generation."""

ENRICHMENT_TIMEOUT_S: float = 300.0
CONCURRENCY: int = 5
"""Max concurrent conversation evaluations."""

RESULTS_DIR = Path(__file__).resolve().parent / "results" / "dmr"

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Data structures                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


@dataclass
class DMRSample:
    """A single MSC conversation sample."""

    conversation_id: str
    sessions: list[list[dict[str, str]]]  # 5 sessions, each a list of messages
    gold_answer: str
    question: str


@dataclass
class DMRResult:
    """Result for a single conversation."""

    conversation_id: str
    oz_answer: str | None = None
    oz_judge: dict[str, Any] | None = None  # from DMRGrader.grade()
    oz_latency_s: float = 0.0
    oz_context_chars: int = 0
    baseline_answer: str | None = None
    baseline_judge: dict[str, Any] | None = None
    baseline_latency_s: float = 0.0
    error: str | None = None


@dataclass
class DMRSummary:
    """Aggregated results."""

    total: int = 0
    oz_correct: int = 0
    oz_errors: int = 0
    oz_avg_latency_s: float = 0.0
    baseline_correct: int = 0
    baseline_errors: int = 0
    baseline_avg_latency_s: float = 0.0
    results: list[DMRResult] = field(default_factory=list)
    elapsed_s: float = 0.0


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Dataset loading                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


async def load_dmr_dataset(max_samples: int | None = None) -> list[DMRSample]:
    """Load the MSC dataset from HuggingFace.

    Each row is one conversation with:
        - ``dialog``: current session (list of ``{"text": ..., "id": "Speaker 1|2"}``)
        - ``previous_dialogs``: list of prior session dicts (each has ``dialog`` list)
        - ``self_instruct``: ``{"A": <gold_answer>, "B": <question>}``
    """
    try:
        from datasets import load_dataset
    except ImportError:
        raise ImportError(
            "Missing `datasets` package — install it with: pip install datasets"
        )

    logger.info("dmr.load_dataset", extra={"dataset_name": DMR_DATASET_NAME})
    dataset = load_dataset(DMR_DATASET_NAME, split="train")
    logger.info("dmr.dataset_loaded", extra={"row_count": len(dataset)})

    def _to_standard(messages: list) -> list[dict[str, str]]:
        """Convert MSC message format to {"role": ..., "content": ...}."""
        result: list[dict[str, str]] = []
        for msg in messages:
            text = msg.get("text", "") if isinstance(msg, dict) else str(msg)
            speaker = msg.get("id", "Speaker 1") if isinstance(msg, dict) else "Speaker 1"
            role = "user" if "Speaker 2" in speaker else "assistant"
            result.append({"role": role, "content": text})
        return result

    samples: list[DMRSample] = []
    for idx, row in enumerate(dataset):
        # Extract self_instruct Q&A
        self_instruct = row.get("self_instruct", {})
        question = self_instruct.get("B", "")
        gold_answer = self_instruct.get("A", "")

        if not question or not gold_answer:
            # Skip rows without clear Q&A
            continue

        # Build sessions: previous_dialogs + current dialog
        sessions: list[list[dict[str, str]]] = []

        # Previous dialogs are the history (sessions 1-4 of 5)
        prev_dialogs = row.get("previous_dialogs", [])
        for prev in prev_dialogs:
            raw_msgs = prev.get("dialog", [])
            sessions.append(_to_standard(raw_msgs))

        # Current dialog is the latest session (session 5 of 5)
        current_dialog = row.get("dialog", [])
        sessions.append(_to_standard(current_dialog))

        if not sessions or not any(sessions):
            continue

        samples.append(
            DMRSample(
                conversation_id=str(idx),
                sessions=sessions,
                gold_answer=gold_answer,
                question=question,
            )
        )

    if max_samples is not None and max_samples < len(samples):
        samples = samples[:max_samples]

    logger.info("dmr.samples_ready", extra={"count": len(samples)})
    return samples


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Answer generation                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


async def answer_question(
    prompt: str,
    llm_client: Any,  # AsyncOpenAI-compatible
    *,
    model: str = ANSWER_MODEL,
    temperature: float = ANSWER_TEMPERATURE,
    max_tokens: int = 512,
) -> str:
    """Answer a question given a composed prompt.

    Args:
        prompt: The full prompt (context + question).
        llm_client: An ``AsyncOpenAI`` instance (OpenAI or OpenRouter).
        model: LLM model ID.
        temperature: Sampling temperature.
        max_tokens: Maximum tokens for the answer.

    Returns:
        The generated answer text.
    """
    response = await llm_client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Per-sample evaluation                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


async def evaluate_oz(
    sample: DMRSample,
    adapter: OpenZepBenchAdapter,
    llm_client: Any,  # AsyncOpenAI-compatible
    grader: DMRGrader,
) -> DMRResult:
    """Evaluate a single sample with the full OpenZep pipeline.

    Flow:
        1. Ingest history sessions (all except the last one) into OpenZep.
        2. Wait for enrichment.
        3. Get context for the question (from the current/present state).
        4. Generate answer with context.
        5. Grade the answer vs gold.
    """
    result = DMRResult(conversation_id=sample.conversation_id)

    try:
        session_id = f"dmr-{sample.conversation_id}"

        # Ingest history sessions (all except last = the present session)
        for i in range(len(sample.sessions) - 1):
            messages = sample.sessions[i]
            if not messages:
                continue
            await adapter.ingest(
                session_id=session_id,
                messages=messages,
                idempotency_key=f"{session_id}-hist-{i}",
            )

        # Wait for enrichment on the whole session
        enrichment = await adapter.wait_for_enrichment(
            session_id=session_id,
            timeout_s=ENRICHMENT_TIMEOUT_S,
        )
        if not enrichment.get("fully_enriched"):
            logger.warning(
                "dmr.enrichment_incomplete",
                extra={
                    "conversation_id": sample.conversation_id,
                    "enrichment": enrichment,
                },
            )

        # Get context for the question
        start = time.monotonic()
        ctx_response = await adapter.get_context(sample.question)
        result.oz_latency_s = time.monotonic() - start
        result.oz_context_chars = len(ctx_response.context)

        # Generate answer
        prompt = compose_prompt(sample.question, ctx_response.context)
        result.oz_answer = await answer_question(prompt, llm_client)

        # Grade
        result.oz_judge = await grader.grade(
            gold_answer=sample.gold_answer,
            generated_answer=result.oz_answer,
        )

    except Exception as exc:
        logger.error(
            "dmr.oz_error",
            extra={"conversation_id": sample.conversation_id, "error": str(exc)},
        )
        result.error = str(exc)

    return result


async def evaluate_baseline(
    sample: DMRSample,
    llm_client: Any,  # AsyncOpenAI-compatible
    grader: DMRGrader,
) -> DMRResult:
    """Evaluate a single sample with the full-context baseline.

    Injects all history messages (all sessions except the last one) as
    plain text — no OpenZep processing — then answers and grades.
    """
    result = DMRResult(conversation_id=sample.conversation_id)

    try:
        # Compose full history as plain text (all sessions except last)
        history_lines: list[str] = []
        for i in range(len(sample.sessions) - 1):
            for msg in sample.sessions[i]:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                history_lines.append(f"[{role}]: {content}")

        full_history = "\n".join(history_lines)
        prompt = compose_prompt(sample.question, full_history)

        start = time.monotonic()
        result.baseline_answer = await answer_question(prompt, llm_client)
        result.baseline_latency_s = time.monotonic() - start

        result.baseline_judge = await grader.grade(
            gold_answer=sample.gold_answer,
            generated_answer=result.baseline_answer,
        )

    except Exception as exc:
        logger.error(
            "dmr.baseline_error",
            extra={"conversation_id": sample.conversation_id, "error": str(exc)},
        )
        result.error = str(exc)

    return result


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Runner                                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


async def run_benchmark(
    samples: list[DMRSample],
    *,
    adapter: OpenZepBenchAdapter | None = None,
    db_session_factory: async_sessionmaker | None = None,
    run_oz: bool = True,
    run_baseline: bool = True,
    concurrency: int = CONCURRENCY,
) -> DMRSummary:
    """Run the DMR benchmark.

    Args:
        samples: List of DMRSample objects.
        adapter: Optional pre-constructed adapter (created from env if None).
        db_session_factory: Required if ``adapter`` is not provided.
        run_oz: If True, evaluate with full OpenZep pipeline.
        run_baseline: If True, evaluate with full-context baseline.
        concurrency: Max concurrent sample evaluations.

    Returns:
        A ``DMRSummary`` with aggregated results.
    """
    llm_client = create_llm_client()
    grader = DMRGrader(llm_client)

    start_time = time.monotonic()
    summary = DMRSummary(total=len(samples))
    sem = asyncio.Semaphore(concurrency)

    async def _eval_one(
        sample: DMRSample,
    ) -> DMRResult:
        async with sem:
            result = DMRResult(conversation_id=sample.conversation_id)

            # OpenZep pipeline
            if run_oz and adapter is not None:
                oz_result = await evaluate_oz(sample, adapter, llm_client, grader)
                result.oz_answer = oz_result.oz_answer
                result.oz_judge = oz_result.oz_judge
                result.oz_latency_s = oz_result.oz_latency_s
                result.oz_context_chars = oz_result.oz_context_chars
                if oz_result.error:
                    result.error = oz_result.error

            # Baseline
            if run_baseline:
                bl_result = await evaluate_baseline(sample, llm_client, grader)
                result.baseline_answer = bl_result.baseline_answer
                result.baseline_judge = bl_result.baseline_judge
                result.baseline_latency_s = bl_result.baseline_latency_s
                if bl_result.error and not result.error:
                    result.error = bl_result.error

            # Update summary counts
            if result.oz_judge:
                if result.oz_judge.get("decision") == "YES":
                    summary.oz_correct += 1
                if result.oz_judge.get("decision") == "ERROR":
                    summary.oz_errors += 1
                summary.oz_avg_latency_s += result.oz_latency_s

            if result.baseline_judge:
                if result.baseline_judge.get("decision") == "YES":
                    summary.baseline_correct += 1
                if result.baseline_judge.get("decision") == "ERROR":
                    summary.baseline_errors += 1
                summary.baseline_avg_latency_s += result.baseline_latency_s

            return result

    # Execute all samples with concurrency control
    tasks = [_eval_one(s) for s in samples]
    summary.results = await asyncio.gather(*tasks)

    summary.elapsed_s = time.monotonic() - start_time

    # Finalize averages
    n_oz = len([r for r in summary.results if r.oz_judge])
    n_bl = len([r for r in summary.results if r.baseline_judge])
    if n_oz:
        summary.oz_avg_latency_s /= n_oz
    if n_bl:
        summary.baseline_avg_latency_s /= n_bl

    return summary


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Reporting                                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


def _report_summary(summary: DMRSummary) -> str:
    """Format the summary as a Markdown report."""
    oz_acc = summary.oz_correct / max(summary.total, 1) * 100
    bl_acc = summary.baseline_correct / max(summary.total, 1) * 100

    lines = [
        "# DMR Benchmark Results",
        "",
        f"- **Date**: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        f"- **Total conversations**: {summary.total}",
        f"- **Elapsed time**: {summary.elapsed_s:.1f}s ({summary.elapsed_s / 60:.1f}m)",
        "",
        "## OpenZep Pipeline",
        f"- **Correct**: {summary.oz_correct} / {summary.total} ({oz_acc:.1f}%)",
        f"- **Errors**: {summary.oz_errors}",
        f"- **Avg latency (context retrieval)**: {summary.oz_avg_latency_s:.2f}s",
        "",
        "## Baseline (full-context)",
        f"- **Correct**: {summary.baseline_correct} / {summary.total} ({bl_acc:.1f}%)",
        f"- **Errors**: {summary.baseline_errors}",
        f"- **Avg latency**: {summary.baseline_avg_latency_s:.2f}s",
        "",
        "## Per-sample detail",
        "",
        "| # | Conversation ID | OZ Pass? | Baseline Pass? | Error |",
        "|---|-----------------|----------|----------------|-------|",
    ]

    for i, r in enumerate(summary.results):
        oz_pass = r.oz_judge.get("decision", "?") if r.oz_judge else "-"
        bl_pass = r.baseline_judge.get("decision", "?") if r.baseline_judge else "-"
        err = (r.error or "")[:60]
        lines.append(f"| {i+1} | {r.conversation_id[:12]}... | {oz_pass} | {bl_pass} | {err} |")

    return "\n".join(lines)


def _save_results(summary: DMRSummary) -> None:
    """Write JSON and Markdown reports."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S")

    # JSON dump
    json_path = RESULTS_DIR / f"dmr_results_{ts}.json"
    json_data = {
        "summary": {
            "total": summary.total,
            "oz_correct": summary.oz_correct,
            "oz_errors": summary.oz_errors,
            "oz_avg_latency_s": summary.oz_avg_latency_s,
            "baseline_correct": summary.baseline_correct,
            "baseline_errors": summary.baseline_errors,
            "baseline_avg_latency_s": summary.baseline_avg_latency_s,
            "elapsed_s": summary.elapsed_s,
        },
        "results": [
            {
                "conversation_id": r.conversation_id,
                "oz_answer": r.oz_answer,
                "oz_judge": r.oz_judge,
                "oz_latency_s": r.oz_latency_s,
                "baseline_answer": r.baseline_answer,
                "baseline_judge": r.baseline_judge,
                "baseline_latency_s": r.baseline_latency_s,
                "error": r.error,
            }
            for r in summary.results
        ],
    }
    with open(json_path, "w") as f:
        json.dump(json_data, f, indent=2, default=str)
    logger.info("dmr.results_saved", extra={"path": str(json_path), "samples": summary.total})

    # Markdown report
    md_path = RESULTS_DIR / f"dmr_report_{ts}.md"
    md_content = _report_summary(summary)
    with open(md_path, "w") as f:
        f.write(md_content)
    print(md_content)
    logger.info("dmr.report_saved", extra={"path": str(md_path)})


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CLI                                                                       ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DMR Benchmark Runner")
    parser.add_argument(
        "--samples",
        type=int,
        default=None,
        help="Number of samples to evaluate (default: all 500)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=CONCURRENCY,
        help=f"Concurrent evaluations (default: {CONCURRENCY})",
    )
    parser.add_argument(
        "--oz-only",
        action="store_true",
        help="Skip baseline, evaluate OpenZep only",
    )
    parser.add_argument(
        "--baseline-only",
        action="store_true",
        help="Skip OpenZep, evaluate baseline only",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="PostgreSQL URL for enrichment polling (default: from OpenZep config)",
    )
    return parser.parse_args()


async def main() -> None:
    """Main entry point."""
    args = _parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    run_oz = not args.baseline_only
    run_baseline = not args.oz_only

    # Load dataset
    print("=" * 60)
    print("DMR Benchmark")
    print("=" * 60)
    print(f"Loading dataset: {DMR_DATASET_NAME}")
    samples = await load_dmr_dataset(max_samples=args.samples)
    print(f"Loaded {len(samples)} samples")

    # Setup adapter (if running OpenZep pipeline)
    adapter: OpenZepBenchAdapter | None = None
    db_session_factory: async_sessionmaker | None = None

    if run_oz:
        if args.db_url:
            from sqlalchemy.ext.asyncio import (
                AsyncSession,
                async_sessionmaker,
                create_async_engine,
            )
            from models.episode import Base  # noqa: F401 — ensure models loaded

            engine = create_async_engine(args.db_url, pool_pre_ping=True, pool_size=5)
            db_session_factory = async_sessionmaker(
                bind=engine, class_=AsyncSession, expire_on_commit=False
            )
        else:
            # Try to get the default engine from OpenZep config
            try:
                from core.config import settings
                from sqlalchemy.ext.asyncio import (
                    AsyncSession,
                    async_sessionmaker,
                    create_async_engine,
                )

                engine = create_async_engine(
                    str(settings.DATABASE_URL),
                    pool_pre_ping=True,
                    pool_size=5,
                )
                db_session_factory = async_sessionmaker(
                    bind=engine, class_=AsyncSession, expire_on_commit=False
                )
            except Exception:
                print(
                    "Could not configure DB session factory. "
                    "Pass --db-url or ensure core.config.settings.DATABASE_URL is set."
                )
                raise

        adapter = OpenZepBenchAdapter(db_session_factory=db_session_factory)

    # Run
    async with adapter or _null_async_cm():
        summary = await run_benchmark(
            samples,
            adapter=adapter,
            db_session_factory=db_session_factory,
            run_oz=run_oz,
            run_baseline=run_baseline,
            concurrency=args.concurrency,
        )

    # Report
    _save_results(summary)

    # Print final summary
    print("\n" + "=" * 60)
    print("DMR Benchmark Complete")
    print("=" * 60)
    if run_oz and summary.total > 0:
        oz_rate = summary.oz_correct / summary.total * 100
        print(f"  OpenZep  accuracy: {oz_rate:.1f}% ({summary.oz_correct}/{summary.total})")
    if run_baseline and summary.total > 0:
        bl_rate = summary.baseline_correct / summary.total * 100
        print(f"  Baseline accuracy: {bl_rate:.1f}% ({summary.baseline_correct}/{summary.total})")
    print(f"  Elapsed: {summary.elapsed_s:.0f}s")


class _NullAsyncCM:
    """No-op async context manager for when adapter is None."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: Any) -> None:
        pass


if __name__ == "__main__":
    asyncio.run(main())
