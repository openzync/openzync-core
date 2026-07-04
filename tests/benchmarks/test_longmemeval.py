"""LongMemEval benchmark — measures retrieval quality and QA accuracy.

This test ingests LongMemEval conversations into a live OpenZync instance,
waits for asynchronous enrichment, queries the search and context endpoints,
and evaluates both R@k (recall at k) and end-to-end QA accuracy.

Usage:
    # Full run (default variant 's', no baseline, no reranker):
    pytest tests/benchmarks/ --run-benchmark -v

    # Quick run (10 questions):
    pytest tests/benchmarks/ --run-benchmark --benchmark-limit=10 -v

    # With baseline comparison (pure vector only):
    pytest tests/benchmarks/ --run-benchmark --baseline -v

    # With reranker enabled:
    pytest tests/benchmarks/ --run-benchmark --reranker -v

    # Oracle variant:
    pytest tests/benchmarks/ --run-benchmark --variant=oracle -v
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.benchmarks.longmemeval_evaluator import EvaluationResult, evaluate_answer
from tests.benchmarks.longmemeval_utils import (
    compute_accuracy,
    compute_recall_at_k,
    is_abstention,
    load_dataset,
)

if TYPE_CHECKING:

    from core.llm import LLMBackend

from types import SimpleNamespace

import httpx

from core.llm import LLMStructuredOutputError

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

BENCHMARK_VARIANT: str = "s"
"""Default dataset variant to use when not overridden by CLI."""

ENRICHMENT_ALL: int = (
    (1 << 0)  # entity extraction
    | (1 << 1)  # episode embedding
    | (1 << 2)  # fact extraction
    | (1 << 3)  # entity-episode linking
    | (1 << 4)  # dialog classification
    | (1 << 5)  # structured extraction
)
"""Bitmask for fully enriched episodes (bits 0-5, excluding observation bit 6)."""

ENRICHMENT_POLL_INTERVAL_S: float = 2.0
"""Seconds between enrichment status polls."""

ENRICHMENT_TIMEOUT_S: int = 300
"""Maximum seconds to wait for enrichment to complete."""

RESULTS_DIR: Path = (
    Path(__file__).resolve().parent.parent.parent / "benchmarks" / "results"
)
"""Directory where timestamped benchmark result JSON files are stored."""


# ═══════════════════════════════════════════════════════════════════════════════
# Private helpers
# ═══════════════════════════════════════════════════════════════════════════════

# Retryable HTTP status codes
_RETRYABLE_STATUSES: set[int] = {429, 502, 503, 504}

#: Path for incremental progress saves (protects against partial data loss).
_TMP_RESULTS_DIR: Path = RESULTS_DIR / ".in_progress"


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    max_retries: int = 3,
    base_delay_s: float = 1.0,
    **kwargs: Any,
) -> httpx.Response:
    """Make an HTTP request with exponential backoff retry.

    Retries on 429 (rate-limit), 502, 503, 504 (server errors).  Other
    errors (4xx, network errors) propagate immediately.

    Args:
        client: The async HTTP client.
        method: HTTP method (``"GET"``, ``"POST"``, etc.).
        url: Request path (relative to client base URL).
        max_retries: Maximum retry attempts (default 3).
        base_delay_s: Initial backoff delay in seconds (doubles each retry).
        **kwargs: Additional arguments for ``client.request()``.

    Returns:
        The response object on success.

    Raises:
        httpx.HTTPStatusError: If a non-retryable error occurs or retries
            are exhausted.
        httpx.RequestError: On network failures.
    """
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 2):  # +1 for the initial attempt
        try:
            resp = await client.request(method, url, **kwargs)

            if resp.status_code in _RETRYABLE_STATUSES and attempt <= max_retries:
                delay = base_delay_s * (2 ** (attempt - 1))
                logger.warning(
                    "Retryable HTTP %d on %s %s — retrying in %.1fs (attempt %d/%d)",
                    resp.status_code,
                    method.upper(),
                    url,
                    delay,
                    attempt,
                    max_retries,
                )
                await asyncio.sleep(delay)
                continue

            resp.raise_for_status()
            return resp

        except httpx.TimeoutException as exc:
            if attempt <= max_retries:
                delay = base_delay_s * (2 ** (attempt - 1))
                logger.warning(
                    "Timeout on %s %s — retrying in %.1fs (attempt %d/%d)",
                    method.upper(),
                    url,
                    delay,
                    attempt,
                    max_retries,
                )
                await asyncio.sleep(delay)
                last_exc = exc
                continue
            raise

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in _RETRYABLE_STATUSES and attempt <= max_retries:
                delay = base_delay_s * (2 ** (attempt - 1))
                logger.warning(
                    "Retryable HTTP %d on %s %s — retrying in %.1fs (attempt %d/%d)",
                    exc.response.status_code,
                    method.upper(),
                    url,
                    delay,
                    attempt,
                    max_retries,
                )
                await asyncio.sleep(delay)
                last_exc = exc
                continue
            raise

    # Should not reach here — last retry raises naturally
    raise httpx.HTTPStatusError(
        f"Request failed after {max_retries} retries",
        request=last_exc.__traceback__ if hasattr(last_exc, "__traceback__") else None,
    )


async def _login(client: httpx.AsyncClient) -> str:
    """Authenticate using the benchmark credentials and return a JWT token.

    Reads ``BENCH_EMAIL`` and ``BENCH_PASSWORD`` from the environment.

    Returns:
        A JWT access token string.

    Raises:
        RuntimeError: If credentials are missing or login fails.
    """
    email = os.environ.get("BENCH_EMAIL")
    password = os.environ.get("BENCH_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "BENCH_EMAIL and BENCH_PASSWORD must be set in environment"
        )

    resp = await _request_with_retry(
        client, "POST", "/v1/auth/login",
        json={"email": email, "password": password},
    )
    data: dict = resp.json()
    return str(data["access_token"])


def _auth_header(token: str) -> dict[str, str]:
    """Return an Authorization header dict for a JWT token."""
    return {"Authorization": f"Bearer {token}"}


async def _create_project(
    client: httpx.AsyncClient, token: str, name: str | None = None
) -> str:
    """Create a fresh project for benchmark ingestion.

    Args:
        client: Authenticated HTTP client.
        token: JWT access token.
        name: Optional project name (auto-generated if omitted).

    Returns:
        The created project's UUID as a string.
    """
    project_name = name or f"longmemeval-{int(time.time())}"
    resp = await _request_with_retry(
        client, "POST", "/v1/projects",
        json={"name": project_name},
        headers=_auth_header(token),
    )
    data: dict = resp.json()
    return str(data["id"])


async def _set_org_graph_backend(
    client: httpx.AsyncClient, token: str, graph_backend: str = "postgres"
) -> dict[str, Any]:
    """Update the org-level graph backend configuration.

    Used to toggle between full pipeline and baseline (no graph) modes.

    Args:
        client: Authenticated HTTP client.
        token: JWT access token.
        graph_backend: ``"postgres"`` (full) or ``"none"`` (baseline).

    Returns:
        The updated org config response.
    """
    resp = await _request_with_retry(
        client, "PATCH", "/admin/org/config",
        json={"graph_backend": graph_backend},
        headers=_auth_header(token),
    )
    return resp.json()


async def _create_session(
    client: httpx.AsyncClient,
    token: str,
    project_id: str,
    external_id: str,
) -> str:
    """Create a session within a project.

    Each LongMemEval entry gets its own session so retrieval is measured
    across independent conversation contexts.

    Args:
        client: Authenticated HTTP client.
        token: JWT access token.
        project_id: Parent project UUID.
        external_id: Caller-defined session identifier (must be unique
            per project).

    Returns:
        The created session's UUID as a string.
    """
    resp = await _request_with_retry(
        client, "POST", f"/v1/projects/{project_id}/sessions",
        json={"external_id": external_id},
        headers=_auth_header(token),
    )
    data: dict = resp.json()
    return str(data["id"])


async def _delete_project(
    client: httpx.AsyncClient, token: str, project_id: str
) -> None:
    """Archive (soft-delete) a benchmark project.

    Args:
        client: Authenticated HTTP client.
        token: JWT access token.
        project_id: The project UUID to delete.
    """
    await _request_with_retry(
        client, "DELETE", f"/v1/projects/{project_id}",
        headers=_auth_header(token),
    )


async def _ingest_memory(
    client: httpx.AsyncClient,
    token: str,
    project_id: str,
    messages: list[dict[str, str]],
    session_id: str | None = None,
) -> None:
    """Ingest a batch of messages into a session within a project.

    If ``session_id`` is provided, messages are ingested into that
    specific session.  If omitted, the server auto-creates a
    ``__default__`` session.

    Enrichment runs asynchronously in the background.

    Args:
        client: Authenticated HTTP client.
        token: JWT access token.
        project_id: Target project UUID.
        messages: List of ``{"role": str, "content": str}`` dicts.
        session_id: Optional session UUID to ingest into.
    """
    body: dict[str, object] = {"messages": messages}
    if session_id is not None:
        body["session_id"] = session_id

    await _request_with_retry(
        client, "POST", f"/v1/projects/{project_id}/memory",
        json=body,
        headers=_auth_header(token),
    )


async def _wait_for_enrichment(
    client: httpx.AsyncClient,
    token: str,
    project_id: str,
) -> None:
    """Poll until all episodes in the organization are fully enriched.

    Uses the ``GET /metrics/summary`` endpoint to check enrichment stats
    org-wide.  The ``episode_stats.in_progress`` field counts episodes
    where ``enrichment_status != 63`` (not all 6 bits set).  Polls every
    2 seconds with a 5-minute timeout.

    A brief initial delay is applied after the last ingestion to give the
    worker queue time to pick up tasks before the first poll.

    Args:
        client: Authenticated HTTP client.
        token: JWT access token.
        project_id: The project UUID to monitor (used for logging only).

    Raises:
        TimeoutError: If enrichment does not complete within the timeout.
    """
    logger.info(
        "Waiting 10s for worker to pick up enrichment tasks before polling..."
    )
    await asyncio.sleep(10)

    deadline = time.monotonic() + ENRICHMENT_TIMEOUT_S
    last_logged: int = -1

    while time.monotonic() < deadline:
        resp = await _request_with_retry(
            client, "GET", "/metrics/summary",
            headers=_auth_header(token),
        )
        data: dict[str, Any] = resp.json()

        episodes = data.get("episodes", {})
        total = episodes.get("added_total", 0)
        in_progress = episodes.get("in_progress", 0)

        if total == 0:
            logger.info("No episodes found yet — waiting...")
            await asyncio.sleep(ENRICHMENT_POLL_INTERVAL_S)
            continue

        completed = total - in_progress
        pct = int(completed / total * 100)
        if pct != last_logged:
            logger.info(
                "Enrichment progress: %d%% (%d/%d episodes, %d in progress)",
                pct,
                completed,
                total,
                in_progress,
            )
            last_logged = pct

        if in_progress == 0:
            logger.info("All %d episodes fully enriched.", total)
            return

        await asyncio.sleep(ENRICHMENT_POLL_INTERVAL_S)

    raise TimeoutError(
        f"Enrichment did not complete within {ENRICHMENT_TIMEOUT_S}s "
        f"for project {project_id}.  Last progress: {last_logged}%."
    )


async def _search(
    client: httpx.AsyncClient,
    token: str,
    project_id: str,
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Run a hybrid search against a project.

    Args:
        client: Authenticated HTTP client.
        token: JWT access token.
        project_id: Target project UUID.
        query: Search query string.
        limit: Max results per source type.

    Returns:
        List of search result dicts with at minimum ``content`` and ``score``
        keys.
    """
    resp = await _request_with_retry(
        client, "GET", f"/v1/projects/{project_id}/search",
        params={"query": query, "limit": limit, "types": "episodes,facts"},
        headers=_auth_header(token),
    )
    data: dict[str, Any] = resp.json()
    return data.get("results", [])


