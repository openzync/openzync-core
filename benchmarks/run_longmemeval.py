"""LongMemEval benchmark — measures retrieval quality.

This harness queries a live OpenZync instance whose data is already
ingested and enriched outside the harness, waits for no enrichment by
default, runs the search and context endpoints, and evaluates both R@k
(recall at k) and retrieval-judge accuracy. Every question is
checkpointed, so an interrupted run resumes instead of restarting.

The harness is query-only by default: it reuses a project that already
holds data and raises ``BenchmarkConfigError`` (exit 2) instead of
ingesting when ingestion would be required.  Pass ``--ingest`` to permit
ingesting dataset conversations into a new or empty project.

Usage:
    # Query-only run against pre-ingested data (default):
    python -m benchmarks

    # Permit ingestion into a new or empty project:
    python -m benchmarks --ingest

    # Quick run (10 questions):
    python -m benchmarks --benchmark-limit 10

    # With baseline comparison (pure vector only):
    python -m benchmarks --baseline

    # With reranker enabled:
    python -m benchmarks --reranker

    # Judge 8 questions concurrently (same LLM cost, shorter wall-clock):
    python -m benchmarks --workers 8

    # Oracle variant:
    python -m benchmarks --variant oracle

    # Resume an interrupted run (auto-resumes the newest matching
    # checkpoint, or points at one manifest explicitly):
    python -m benchmarks
    python -m benchmarks --resume \
        benchmarks/results/.in_progress/<manifest>.json

    # Ignore existing checkpoints and start over:
    python -m benchmarks --fresh
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx

from benchmarks import display
from benchmarks.checkpoint import RESULTS_DIR, Checkpoint
from benchmarks.cli import (
    BenchmarkConfigError,
    build_fingerprint,
    dataset_question_ids,
    get_git_info,
    resolve_checkpoint,
)
from benchmarks.longmemeval_evaluator import EvaluationResult, evaluate_retrieval
from benchmarks.longmemeval_utils import (
    compute_accuracy,
    compute_recall_at_k,
    is_abstention,
)
from core.llm import LLMStructuredOutputError

if TYPE_CHECKING:
    from core.llm import LLMBackend

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


# ═══════════════════════════════════════════════════════════════════════════════
# Private helpers
# ═══════════════════════════════════════════════════════════════════════════════

# Retryable HTTP status codes
_RETRYABLE_STATUSES: set[int] = {429, 502, 503, 504}

# httpx encodes ``data`` as ``application/x-www-form-urlencoded`` unless
# ``files`` is truthy — an empty list/dict silently downgrades a multipart
# call. ``_EMPTY_FILES`` is truthy yet iterates to zero parts, forcing
# genuine multipart encoding with no file parts. Mirrors the SDK's
# ``openzync._http._EMPTY_FILES`` sentinel.
_EMPTY_FILES = iter(())


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

    # All retries exhausted — re-raise the last exception with context
    if isinstance(last_exc, httpx.HTTPStatusError):
        raise httpx.HTTPStatusError(
            f"Request failed after {max_retries} retries: {last_exc}",
            request=last_exc.request,
            response=last_exc.response,
        ) from last_exc
    if isinstance(last_exc, httpx.RequestError):
        raise httpx.HTTPStatusError(
            f"Request failed after {max_retries} retries: {last_exc}",
            request=last_exc.request,
            response=None,  # type: ignore[arg-type]
        ) from last_exc
    raise RuntimeError(f"Request failed after {max_retries} retries") from last_exc


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
        raise RuntimeError("BENCH_EMAIL and BENCH_PASSWORD must be set in environment")

    resp = await _request_with_retry(
        client,
        "POST",
        "/v1/auth/login",
        json={"email": email, "password": password},
    )
    data: dict = resp.json()
    return str(data["access_token"])


def _auth_header(token: str) -> dict[str, str]:
    """Return an Authorization header dict for a JWT token."""
    return {"Authorization": f"Bearer {token}"}


BENCHMARK_PROJECT_PREFIX: str = "longmemeval-benchmark"
"""Prefix for persistent benchmark project names.

