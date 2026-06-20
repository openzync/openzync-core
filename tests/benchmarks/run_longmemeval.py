"""LongMemEval benchmark runner — ~200 multi-episode conversations.

Usage::

    # Prerequisites
    python -m tests.benchmarks.setup                              # one-time
    git clone https://github.com/xiaowu0162/LongMemEval.git /tmp/LongMemEval  # dataset
    export LLM_API_KEY="..."                                      # optional for OpenRouter free

    # Run benchmark
    python -m tests.benchmarks.run_longmemeval
    python -m tests.benchmarks.run_longmemeval --samples 10      # quick smoke test
    python -m tests.benchmarks.run_longmemeval --baseline-only
    python -m tests.benchmarks.run_longmemeval --oz-only

Output is written to ``tests/benchmarks/results/longmemeval/`` as JSON and Markdown.

LongMemEval evaluates 6 capabilities:
    - temporal-reasoning
    - knowledge-update
    - single-session-preference
    - cross-session-preference
    - cross-session-reasoning
    - self-contained

Each sample contains multiple conversation episodes (sessions), a question,
a gold answer, and a question-type label.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

# ── Add project root to path for imports ───────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.benchmarks.context_composer import compose_prompt
from tests.benchmarks.llm import (
    LLM_MODEL,
    LLM_DEFAULT_TEMPERATURE,
    create_llm_client,
)
from tests.benchmarks.lme_grader import LongMemEvalGrader
from tests.benchmarks.oz_adapter import OpenZepBenchAdapter

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

LME_DATASET_PATH: str = "/tmp/LongMemEval/data/longmemeval_s.json"
"""Path to the LongMemEval dataset ``s`` (small) variant."""

LME_DATASET_GIT: str = "https://github.com/xiaowu0162/LongMemEval.git"

ANSWER_MODEL: str = LLM_MODEL
ANSWER_TEMPERATURE: float = LLM_DEFAULT_TEMPERATURE
ENRICHMENT_TIMEOUT_S: float = 300.0
CONCURRENCY: int = 5

RESULTS_DIR = Path(__file__).resolve().parent / "results" / "longmemeval"

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Data structures                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


@dataclass
class LMESample:
    """A single LongMemEval sample."""

    sample_id: str
    question_type: str  # one of the 6 types
    episodes: list[list[dict[str, str]]]  # conversation episodes
    question: str
    gold_answer: str


@dataclass
class LMEResult:
    """Result for a single sample."""

    sample_id: str
    question_type: str
    oz_answer: str | None = None
    oz_judge: dict[str, Any] | None = None
    oz_latency_s: float = 0.0
    baseline_answer: str | None = None
    baseline_judge: dict[str, Any] | None = None
    baseline_latency_s: float = 0.0
    error: str | None = None


@dataclass
class LMESummary:
    """Aggregated per-type and overall results."""

    total: int = 0
    by_type: dict[str, dict[str, int | float]] = field(default_factory=dict)
    oz_correct: int = 0
    oz_errors: int = 0
    oz_avg_latency_s: float = 0.0
    baseline_correct: int = 0
    baseline_errors: int = 0
    baseline_avg_latency_s: float = 0.0
    results: list[LMEResult] = field(default_factory=list)
    elapsed_s: float = 0.0


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Dataset loading                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


async def load_longmemeval_dataset(
    path: str = LME_DATASET_PATH,
    max_samples: int | None = None,
) -> list[LMESample]:
    """Load the LongMemEval dataset from a local JSON file.

    The dataset is structured as a list of samples, each containing:
        - ``id`` (str)
        - ``question_type`` (str) — one of 6 types
        - ``episodes`` (list of lists of {"role": ..., "content": ...})
        - ``question`` (str)
        - ``gold_answer`` (str)

    If the file doesn't exist, attempts to clone from GitHub.
    """
    path_obj = Path(path)

    if not path_obj.exists():
        logger.warning(
            "lme.dataset_not_found",
            extra={"path": path, "git": LME_DATASET_GIT},
        )
        print(f"\nDataset not found at {path}")
        print(f"Clone it with:\n    git clone {LME_DATASET_GIT} {path_obj.parent}")
        print("Or download from: https://github.com/xiaowu0162/LongMemEval\n")
        raise FileNotFoundError(f"LongMemEval dataset not found at {path}")

    with open(path_obj) as f:
        raw_data = json.load(f)

    samples: list[LMESample] = []
    for row in raw_data:
        sample = LMESample(
            sample_id=row.get("id", str(len(samples))),
            question_type=row.get("question_type", "self-contained"),
            episodes=row.get("episodes", []),
            question=row.get("question", ""),
            gold_answer=row.get("gold_answer", ""),
        )
        samples.append(sample)

    if max_samples is not None and max_samples < len(samples):
        samples = samples[:max_samples]

    logger.info("lme.dataset_loaded", extra={"count": len(samples)})
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
    """Answer a question given a composed prompt."""
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
    sample: LMESample,
    adapter: OpenZepBenchAdapter,
    llm_client: Any,  # AsyncOpenAI-compatible
    grader: LongMemEvalGrader,
) -> LMEResult:
    """Evaluate a single LongMemEval sample with the OpenZep pipeline."""
    result = LMEResult(
        sample_id=sample.sample_id,
        question_type=sample.question_type,
    )

    try:
        session_id = f"lme-{sample.sample_id}"

        # Ingest all episodes into the same session
        for i, messages in enumerate(sample.episodes):
            if not messages:
                continue
            await adapter.ingest(
                session_id=session_id,
                messages=messages,
                idempotency_key=f"{session_id}-ep-{i}",
            )

        # Wait for enrichment
        enrichment = await adapter.wait_for_enrichment(
            session_id=session_id,
            timeout_s=ENRICHMENT_TIMEOUT_S,
        )
        if not enrichment.get("fully_enriched"):
            logger.warning(
                "lme.enrichment_incomplete",
                extra={"sample_id": sample.sample_id, "enrichment": enrichment},
            )

        # Get context
        start = time.monotonic()
        ctx_response = await adapter.get_context(sample.question)
        result.oz_latency_s = time.monotonic() - start

        # Generate answer
        prompt = compose_prompt(sample.question, ctx_response.context)
        result.oz_answer = await answer_question(prompt, llm_client)

        # Grade
        result.oz_judge = await grader.grade(
            question_type=sample.question_type,
            gold_answer=sample.gold_answer,
            generated_answer=result.oz_answer,
        )

    except Exception as exc:
        logger.error(
            "lme.oz_error",
            extra={"sample_id": sample.sample_id, "error": str(exc)},
        )
        result.error = str(exc)

    return result


async def evaluate_baseline(
    sample: LMESample,
    llm_client: Any,  # AsyncOpenAI-compatible
    grader: LongMemEvalGrader,
) -> LMEResult:
    """Evaluate a single sample with the full-context baseline."""
    result = LMEResult(
        sample_id=sample.sample_id,
        question_type=sample.question_type,
    )

    try:
        # Compose full history as plain text
        history_lines: list[str] = []
        for messages in sample.episodes:
            for msg in messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                history_lines.append(f"[{role}]: {content}")

        full_history = "\n".join(history_lines)
        prompt = compose_prompt(sample.question, full_history)

        start = time.monotonic()
        result.baseline_answer = await answer_question(prompt, llm_client)
        result.baseline_latency_s = time.monotonic() - start

        result.baseline_judge = await grader.grade(
            question_type=sample.question_type,
            gold_answer=sample.gold_answer,
            generated_answer=result.baseline_answer,
        )

    except Exception as exc:
        logger.error(
            "lme.baseline_error",
            extra={"sample_id": sample.sample_id, "error": str(exc)},
        )
        result.error = str(exc)

    return result


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Runner                                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


async def run_benchmark(
    samples: list[LMESample],
    *,
    adapter: OpenZepBenchAdapter | None = None,
    db_session_factory: async_sessionmaker | None = None,
    run_oz: bool = True,
    run_baseline: bool = True,
    concurrency: int = CONCURRENCY,
) -> LMESummary:
    """Run the LongMemEval benchmark."""
    llm_client = create_llm_client()
    grader = LongMemEvalGrader(llm_client)

    start_time = time.monotonic()
    summary = LMESummary(total=len(samples))
    sem = asyncio.Semaphore(concurrency)

    # Track per-type counts
    type_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "oz_correct": 0, "oz_errors": 0, "baseline_correct": 0, "baseline_errors": 0}
    )

    async def _eval_one(sample: LMESample) -> LMEResult:
        async with sem:
            result = LMEResult(
                sample_id=sample.sample_id,
                question_type=sample.question_type,
            )

            # Track per-type total
            type_counts[sample.question_type]["total"] += 1

            if run_oz and adapter is not None:
                oz_result = await evaluate_oz(sample, adapter, llm_client, grader)
                result.oz_answer = oz_result.oz_answer
                result.oz_judge = oz_result.oz_judge
                result.oz_latency_s = oz_result.oz_latency_s
                if oz_result.error:
                    result.error = oz_result.error

            if run_baseline:
                bl_result = await evaluate_baseline(sample, llm_client, grader)
                result.baseline_answer = bl_result.baseline_answer
                result.baseline_judge = bl_result.baseline_judge
                result.baseline_latency_s = bl_result.baseline_latency_s
                if bl_result.error and not result.error:
                    result.error = bl_result.error

            # Update per-type counts
            if result.oz_judge:
                if result.oz_judge.get("decision") == "YES":
                    type_counts[sample.question_type]["oz_correct"] += 1
                    summary.oz_correct += 1
                if result.oz_judge.get("decision") == "ERROR":
                    type_counts[sample.question_type]["oz_errors"] += 1
                    summary.oz_errors += 1
                summary.oz_avg_latency_s += result.oz_latency_s

            if result.baseline_judge:
                if result.baseline_judge.get("decision") == "YES":
                    type_counts[sample.question_type]["baseline_correct"] += 1
                    summary.baseline_correct += 1
                if result.baseline_judge.get("decision") == "ERROR":
                    type_counts[sample.question_type]["baseline_errors"] += 1
                    summary.baseline_errors += 1
                summary.baseline_avg_latency_s += result.baseline_latency_s

            return result

    tasks = [_eval_one(s) for s in samples]
    summary.results = await asyncio.gather(*tasks)
    summary.elapsed_s = time.monotonic() - start_time

    # Build per-type summary
    for qtype, counts in type_counts.items():
        t = counts["total"]
        summary.by_type[qtype] = {
            "total": t,
            "oz_correct": counts["oz_correct"],
            "oz_accuracy_pct": round(counts["oz_correct"] / max(t, 1) * 100, 1),
            "baseline_correct": counts["baseline_correct"],
            "baseline_accuracy_pct": round(counts["baseline_correct"] / max(t, 1) * 100, 1),
        }

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


def _report_summary(summary: LMESummary) -> str:
    """Format as Markdown report."""
    oz_acc = summary.oz_correct / max(summary.total, 1) * 100
    bl_acc = summary.baseline_correct / max(summary.total, 1) * 100

    lines = [
        "# LongMemEval Benchmark Results",
        "",
        f"- **Date**: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        f"- **Total samples**: {summary.total}",
        f"- **Elapsed time**: {summary.elapsed_s:.1f}s ({summary.elapsed_s / 60:.1f}m)",
        "",
        "## Overall",
        f"- **OpenZep  accuracy**: {summary.oz_correct} / {summary.total} ({oz_acc:.1f}%)",
        f"- **Baseline accuracy**: {summary.baseline_correct} / {summary.total} ({bl_acc:.1f}%)",
        "",
        "## Per-type accuracy",
        "",
        "| Type | Total | OZ Correct | OZ % | Baseline Correct | Baseline % |",
        "|------|-------|------------|------|------------------|------------|",
    ]

    for qtype in [
        "temporal-reasoning",
        "knowledge-update",
        "single-session-preference",
        "cross-session-preference",
        "cross-session-reasoning",
        "self-contained",
    ]:
        d = summary.by_type.get(qtype, {})
        t = d.get("total", 0)
        oz_c = d.get("oz_correct", 0)
        bl_c = d.get("baseline_correct", 0)
        oz_p = d.get("oz_accuracy_pct", 0.0)
        bl_p = d.get("baseline_accuracy_pct", 0.0)
        lines.append(f"| {qtype} | {t} | {oz_c} | {oz_p}% | {bl_c} | {bl_p}% |")

    lines.extend([
        "",
        "## Per-sample detail",
        "",
        "| # | Sample ID | Type | OZ Pass? | BL Pass? |",
        "|---|-----------|------|----------|----------|",
    ])

    for i, r in enumerate(summary.results):
        oz_pass = r.oz_judge.get("decision", "?") if r.oz_judge else "-"
        bl_pass = r.baseline_judge.get("decision", "?") if r.baseline_judge else "-"
        lines.append(f"| {i+1} | {r.sample_id[:16]} | {r.question_type} | {oz_pass} | {bl_pass} |")

    return "\n".join(lines)


def _save_results(summary: LMESummary) -> None:
    """Write JSON and Markdown reports."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")

    json_path = RESULTS_DIR / f"lme_results_{ts}.json"
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
        "by_type": summary.by_type,
        "results": [
            {
                "sample_id": r.sample_id,
                "question_type": r.question_type,
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
    logger.info("lme.results_saved", extra={"path": str(json_path)})

    md_path = RESULTS_DIR / f"lme_report_{ts}.md"
    md_content = _report_summary(summary)
    with open(md_path, "w") as f:
        f.write(md_content)
    print(md_content)
    logger.info("lme.report_saved", extra={"path": str(md_path)})


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CLI                                                                       ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LongMemEval Benchmark Runner")
    parser.add_argument(
        "--samples",
        type=int,
        default=None,
        help="Number of samples to evaluate",
    )
    parser.add_argument(
        "--dataset-path",
        type=str,
        default=LME_DATASET_PATH,
        help=f"Path to LongMemEval JSON dataset (default: {LME_DATASET_PATH})",
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

    print("=" * 60)
    print("LongMemEval Benchmark")
    print("=" * 60)
    print(f"Loading dataset from: {args.dataset_path}")
    samples = await load_longmemeval_dataset(
        path=args.dataset_path,
        max_samples=args.samples,
    )
    print(f"Loaded {len(samples)} samples")
    print(f"Question types: {sorted(set(s.question_type for s in samples))}")
    print(f"  Samples per type:")
    for qtype in sorted(set(s.question_type for s in samples)):
        count = sum(1 for s in samples if s.question_type == qtype)
        print(f"    {qtype}: {count}")

    adapter: OpenZepBenchAdapter | None = None
    db_session_factory: async_sessionmaker | None = None

    if run_oz:
        if args.db_url:
            from sqlalchemy.ext.asyncio import (
                AsyncSession,
                async_sessionmaker,
                create_async_engine,
            )
            from models.episode import Base  # noqa: F401

            engine = create_async_engine(args.db_url, pool_pre_ping=True, pool_size=5)
            db_session_factory = async_sessionmaker(
                bind=engine, class_=AsyncSession, expire_on_commit=False
            )
        else:
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

        adapter = OpenZepBenchAdapter(db_session_factory=db_session_factory)

    async with adapter or _null_async_cm():
        summary = await run_benchmark(
            samples,
            adapter=adapter,
            db_session_factory=db_session_factory,
            run_oz=run_oz,
            run_baseline=run_baseline,
            concurrency=args.concurrency,
        )

    _save_results(summary)

    print("\n" + "=" * 60)
    print("LongMemEval Benchmark Complete")
    print("=" * 60)
    if run_oz and summary.total > 0:
        oz_rate = summary.oz_correct / summary.total * 100
        print(f"  OpenZep  accuracy: {oz_rate:.1f}% ({summary.oz_correct}/{summary.total})")
    if run_baseline and summary.total > 0:
        bl_rate = summary.baseline_correct / summary.total * 100
        print(f"  Baseline accuracy: {bl_rate:.1f}% ({summary.baseline_correct}/{summary.total})")
    print(f"  Elapsed: {summary.elapsed_s:.0f}s")


class _NullAsyncCM:
    """No-op async context manager."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: Any) -> None:
        pass


if __name__ == "__main__":
    asyncio.run(main())