async def _get_context(
    client: httpx.AsyncClient,
    token: str,
    project_id: str,
    query: str,
    limit: int = 20,
) -> str:
    """Retrieve assembled context for a query.

    Args:
        client: Authenticated HTTP client.
        token: JWT access token.
        project_id: Target project UUID.
        query: Context query string.
        limit: Max items per source type.

    Returns:
        The assembled context text.
    """
    resp = await _request_with_retry(
        client, "GET", f"/v1/projects/{project_id}/context",
        params={"query": query, "limit": limit, "format": "text"},
        headers=_auth_header(token),
    )
    data: dict[str, Any] = resp.json()
    return str(data.get("context", ""))


def _build_comparison_table(
    metrics_full: dict[str, Any],
    metrics_baseline: dict[str, Any] | None,
) -> str:
    """Build a markdown comparison table of benchmark results.

    Compare OpenZync scores against published Graphiti and Mem0 numbers.

    Reference numbers (published):
        - Graphiti LongMemEval-S: 83%
        - Graphiti LoCoMo: 93%
        - Mem0 LongMemEval: ~49%

    Args:
        metrics_full: Results dict from the full pipeline run.
        metrics_baseline: Optional results dict from the baseline run.

    Returns:
        A markdown-formatted comparison table.
    """
    accuracy_full = metrics_full.get("overall_accuracy", 0.0)
    accuracy_baseline = (
        metrics_baseline.get("overall_accuracy", 0.0) if metrics_baseline else None
    )

    lines = [
        "| System | Accuracy | R@1 | R@5 | R@10 | Conditions |",
        "|--------|----------|-----|-----|------|------------|",
    ]

    # OpenZync full pipeline
    lines.append(
        f"| OpenZync (full) | {accuracy_full:.1%} | "
        f"{metrics_full.get('r1', 0):.1%} | "
        f"{metrics_full.get('r5', 0):.1%} | "
        f"{metrics_full.get('r10', 0):.1%} | "
        f"LongMemEval-S, RRF + reranker |"
    )

    # OpenZync baseline (if available)
    if accuracy_baseline is not None:
        lines.append(
            f"| OpenZync (baseline) | {accuracy_baseline:.1%} | "
            f"{metrics_baseline.get('r1', 0):.1%} | "
            f"{metrics_baseline.get('r5', 0):.1%} | "
            f"{metrics_baseline.get('r10', 0):.1%} | "
            f"Pure vector only |"
        )

    # Published reference numbers
    lines.append("| Graphiti | 83% | — | — | — | LongMemEval-S |")
    lines.append("| Graphiti | 93% | — | — | — | LoCoMo |")
    lines.append("| Mem0 | ~49% | — | — | — | LongMemEval |")

    # Per-category breakdown
    lines.append("")
    lines.append("### Per-Category Accuracy")
    lines.append("| Category | Accuracy | Count |")
    lines.append("|----------|----------|-------|")
    for cat, stats in sorted(metrics_full.get("per_category", {}).items()):
        lines.append(
            f"| {cat} | {stats['accuracy']:.1%} | {stats['total']} |"
        )

    return "\n".join(lines)