The full name is ``{prefix}-{variant}`` (e.g. ``longmemeval-benchmark-s``).
Data persists across runs so enrichment is performed once and reused.
"""


async def _find_project_by_name(
    client: httpx.AsyncClient, token: str, name: str
) -> str | None:
    """Look up a project by name.  Returns its ID, or ``None`` if not found.

    Only returns non-archived projects.
    """
    resp = await _request_with_retry(
        client,
        "GET",
        "/v1/projects",
        params={"limit": 200},
        headers=_auth_header(token),
    )
    for project in resp.json():
        if project.get("name") == name and not project.get("is_archived", False):
            return str(project["id"])
    return None


async def _project_has_sessions(
    client: httpx.AsyncClient, token: str, project_id: str
) -> bool:
    """Check whether a project holds any sessions.

    Presence guard for query-only mode: a found project is trusted as
    complete only when it is non-empty. Closed sessions count because
    their episodes/facts remain retrievable, and the dashboard passes
    the same ``include_closed`` flag — do not drop it to "simplify".

    Args:
        client: Authenticated HTTP client.
        token: JWT access token.
        project_id: Project UUID to probe.

    Returns:
        True when at least one session exists, False otherwise.
    """
    resp = await _request_with_retry(
        client,
        "GET",
        f"/v1/projects/{project_id}/sessions",
        params={"limit": 1, "include_closed": "true"},
        headers=_auth_header(token),
    )
    payload: Any = resp.json()
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        raw_items = payload.get("data", payload.get("items", []))
        items = raw_items if isinstance(raw_items, list) else []
    else:
        items = []
    return len(items) > 0


async def _ensure_project(
    client: httpx.AsyncClient,
    token: str,
    variant: str,
    checkpoint: Checkpoint,
    allow_ingest: bool,
) -> tuple[str, bool]:
    """Get or create a persistent benchmark project.

    The harness is query-only by default: backend data is fully
    ingested/enriched and managed outside the harness, so ingestion runs
    only when ``allow_ingest`` (``--ingest``) is set.  Without the flag
    every branch that would require ingestion raises
    ``BenchmarkConfigError`` instead of ingesting or skipping silently.

    Resolution order:
    0. Project id recorded in the checkpoint manifest (resumed runs) —
       reused without ingestion only when the manifest's ``ingested``
       flag is set; a manifest checkpointed mid-ingest re-runs ingest
       into the same project (``is_new=True`` with the pinned id) when
       ``allow_ingest``, else raises naming the manifest path and the
       project id (re-run WITH ``--ingest`` to complete ingestion, or
       ``--fresh`` to start over)
    1. Look for project named ``longmemeval-benchmark-{variant}`` —
       non-empty projects are trusted as complete (``is_new=False``);
       empty projects raise without the flag, or ingest into the pinned
       existing id with the flag
    2. Fall back to any non-archived project with ``"longmemeval"`` in
       name — same empty/non-empty rules as priority 1
    3. Create a new project with the deterministic name — raises naming
       ``longmemeval-benchmark-{variant}`` without the flag, creates
       with it (``is_new=True``)

    In no-flag mode the operator asserts pre-existing project data is
    complete — the harness trusts a non-empty project without verifying
    per-entry coverage.

    Args:
        client: Authenticated HTTP client.
        token: JWT access token.
        variant: Dataset variant (``"s"``, ``"oracle"``, etc.).
        checkpoint: Checkpoint manifest — supplies a known project id on
            resume and records the resolved id for future resumes.
        allow_ingest: Whether ingestion is permitted (``--ingest``).
            False makes every ingestion-requiring branch a hard error.

    Returns:
        A tuple of ``(project_id, is_new)`` where ``is_new`` is ``True``
        if the project needs ingestion + enrichment (just created, an
        empty pre-existing project ingested into with the flag, or a
        resumed manifest whose ingest never completed with the flag).

    Raises:
        BenchmarkConfigError: If ingestion would be required but
            ``allow_ingest`` is False.
    """
    # Priority 0: project id pinned by a resumed checkpoint manifest —
    # no project listing needed. The ingested flag decides whether the
    # data is complete: without it the run was killed mid-ingest and
    # must re-ingest into the same project, not skip to querying.
    if checkpoint.project_id is not None:
        if checkpoint.ingested:
            logger.info(
                "Reusing project %s from checkpoint manifest — skipping lookup",
                checkpoint.project_id,
            )
            return checkpoint.project_id, False
        if not allow_ingest:
            raise BenchmarkConfigError(
                f"Checkpoint {checkpoint.path} pins project "
                f"{checkpoint.project_id} but ingest never completed for "
                "this project — re-run WITH --ingest to complete ingestion "
                "(or --fresh to start over)."
            )
        logger.warning(
            "Checkpoint %s pins project %s but ingest never completed "
            "(interrupted mid-ingest) — re-running ingest into the same "
            "project instead of querying partial data",
            checkpoint.path,
            checkpoint.project_id,
        )
        return checkpoint.project_id, True

    project_name = f"{BENCHMARK_PROJECT_PREFIX}-{variant}"

    # Priority 1: exact match by name
    existing = await _find_project_by_name(client, token, project_name)
    if existing is not None:
        if await _project_has_sessions(client, token, existing):
            logger.warning(
                "Reusing project %s (%s) — trusting pre-existing data, "
                "completeness is operator-managed",
                project_name,
                existing,
            )
            checkpoint.set_project_id(existing)
            # ⚠️ Deliberate residual hole: --fresh on a partial project
            # re-trusts it here. Closed fully only by future per-entry
            # ingest tracking.
            # REQUIRED: without this, every questioning-phase kill on a
            # pre-existing project resumes with ingested=False and hits
            # the priority-0 hard error above.
            checkpoint.mark_ingested()
            return existing, False
        if not allow_ingest:
            raise BenchmarkConfigError(
                f"Project {project_name} ({existing}) exists but holds no "
                "sessions — ingestion would be required. Re-run WITH "
                "--ingest to ingest into it."
            )
        logger.warning(
            "Project %s (%s) exists but is empty — ingesting into the "
            "existing project (zero sessions, no conflict risk)",
            project_name,
            existing,
        )
        checkpoint.set_project_id(existing)
        return existing, True

    # Priority 2: any non-archived project with "longmemeval" in its name
    # (catches legacy project names like longmemeval-1783604743)
    resp = await _request_with_retry(
        client,
        "GET",
        "/v1/projects",
        params={"limit": 200},
        headers=_auth_header(token),
    )
    for project in resp.json():
        if project.get("is_archived", False):
            continue
        name = project.get("name", "")
        if "longmemeval" in name.lower():
            pid = str(project["id"])
            if await _project_has_sessions(client, token, pid):
                logger.warning(
                    "Reusing existing project %s (%s) — trusting "
                    "pre-existing data, completeness is operator-managed",
                    name,
                    pid,
                )
                checkpoint.set_project_id(pid)
                # ⚠️ Deliberate residual hole: --fresh on a partial
                # project re-trusts it here. Closed fully only by future
                # per-entry ingest tracking.
                # REQUIRED: without this, every questioning-phase kill on
                # a pre-existing project resumes with ingested=False and
                # hits the priority-0 hard error above.
                checkpoint.mark_ingested()
                return pid, False
            if not allow_ingest:
                raise BenchmarkConfigError(
                    f"Project {name} ({pid}) exists but holds no sessions "
                    "— ingestion would be required. Re-run WITH --ingest "
                    "to ingest into it."
                )
            logger.warning(
                "Project %s (%s) exists but is empty — ingesting into the "
                "existing project (zero sessions, no conflict risk)",
                name,
                pid,
            )
            checkpoint.set_project_id(pid)
            return pid, True

    # Priority 3: create new
    if not allow_ingest:
        raise BenchmarkConfigError(
            f"Project {project_name} not found — ingestion would be "
            "required. Re-run WITH --ingest to create and ingest it."
        )
    resp = await _request_with_retry(
        client,
        "POST",
        "/v1/projects",
        json={"name": project_name},
        headers=_auth_header(token),
    )
    data: dict = resp.json()
    project_id = str(data["id"])
    logger.info("Created new project %s (%s)", project_name, project_id)
    checkpoint.set_project_id(project_id)
    return project_id, True


async def _set_org_graph_backend(
    client: httpx.AsyncClient, token: str, graph_backend: str = "falkordb"
) -> dict[str, Any]:
    """Update the org-level graph backend configuration.

    Used to toggle between full pipeline and baseline (no graph) modes.

    Args:
        client: Authenticated HTTP client.
        token: JWT access token.
        graph_backend: ``"falkordb"`` (full) or ``"none"`` (baseline).

    Returns:
        The updated org config response.
    """
    resp = await _request_with_retry(
        client,
        "PATCH",
        "/admin/platform-config",
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
        client,
        "POST",
        f"/v1/projects/{project_id}/sessions",
        json={"external_id": external_id},
        headers=_auth_header(token),
    )
    data: dict = resp.json()
    return str(data["id"])


async def _ingest_memory(
    client: httpx.AsyncClient,
    token: str,
    project_id: str,
    messages: list[dict[str, str]],
    session_external_id: str,
) -> None:
    """Ingest a batch of messages into a session within a project.

    The session must already exist — the server never auto-creates
    sessions from arbitrary IDs, so a missing ``session_external_id``
    is a 422.

    Enrichment runs asynchronously in the background.

    Args:
        client: Authenticated HTTP client.
        token: JWT access token.
        project_id: Target project UUID.
        messages: List of ``{"role": str, "content": str}`` dicts.
        session_external_id: Session EXTERNAL id to ingest into (the
            ``IngestMemoryRequest.session_id`` field carries the external
            id, not the internal UUID).
    """
    body: dict[str, object] = {
        "messages": messages,
        "session_id": session_external_id,
    }

    # Always multipart — the backend accepts only multipart/form-data
    # (``data: str = Form(...)``), even for text-only calls; a plain
    # JSON body is rejected with 422. ``files`` must be truthy or httpx
    # silently downgrades to urlencoded — hence the ``_EMPTY_FILES``
    # sentinel (mirrors the SDK's ``request_multipart``).
    try:
        await _request_with_retry(
            client,
            "POST",
            f"/v1/projects/{project_id}/memory",
            data={"data": json.dumps(body)},
            files=_EMPTY_FILES,
            headers=_auth_header(token),
        )
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Ingest failed: HTTP %d on POST /v1/projects/%s/memory — body: %s",
            exc.response.status_code,
            project_id,
            exc.response.text,
        )
        raise


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
    logger.info("Waiting 10s for worker to pick up enrichment tasks before polling...")
    await asyncio.sleep(10)

    deadline = time.monotonic() + ENRICHMENT_TIMEOUT_S
    last_logged: int = -1

    while time.monotonic() < deadline:
        resp = await _request_with_retry(
            client,
            "GET",
            "/metrics/summary",
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
        client,
        "GET",
        f"/v1/projects/{project_id}/search",
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
        client,
        "GET",
        f"/v1/projects/{project_id}/context",
        params={"query": query, "limit": limit, "format": "text"},
        headers=_auth_header(token),
    )
    data: dict[str, Any] = resp.json()
    return str(data.get("context", ""))


def build_comparison_rows(
    metrics_full: dict[str, Any],
    metrics_baseline: dict[str, Any] | None,
    *,
    reranker: bool,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Build plain-data comparison rows for the benchmark results report.

    Same numbers and strings the old markdown table carried —
    percentages pre-formatted (``"62.5%"``), reference rows identical.
    Presentation (rich tables) lives in ``benchmarks.display``; this
    function only shapes the data.

    The baseline row is emitted if and only if ``metrics_baseline`` was
    provided AND contains a non-``None`` ``overall_accuracy`` — a bare
    ``{}`` or a dict without accuracy yields no baseline row.

    Reference numbers (published):
        - Zep LongMemEval-S: 90.2% (451/500)
        - Zep LoCoMo: 94.7%
        - Mem0 LongMemEval: 94.4% (new algorithm; old: 67.8%)

    Sources, verified 2026-10-10:
        - https://www.getzep.com/research/
        - https://mem0.ai/blog/mem0-the-token-efficient-memory-algorithm

    These are vendor-published figures, measured with differing
    judge/reader stacks — not apples-to-apples with each other or with
    this harness's own numbers.

    Args:
        metrics_full: Results dict from the full pipeline run.
        metrics_baseline: Optional results dict from the baseline run.
        reranker: Whether the reranker was enabled — gates the
            ``", RRF + reranker"`` suffix on the full-pipeline
            conditions string.

    Returns:
        A ``(system_rows, category_rows)`` pair. System rows carry keys
        ``system``, ``accuracy``, ``r1``, ``r5``, ``r10``,
        ``conditions``; category rows carry ``category``, ``accuracy``,
        ``count`` — all values pre-formatted strings. Malformed
        per-category entries are skipped with a warning, never a
        ``KeyError``.
    """
    accuracy_baseline = (
        metrics_baseline.get("overall_accuracy") if metrics_baseline else None
    )

    system_rows = [
        {
            "system": "OpenZync (full)",
            "accuracy": f"{metrics_full.get('overall_accuracy', 0.0):.1%}",
            "r1": f"{metrics_full.get('r1', 0):.1%}",
            "r5": f"{metrics_full.get('r5', 0):.1%}",
            "r10": f"{metrics_full.get('r10', 0):.1%}",
            "conditions": (
                "LongMemEval-S, RRF + reranker" if reranker else "LongMemEval-S"
            ),
        }
    ]

    # OpenZync baseline (if available)
    if accuracy_baseline is not None and metrics_baseline is not None:
        system_rows.append(
            {
                "system": "OpenZync (baseline)",
                "accuracy": f"{accuracy_baseline:.1%}",
                "r1": f"{metrics_baseline.get('r1', 0):.1%}",
                "r5": f"{metrics_baseline.get('r5', 0):.1%}",
                "r10": f"{metrics_baseline.get('r10', 0):.1%}",
                "conditions": "Pure vector only",
            }
        )

    # Published reference numbers
    system_rows.append(
        {
            "system": "Zep",
            "accuracy": "90.2%",
            "r1": "—",
            "r5": "—",
            "r10": "—",
            "conditions": "LongMemEval-S, reader gpt-5.4, cross-encoder",
        }
    )
    system_rows.append(
        {
            "system": "Zep",
            "accuracy": "94.7%",
            "r1": "—",
            "r5": "—",
            "r10": "—",
            "conditions": "LoCoMo",
        }
    )
    system_rows.append(
        {
            "system": "Mem0",
            "accuracy": "94.4%",
            "r1": "—",
            "r5": "—",
            "r10": "—",
            "conditions": "LongMemEval, new algorithm (old: 67.8%)",
        }
    )

    # Per-category breakdown — malformed entries are skipped with a
    # warning so one bad entry cannot KeyError a finished run.
    category_rows: list[dict[str, str]] = []
    per_category = metrics_full.get("per_category", {})
    if not isinstance(per_category, dict):
        logger.warning(
            "skipping per_category breakdown: expected dict, got %s",
            type(per_category).__name__,
        )
    else:
        for cat, stats in sorted(per_category.items()):
            if not isinstance(stats, dict):
                logger.warning(
                    "skipping malformed per_category entry %r: expected dict, got %s",
                    cat,
                    type(stats).__name__,
                )
                continue
            accuracy = stats.get("accuracy")
            total = stats.get("total")
            if accuracy is None or total is None:
                logger.warning(
                    "skipping malformed per_category entry %r: "
                    "missing 'accuracy' or 'total'",
                    cat,
                )
                continue
            category_rows.append(
                {
                    "category": cat,
                    "accuracy": f"{accuracy:.1%}",
                    "count": str(total),
                }
            )

    return system_rows, category_rows