async def _answer_from_context(
    backend: LLMBackend,
    question: str,
    context: str,
    is_abstention: bool,
) -> str:
    """Generate an answer from retrieved context using the LLM.

    Args:
        backend: LLM backend for answer generation (same as judge backend).
        question: The user's question.
        context: Retrieved context text from OpenZync.
        is_abstention: Whether the question expects abstention (i.e., the
            model should say it doesn't know if the info isn't in context).

    Returns:
        The generated answer string.
    """
    system_prompt = (
        "You are a precise question-answering assistant. Answer the question "
        "using ONLY the information provided in the context below. "
        "If the context does not contain enough information to answer the "
        "question, respond with: 'I cannot answer this question based on the "
        "available information.' Do NOT make up or infer information."
    )
    if is_abstention:
        system_prompt += (
            "\n\nThis is an abstention question. If the context does not "
            "contain the answer, you MUST say you don't know. Do not guess."
        )

    user_prompt = f"Context:\n{context}\n\nQuestion: {question}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    response = await backend.chat(
        messages=messages,
        temperature=0.0,
        max_tokens=512,
    )
    return response.content


def _save_results(
    results: list[dict[str, Any]],
    config: SimpleNamespace,
    git_commit: str | None,
    version: str | None,
) -> Path:
    """Save benchmark results to a timestamped JSON file.

    Args:
        results: List of per-question result dicts.
        config: Benchmark configuration from CLI args.
        git_commit: Current git commit hash.
        version: OpenZync version string.

    Returns:
        Path to the saved results file.
    """
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    variant = config.variant or BENCHMARK_VARIANT

    # Compute aggregate metrics
    metrics = compute_accuracy(results)

    # Compute R@k metrics
    r1_count = sum(1 for r in results if r.get("r1", False))
    r5_count = sum(1 for r in results if r.get("r5", False))
    r10_count = sum(1 for r in results if r.get("r10", False))
    total = len(results) if results else 1

    metrics["r1"] = r1_count / total
    metrics["r5"] = r5_count / total
    metrics["r10"] = r10_count / total

    output = {
        "version": version or "unknown",
        "git_commit": git_commit or "unknown",
        "date": datetime.now(UTC).isoformat(),
        "config": {
            "variant": variant,
            "reranker_enabled": config.reranker,
            "baseline_mode": config.baseline,
            "benchmark_limit": config.benchmark_limit,
            "llm_judge_model": "openai/gpt-oss-120b",
            "judge_temperature": 0.0,
        },
        "metrics": metrics,
        "per_question": results,
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"longmemeval_{variant}_{timestamp}.json"
    filepath = RESULTS_DIR / filename

    with open(filepath, "w") as f:
        json.dump(output, f, indent=2, default=str)

    logger.info("Results saved to %s", filepath)
    return filepath


def _get_git_info() -> tuple[str | None, str | None]:
    """Attempt to read the current git commit hash and project version.

    Returns:
        A tuple of ``(git_commit_hash, project_version)``, each possibly
        ``None`` if the information cannot be determined.
    """
    git_commit: str | None = None
    version: str | None = None

    try:
        import subprocess  # noqa: S404 — intentional git metadata read

        result = subprocess.run(  # noqa: S607
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parent.parent.parent,
        )
        if result.returncode == 0:
            git_commit = result.stdout.strip()
    except Exception:  # noqa: S110 — best-effort, safe to ignore
        pass

    try:
        import tomllib  # Python 3.11+

        pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
        if pyproject.exists():
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
            version = data.get("project", {}).get("version", None)
    except Exception:  # noqa: S110 — best-effort, safe to ignore
        pass

    return git_commit, version


def _flatten_messages(
    haystack_sessions: list[list[dict[str, Any]]],
) -> list[dict[str, str]]:
    """Flatten a list of sessions into a single message list.

    LongMemEval stores conversations as a list of sessions, each session
    being a list of ``{role, content}`` dicts.  This flattens them to a
    single list for ingestion.

    Args:
        haystack_sessions: List of sessions, each session being a list of
            message dicts.

    Returns:
        A single flat list of ``{"role": str, "content": str}`` dicts.
    """
    if not isinstance(haystack_sessions, list):
        raise TypeError(
            f"Expected list of sessions, got {type(haystack_sessions).__name__}. "
            "LongMemEval shape: haystack_sessions = [[{role, content}, ...], ...]"
        )

    flat: list[dict[str, str]] = []
    for session_idx, session in enumerate(haystack_sessions):
        if not isinstance(session, list):
            logger.warning(
                "Session %d is %s, expected list — skipping",
                session_idx,
                type(session).__name__,
            )
            continue
        for msg in session:
            flat.append({
                "role": msg.get("role", "user"),
                "content": msg.get("content", ""),
            })
    return flat


# ═══════════════════════════════════════════════════════════════════════════════
# Benchmark test
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_longmemeval_benchmark(
    openrouter_backend: LLMBackend,
    benchmark_config: SimpleNamespace,
    api_client: httpx.AsyncClient,
) -> None:
    """Run the LongMemEval benchmark end-to-end.

    This test:
    1. Loads the LongMemEval dataset
    2. Creates a project and ingests all conversations
    3. Waits for enrichment to complete
    4. For each question: runs search (R@k) and context (LLM-judge QA)
    5. If ``--baseline``: repeats with graph backend disabled
    6. Saves results as timestamped JSON and prints a comparison table

    Args:
        openrouter_backend: OpenRouter LLM backend for answer evaluation.
        benchmark_config: Parsed CLI options from the conftest.
        api_client: HTTP client configured with the benchmark API base URL.
    """
    variant = benchmark_config.variant or BENCHMARK_VARIANT
    limit = benchmark_config.benchmark_limit

    # ── Phase 0: Load dataset ──────────────────────────────────────────────
    dataset = load_dataset(variant)
    if limit and limit < len(dataset):
        logger.info(
            "Benchmark limit set to %d — using subset of %d questions",
            limit,
            len(dataset),
        )
        dataset = dataset[:limit]

    logger.info(
        "Starting LongMemEval benchmark: %d questions, variant=%s",
        len(dataset),
        variant,
    )

    # ── Phase 1: Authenticate ──────────────────────────────────────────────
    token = await _login(api_client)
    logger.info("Authenticated successfully")

    # ── Git metadata ───────────────────────────────────────────────────────
    git_commit, version = _get_git_info()

    # ── Run full pipeline ──────────────────────────────────────────────────
    results_full = await _run_benchmark_pipeline(
        api_client=api_client,
        token=token,
        dataset=dataset,
        openrouter_backend=openrouter_backend,
        reranker=benchmark_config.reranker,
        label="full",
    )

    # ── Run baseline (if requested) ────────────────────────────────────────
    results_baseline = None
    if benchmark_config.baseline:
        logger.info("Running baseline mode — disabling graph backend...")
        # Switch org config to disable graph backend
        await _set_org_graph_backend(api_client, token, graph_backend="none")
        try:
            results_baseline = await _run_benchmark_pipeline(
                api_client=api_client,
                token=token,
                dataset=dataset,
                openrouter_backend=openrouter_backend,
                reranker=False,
                label="baseline",
            )
        finally:
            # Restore graph backend
            await _set_org_graph_backend(api_client, token, graph_backend="postgres")

    # ── Compute metrics and output ─────────────────────────────────────────
    metrics_full = compute_accuracy(results_full)

    # Compute R@k for full
    r1_full = sum(1 for r in results_full if r.get("r1", False))
    r5_full = sum(1 for r in results_full if r.get("r5", False))
    r10_full = sum(1 for r in results_full if r.get("r10", False))
    total_full = len(results_full) if results_full else 1
    metrics_full["r1"] = r1_full / total_full
    metrics_full["r5"] = r5_full / total_full
    metrics_full["r10"] = r10_full / total_full

    metrics_baseline = None
    if results_baseline:
        metrics_baseline = compute_accuracy(results_baseline)
        r1_baseline = sum(1 for r in results_baseline if r.get("r1", False))
        r5_baseline = sum(1 for r in results_baseline if r.get("r5", False))
        r10_baseline = sum(1 for r in results_baseline if r.get("r10", False))
        total_baseline = len(results_baseline) if results_baseline else 1
        metrics_baseline["r1"] = r1_baseline / total_baseline
        metrics_baseline["r5"] = r5_baseline / total_baseline
        metrics_baseline["r10"] = r10_baseline / total_baseline

    # Save results
    saved_path = _save_results(results_full, benchmark_config, git_commit, version)
    if results_baseline:
        base_config = SimpleNamespace(
            run_benchmark=benchmark_config.run_benchmark,
            benchmark_limit=benchmark_config.benchmark_limit,
            baseline=False,
            reranker=False,
            variant=benchmark_config.variant,
        )
        baseline_path = _save_results(
            results_baseline, base_config, git_commit, version
        )
        # Rename to include _baseline suffix for clarity
        baseline_renamed = baseline_path.with_stem(baseline_path.stem + "_baseline")
        baseline_path.rename(baseline_renamed)
        logger.info("Baseline results saved to %s", baseline_renamed)

    # Print comparison table
    table = _build_comparison_table(metrics_full, metrics_baseline)
    print("\n" + "=" * 72)
    print("LongMemEval Benchmark Results")
    print("=" * 72)
    print(
        f"Variant: {variant}  |  Questions: {len(dataset)}  "
        f"|  Reranker: {benchmark_config.reranker}"
    )
    if benchmark_config.baseline:
        print("Baseline (pure vector): included")
    print()
    print(table)
    print()
    print(f"Results saved to: {saved_path}")
    print("=" * 72)


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline runner
# ═══════════════════════════════════════════════════════════════════════════════


async def _run_benchmark_pipeline(
    api_client: httpx.AsyncClient,
    token: str,
    dataset: list[dict[str, Any]],
    openrouter_backend: LLMBackend,
    reranker: bool,
    label: str = "run",
) -> list[dict[str, Any]]:
    """Execute a single benchmark pipeline run (ingest → enrich → query).

    Creates a project, ingests all conversations, waits for enrichment,
    then queries each question for R@k and QA accuracy.

    Args:
        api_client: Authenticated HTTP client.
        token: JWT access token.
        dataset: LongMemEval question entries.
        openrouter_backend: LLM backend for answer evaluation.
        reranker: Whether the reranker is enabled for this run.
        label: Short label for logging (e.g. ``"full"``, ``"baseline"``).

    Returns:
        List of per-question result dicts with keys: ``id``, ``question``,
        ``question_type``, ``correct``, ``reasoning``, ``r1``, ``r5``, ``r10``,
        ``context``, ``model_answer``.
    """
    # ── Create project ─────────────────────────────────────────────────────
    project_id = await _create_project(api_client, token)
    logger.info("[%s] Created project %s", label, project_id)

# ── Ingest all conversations ───────────────────────────────────────────
    # Each dataset entry gets its own session so retrieval is measured
    # across independent conversation contexts.
    ingested_count = 0
    for idx, entry in enumerate(dataset):
        entry_id = entry.get("question_id", f"entry_{idx}")
        haystack = entry.get("haystack_sessions", [])
        messages = _flatten_messages(haystack)
        if not messages:
            logger.warning(
                "[%s] Entry %s has empty messages — skipping",
                label,
                entry_id,
            )
            continue

        # Create a session for this entry
        session_id = await _create_session(
            api_client,
            token,
            project_id,
            external_id=f"longmemeval_{entry_id}",
        )

        # LongMemEval conversations may have many messages; batch if needed
        batch_size = 500
        for i in range(0, len(messages), batch_size):
            batch = messages[i : i + batch_size]
            await _ingest_memory(
                api_client, token, project_id, batch,
                session_id=session_id,
            )
            ingested_count += len(batch)

    logger.info(
        "[%s] Ingested %d messages across %d entries (1 session per entry)",
        label,
        ingested_count,
        len(dataset),
    )

    # ── Wait for enrichment ────────────────────────────────────────────
    logger.info("[%s] Waiting for enrichment to complete...", label)
    try:
        await _wait_for_enrichment(api_client, token, project_id)
    except TimeoutError:
        logger.warning(
            "[%s] Enrichment timed out — proceeding with partial data",
            label,
        )

    # ── Query each question ────────────────────────────────────────────
    results: list[dict[str, Any]] = []
    for idx, entry in enumerate(dataset):
        question = entry.get("question", "")
        question_id = entry.get("question_id", str(idx))
        # The LongMemEval-S dataset uses key "answer"; the oracle variant
        # uses "expected_answer".  Try both for compatibility.
        expected_answer = entry.get("answer", entry.get("expected_answer", ""))
        qtype = entry.get("question_type", "unknown")
        abstention = is_abstention(question_id)

        logger.info(
            "[%s] Query %d/%d: %s",
            label,
            idx + 1,
            len(dataset),
            question_id,
        )

        # R@k via search
        search_results = await _search(
            api_client, token, project_id, question, limit=10
        )

        r1 = compute_recall_at_k(search_results, expected_answer, k=1)
        r5 = compute_recall_at_k(search_results, expected_answer, k=5)
        r10 = compute_recall_at_k(search_results, expected_answer, k=10)

        # End-to-end QA via context → LLM answer → judge
        context_text = await _get_context(
            api_client, token, project_id, question, limit=20
        )

        try:
            model_answer = await _answer_from_context(
                backend=openrouter_backend,
                question=question,
                context=context_text,
                is_abstention=abstention,
            )
            judge_result: EvaluationResult = await evaluate_answer(
                backend=openrouter_backend,
                question=question,
                expected_answer=expected_answer,
                model_answer=model_answer,
                is_abstention=abstention,
                temperature=0.0,
                max_tokens=512,
            )
            correct = judge_result.correct
            reasoning = judge_result.reasoning
        except LLMStructuredOutputError as exc:
            logger.warning(
                "[%s] Judge LLM failed for %s: %s — marking incorrect",
                label,
                question_id,
                exc,
            )
            correct = False
            reasoning = f"Judge LLM error: {exc}"
            model_answer = ""

        result_entry = {
            "id": question_id,
            "question": question,
            "question_type": qtype,
            "expected_answer": expected_answer,
            "abstention": abstention,
            "correct": correct,
            "reasoning": reasoning,
            "r1": r1,
            "r5": r5,
            "r10": r10,
            "search_result_count": len(search_results),
            "model_answer": model_answer,
        }
        results.append(result_entry)

        # Incremental save every 10 questions to protect against data loss
        if (idx + 1) % 10 == 0:
            _TMP_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path = _TMP_RESULTS_DIR / f"{label}_partial_{idx+1}.json"
            with open(tmp_path, "w") as f:
                json.dump(results, f, indent=2, default=str)
            logger.info(
                "[%s] Progress: %d/%d questions — saved partial results to %s",
                label,
                idx + 1,
                len(dataset),
                tmp_path,
            )

        return results