def _save_results(
    results: list[dict[str, Any]],
    config: SimpleNamespace,
    git_commit: str | None,
    version: str | None,
    checkpoint: Checkpoint,
    judge_model: str | None = None,
) -> Path:
    """Save benchmark results to a timestamped JSON file.

    Args:
        results: List of per-question result dicts.
        config: Benchmark configuration from CLI args.
        git_commit: Current git commit hash.
        version: OpenZync version string.
        checkpoint: Checkpoint manifest of this run — its ``started_at``
            stamps the output file, so a resumed run keeps the original
            run's timestamp.
        judge_model: Judge backend model name for the config label.
            Falls back to ``NVIDIA_MODEL`` env (default NVIDIA model)
            when not provided.

    Returns:
        Path to the saved results file.
    """
    started_at = datetime.fromisoformat(checkpoint.started_at)
    timestamp = started_at.strftime("%Y%m%d_%H%M%S")
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
            "llm_judge_model": judge_model
            or os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3-ultra-550b-a55b"),
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
            flat.append(
                {
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                }
            )
    return flat


# ═══════════════════════════════════════════════════════════════════════════════
# Benchmark entry point
# ═══════════════════════════════════════════════════════════════════════════════


async def run_benchmark(
    cfg: SimpleNamespace,
    llm_backend: LLMBackend,
    api_client: httpx.AsyncClient,
    checkpoint: Checkpoint,
    dataset: list[dict[str, Any]],
) -> None:
    """Run the LongMemEval benchmark end-to-end.

    This entry point:
    1. Uses the caller-supplied dataset (already limit-sliced by
       ``cli.main`` — the single load for the whole run)
    2. Resolves the persistent project query-only by default (ingests
       only with ``--ingest``)
    3. Waits for enrichment to complete (ingest path only)
    4. For each question: runs search (R@k) and judges the retrieved
       context directly against ground truth, checkpointing after every
       question
    5. If ``--baseline``: repeats with graph backend disabled
    6. Saves results as timestamped JSON and prints a comparison table

    Args:
        cfg: Parsed CLI options — ``variant``, ``benchmark_limit``,
            ``baseline``, ``reranker``, ``ingest``, and ``workers``.
        llm_backend: LLM backend for retrieval judging.
        api_client: HTTP client configured with the benchmark API base URL.
        checkpoint: Checkpoint manifest for the full-pipeline run.
        dataset: LongMemEval question entries, already sliced to
            ``cfg.benchmark_limit`` by the caller.
    """
    variant = cfg.variant or BENCHMARK_VARIANT
    limit = cfg.benchmark_limit

    logger.info(
        "Starting LongMemEval benchmark: %d questions, variant=%s",
        len(dataset),
        variant,
    )
    question_ids = dataset_question_ids(dataset)

    # ── Phase 1: Authenticate ──────────────────────────────────────────────
    token = await _login(api_client)
    logger.info("Authenticated successfully")

    # ── Git metadata ───────────────────────────────────────────────────────
    git_commit, version = get_git_info()

    # ── Run header (once per invocation — no second header for baseline) ──
    resumed_ids = checkpoint.completed_ids()
    display.print_run_header(
        variant=variant,
        limit=limit,
        reranker=cfg.reranker,
        baseline=cfg.baseline,
        judge_model=llm_backend.model_name,
        manifest_path=checkpoint.path,
        resumed=bool(resumed_ids),
        completed_count=len(resumed_ids),
        total_count=len(dataset),
    )

    # ── Run full pipeline ──────────────────────────────────────────────────
    results_full = await _run_benchmark_pipeline(
        api_client=api_client,
        token=token,
        dataset=dataset,
        openai_backend=llm_backend,
        reranker=cfg.reranker,
        variant=variant,
        label="full",
        checkpoint=checkpoint,
        allow_ingest=cfg.ingest,
        workers=cfg.workers,
    )

    # ── Run baseline (if requested) ────────────────────────────────────────
    results_baseline = None
    baseline_checkpoint: Checkpoint | None = None
    if cfg.baseline:
        logger.info("Running baseline mode — disabling graph backend...")
        # Switch org config to disable graph backend
        await _set_org_graph_backend(api_client, token, graph_backend="none")
        try:
            # Separate manifest (label "baseline") so the two runs never
            # cross-resume — the fingerprint includes the label.
            baseline_fingerprint = build_fingerprint(
                variant=variant,
                label="baseline",
                reranker=False,
                judge_model=llm_backend.model_name,
                git_commit=git_commit,
                question_ids=question_ids,
                limit=limit,
            )
            # The user's --resume path (if any) names a full-run manifest
            # and can never match the baseline fingerprint — resolving with
            # the full cfg would die with exit 2 only AFTER the hours-long
            # full pipeline above. Resolve by auto-discovery (or --fresh).
            baseline_args = SimpleNamespace(resume=None, fresh=cfg.fresh)
            baseline_checkpoint = resolve_checkpoint(
                baseline_fingerprint, baseline_args
            )
            logger.info("Baseline checkpoint manifest: %s", baseline_checkpoint.path)
            results_baseline = await _run_benchmark_pipeline(
                api_client=api_client,
                token=token,
                dataset=dataset,
                openai_backend=llm_backend,
                reranker=False,
                variant=f"{variant}-baseline",
                label="baseline",
                checkpoint=baseline_checkpoint,
                allow_ingest=cfg.ingest,
                workers=cfg.workers,
            )
        finally:
            # Restore graph backend
            await _set_org_graph_backend(api_client, token, graph_backend="falkordb")

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
    saved_path = _save_results(
        results_full,
        cfg,
        git_commit,
        version,
        checkpoint,
        judge_model=llm_backend.model_name,
    )
    if results_baseline and baseline_checkpoint is not None:
        base_config = SimpleNamespace(
            benchmark_limit=cfg.benchmark_limit,
            baseline=False,
            reranker=False,
            variant=cfg.variant,
        )
        baseline_path = _save_results(
            results_baseline,
            base_config,
            git_commit,
            version,
            baseline_checkpoint,
            judge_model=llm_backend.model_name,
        )
        # Rename to include _baseline suffix for clarity
        baseline_renamed = baseline_path.with_stem(baseline_path.stem + "_baseline")
        baseline_path.rename(baseline_renamed)
        logger.info("Baseline results saved to %s", baseline_renamed)

    # Results are durable on disk — the manifests can now be closed out.
    # They stay in .in_progress/ for audit (see Checkpoint.mark_completed).
    checkpoint.mark_completed()
    if baseline_checkpoint is not None:
        baseline_checkpoint.mark_completed()

    # Print comparison report (stdout; logging on stderr is untouched)
    system_rows, category_rows = build_comparison_rows(
        metrics_full, metrics_baseline, reranker=cfg.reranker
    )
    display.print_results(
        system_rows=system_rows,
        category_rows=category_rows,
        saved_path=saved_path,
        manifest_path=checkpoint.path,
        judge_errors=metrics_full["judge_errors"],
        graded_accuracy=metrics_full["graded_accuracy"],
        total=len(results_full),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline runner
# ═══════════════════════════════════════════════════════════════════════════════


async def _judge_one_question(
    *,
    idx: int,
    entry: dict[str, Any],
    question_id: str,
    api_client: httpx.AsyncClient,
    token: str,
    project_id: str,
    openai_backend: LLMBackend,
    label: str,
) -> dict[str, Any]:
    """Judge a single benchmark question (search R@k + retrieval judge).

    Pure computation + I/O — no shared-state mutation. The caller owns
    result recording (``results.append``), progress updates, and
    checkpoint persistence, so concurrent workers can share this safely.

    Args:
        idx: Dataset index of the question (logging context).
        entry: LongMemEval question entry.
        question_id: Stable question id.
        api_client: Authenticated HTTP client.
        token: JWT access token.
        project_id: Target project UUID.
        openai_backend: LLM backend for retrieval judging.
        label: Short label for logging (e.g. ``"full"``, ``"baseline"``).

    Returns:
        Per-question result dict with keys: ``id``, ``question``,
        ``question_type``, ``correct``, ``reasoning``, ``judge_error``,
        ``r1``, ``r5``, ``r10``, ``search_result_count``. ``judge_error``
        is True when the judge LLM failed (fail-closed: ``correct`` stays
        False); False on a genuine graded verdict.
    """
    question = entry.get("question", "")
    # The LongMemEval-S dataset uses key "answer"; the oracle variant
    # uses "expected_answer".  Try both for compatibility.
    raw_answer = entry.get("answer", entry.get("expected_answer", ""))
    # LongMemEval carries numeric answers as ints (32/500 in variant "s") —
    # the judge prompt and R@k both require str.
    expected_answer: str = "" if raw_answer is None else str(raw_answer)
    qtype = entry.get("question_type", "unknown")
    abstention = is_abstention(question_id)

    # R@k via search + retrieval context (independent idempotent GETs,
    # both retry internally).
    search_results, context_text = await asyncio.gather(
        _search(api_client, token, project_id, question, limit=10),
        _get_context(api_client, token, project_id, question, limit=20),
    )

    r1 = compute_recall_at_k(search_results, expected_answer, k=1)
    r5 = compute_recall_at_k(search_results, expected_answer, k=5)
    r10 = compute_recall_at_k(search_results, expected_answer, k=10)

    # The judge LLM grades the retrieved context directly against ground
    # truth — no intermediate model answer is generated.
    try:
        judge_result: EvaluationResult = await evaluate_retrieval(
            backend=openai_backend,
            question=question,
            expected_answer=expected_answer,
            context_text=context_text,
            is_abstention=abstention,
            temperature=0.0,
            max_tokens=512,
        )
        correct = judge_result.correct
        reasoning = judge_result.reasoning
        judge_error = False
    except LLMStructuredOutputError as exc:
        logger.warning(
            "[%s] Judge LLM failed for %s: %s — marking incorrect",
            label,
            question_id,
            exc,
        )
        correct = False
        reasoning = f"Judge LLM error: {exc}"
        judge_error = True

    return {
        "id": question_id,
        "question": question,
        "question_type": qtype,
        "expected_answer": expected_answer,
        "abstention": abstention,
        "correct": correct,
        "reasoning": reasoning,
        "judge_error": judge_error,
        "r1": r1,
        "r5": r5,
        "r10": r10,
        "search_result_count": len(search_results),
    }


async def _run_benchmark_pipeline(
    api_client: httpx.AsyncClient,
    token: str,
    dataset: list[dict[str, Any]],
    openai_backend: LLMBackend,
    reranker: bool,
    variant: str,
    label: str,
    checkpoint: Checkpoint,
    allow_ingest: bool,
    workers: int = 1,
) -> list[dict[str, Any]]:
    """Execute a single benchmark pipeline run (resolve → query, ingest if allowed).

    Uses a persistent project (``longmemeval-benchmark-{variant}``) whose
    data is managed outside the harness. Query-only by default: existing
    non-empty projects are trusted as complete and queried directly;
    ingestion runs only when ``allow_ingest`` (``--ingest``) permits it.

    The question-judge phase runs under ``asyncio.TaskGroup`` bounded by
    a semaphore: ``workers=1`` judges strictly in dataset order
    (sequential-equivalent); higher values shorten wall-clock only — LLM
    cost is identical (1 call per question). Result recording
    (append → progress → checkpoint save) is serialized under a lock so
    the bar and the manifest never disagree. Any unexpected error
    (anything other than a handled judge failure) propagates out of its
    worker, cancels its siblings via the ``TaskGroup``, and aborts the
    run loudly — the manifest keeps every completed question, so a rerun
    resumes instead of restarting.

    Args:
        api_client: Authenticated HTTP client.
        token: JWT access token.
        dataset: LongMemEval question entries.
        openai_backend: LLM backend for retrieval judging.
        reranker: Whether the reranker is enabled for this run.
        variant: Dataset variant (``"s"``, ``"oracle"``, etc.) — used
            to derive the persistent project name.
        label: Short label for logging (e.g. ``"full"``, ``"baseline"``).
        checkpoint: Checkpoint manifest for this run — supplies the
            resumed project id, the set of already-answered questions,
            and the per-question incremental persistence.
        allow_ingest: Whether ingestion is permitted (``--ingest``).
        workers: Max questions judged concurrently (``--workers``).

    Returns:
        List of per-question result dicts with keys: ``id``, ``question``,
        ``question_type``, ``correct``, ``reasoning``, ``r1``, ``r5``, ``r10``,
        ``search_result_count``.

    Raises:
        BenchmarkConfigError: If ingestion would be required but
            ``allow_ingest`` is False, or ``workers`` is below 1 (a
            zero-sized semaphore would deadlock instead of failing loud).
    """
    if workers < 1:
        raise BenchmarkConfigError(f"--workers must be between 1 and 32, got {workers}")
    # ── Get or create persistent project ──────────────────────────────────
    project_id, is_new = await _ensure_project(
        api_client, token, variant, checkpoint, allow_ingest
    )

    # Invariant: is_new is True only when --ingest was passed — every
    # ingestion-requiring branch in _ensure_project raises without it.
    if is_new:
        # ── Ingest all conversations ───────────────────────────────────────────
        # Each dataset entry gets its own session so retrieval is measured
        # across independent conversation contexts.
        ingested_count = 0
        with display.enrichment_status(f"[{label}] Ingesting conversations"):
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

                # Create a session for this entry (existence guarantee —
                # ingest below references the session by its external id).
                session_external_id = f"longmemeval_{entry_id}"
                await _create_session(
                    api_client,
                    token,
                    project_id,
                    external_id=session_external_id,
                )

                # LongMemEval conversations may have many messages; batch if needed
                batch_size = 500
                for i in range(0, len(messages), batch_size):
                    batch = messages[i : i + batch_size]
                    await _ingest_memory(
                        api_client,
                        token,
                        project_id,
                        batch,
                        session_external_id=session_external_id,
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
        with display.enrichment_status(f"[{label}] Waiting for enrichment"):
            try:
                await _wait_for_enrichment(api_client, token, project_id)
            except TimeoutError:
                logger.warning(
                    "[%s] Enrichment timed out — proceeding with partial data",
                    label,
                )
        # Ingest phase is done (all messages sent, enrichment waited out):
        # persist immediately so a resume never mistakes this project for
        # mid-ingest partial data. Set even after a timeout — the timeout
        # path knowingly proceeds to querying today.
        checkpoint.mark_ingested()
    else:
        logger.info(
            "[%s] Project %s already has data — skipping ingestion + enrichment",
            label,
            project_id,
        )

    # ── Query each question ────────────────────────────────────────────
    question_ids = dataset_question_ids(dataset)
    completed = checkpoint.completed_ids()
    if completed:
        logger.info(
            "[%s] Resuming from checkpoint %s — %d/%d questions complete",
            label,
            checkpoint.path,
            len(completed),
            len(dataset),
        )
    # Previously completed results lead the list so the running accuracy
    # and the saved file cover the whole run, not just this process.
    results: list[dict[str, Any]] = list(checkpoint.results)
    # Resumed manifests pre-fill the bar — skipped questions never advance
    # it, so the position always reflects judged questions.
    progress, task_id = display.make_question_progress(len(dataset))
    progress.update(task_id, completed=len(completed))

    # Bounded worker pool: one task per unanswered question, at most
    # ``workers`` judging concurrently. Workers only compute (pure I/O in
    # _judge_one_question); all shared-state recording stays in the
    # completion handler under ``state_lock``.
    state_lock = asyncio.Lock()
    sem = asyncio.Semaphore(workers)

    async def _run_one(idx: int, entry: dict[str, Any], question_id: str) -> None:
        """Judge one question, then record it under the shared-state lock."""
        async with sem:
            result_entry = await _judge_one_question(
                idx=idx,
                entry=entry,
                question_id=question_id,
                api_client=api_client,
                token=token,
                project_id=project_id,
                openai_backend=openai_backend,
                label=label,
            )
            async with state_lock:
                results.append(result_entry)

                # ── Real-time progress ──────────────────────────────
                # The bar carries correct answers; failures get one red line.
                fields = display.format_progress_fields(
                    sum(1 for r in results if r.get("correct", False)),
                    len(results),
                    sum(1 for r in results if r.get("r1", False)),
                    sum(1 for r in results if r.get("r5", False)),
                    sum(1 for r in results if r.get("r10", False)),
                )
                # Same-thread coroutines only, and rich's update performs
                # no awaits internally — safe to call under the lock.
                progress.update(
                    task_id,
                    advance=1,
                    acc=fields["acc"],
                    r1=fields["r1"],
                    r5=fields["r5"],
                    r10=fields["r10"],
                )
                if not result_entry["correct"]:
                    display.question_failed(question_id, result_entry["reasoning"])

                # Checkpoint after EVERY question — the manifest (not the old
                # write-only partial files) is the resume point for a rerun.
                checkpoint.append_result(result_entry)
                checkpoint.save()

    with progress:
        async with asyncio.TaskGroup() as tg:
            # Invariant: with workers=1 the semaphore admits tasks in
            # creation (dataset) order, so execution is sequential-equivalent.
            for idx, entry in enumerate(dataset):
                question_id = question_ids[idx]

                if question_id in completed:
                    logger.info(
                        "[%s] Skipping %s — already complete in checkpoint",
                        label,
                        question_id,
                    )
                    continue

                logger.info(
                    "[%s] Query %d/%d: %s",
                    label,
                    idx + 1,
                    len(dataset),
                    question_id,
                )
                tg.create_task(_run_one(idx, entry, question_id))

    # Completion order under concurrency is nondeterministic — restore
    # canonical dataset order before saving. The manifest keeps completion
    # order (its set-based skip resume tolerates any order — unchanged).
    order = {qid: i for i, qid in enumerate(question_ids)}
    unknown_ids = [r.get("id") for r in results if r.get("id") not in order]
    if unknown_ids:
        # Zero-fallback: an unknown result id indicates a bug — warn loudly.
        logger.warning(
            "[%s] %d result(s) with ids outside the dataset order: %s",
            label,
            len(unknown_ids),
            unknown_ids,
        )
    results.sort(key=lambda r: order.get(str(r.get("id")), len(order)))

    return results
