"""Shared fixtures and CLI options for the LongMemEval benchmark suite.

Benchmark tests are slow — they call the actual LLM backend against a
long-term-memory evaluation dataset.  They are skipped by default and only
run when ``--run-benchmark`` is passed to pytest.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx
import pytest

from core.llm_backends import OpenRouterBackend

if TYPE_CHECKING:
    from collections.abc import Generator

# ── Load local .env file (if available) ───────────────────────────────────────
# This allows benchmark credentials (BENCH_EMAIL, OZ_OPENROUTER_API_KEY, etc.)
# to be set in tests/benchmarks/.env without polluting the root .env.
try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).resolve().parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass  # python-dotenv not installed — user must export env vars manually

logger = logging.getLogger(__name__)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add benchmark-specific CLI options to pytest.

    Options:
        ``--run-benchmark`` — enable LongMemEval benchmark tests.
        ``--benchmark-limit`` — limit the number of questions (default: all).
        ``--baseline`` — run pure vector baseline for comparison.
        ``--reranker`` — run with cross-encoder reranker enabled.
        ``--variant`` — dataset variant: ``"s"`` (small, default) or ``"oracle"``.
    """
    parser.addoption(
        "--run-benchmark",
        action="store_true",
        default=False,
        help="Run LongMemEval benchmark tests (slow, requires LLM backend)",
    )
    parser.addoption(
        "--benchmark-limit",
        type=int,
        default=None,
        help="Limit number of questions for quick runs (default: all)",
    )
    parser.addoption(
        "--baseline",
        action="store_true",
        default=False,
        help="Run pure vector baseline for comparison (no reranker)",
    )
    parser.addoption(
        "--reranker",
        action="store_true",
        default=False,
        help="Run with cross-encoder reranker enabled",
    )
    parser.addoption(
        "--variant",
        type=str,
        default="s",
        help="Dataset variant: 's' (small, default) or 'oracle'",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``benchmark`` marker."""
    config.addinivalue_line(
        "markers",
        "benchmark: LongMemEval benchmark test (slow, requires LLM backend)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip benchmark tests by default unless ``--run-benchmark`` is passed."""
    if not config.getoption("--run-benchmark"):
        for item in items:
            if "benchmark" in item.keywords:
                item.add_marker(
                    pytest.mark.skip(
                        reason="use --run-benchmark to run benchmark tests"
                    )
                )


@pytest.fixture(scope="session")
def openrouter_backend() -> Generator[OpenRouterBackend, None, None]:
    """Create an OpenRouter LLM backend for benchmark evaluation.

    Reads ``OZ_OPENROUTER_API_KEY`` from the environment.  Skips all
    dependent tests if the key is not set.

    Yields:
        A configured ``OpenRouterBackend`` instance pointed at the
        non-free ``openai/gpt-oss-120b`` model for reliable throughput.

    Raises:
        pytest.skip: If ``OZ_OPENROUTER_API_KEY`` is not set.
    """
    api_key = os.environ.get("OZ_OPENROUTER_API_KEY")
    if not api_key:
        logger.warning("OZ_OPENROUTER_API_KEY not set — skipping benchmark tests")
        pytest.skip("OZ_OPENROUTER_API_KEY not set — cannot run benchmark")
        # The yield below is unreachable but satisfies the type checker.
        # Generator return type allows the early skip via exception.
        yield None  # type: ignore[func-returns-value]
        return

    # Use the non-free model for reliable benchmark results.
    # The :free tier may have degraded availability or rate limits
    # that would skew benchmark measurements.
    backend = OpenRouterBackend(api_key=api_key, model="openai/gpt-oss-120b")
    yield backend


@pytest.fixture(scope="function")
def benchmark_config(request: pytest.FixtureRequest) -> SimpleNamespace:
    """Expose parsed benchmark CLI options as a convenient namespace.

    Provides attribute access to:
        - ``run_benchmark`` (bool)
        - ``benchmark_limit`` (int | None)
        - ``baseline`` (bool)
        - ``reranker`` (bool)
        - ``variant`` (str)

    Returns:
        A ``SimpleNamespace`` with all parsed CLI option values.
    """
    return SimpleNamespace(
        run_benchmark=request.config.getoption("--run-benchmark"),
        benchmark_limit=request.config.getoption("--benchmark-limit"),
        baseline=request.config.getoption("--baseline"),
        reranker=request.config.getoption("--reranker"),
        variant=request.config.getoption("--variant"),
    )


@pytest.fixture(scope="session")
def api_client() -> httpx.AsyncClient:
    """Create an ``httpx.AsyncClient`` pointed at the OpenZync API.

    Base URL is read from ``OPENZYNC_BASE_URL``
    (default: ``http://localhost:8000``).

    Session-scoped: one client for the entire benchmark run.  Cleanup
    is handled by garbage collection at process exit — avoids the
    ``Event loop is closed`` teardown race in pytest-asyncio.

    Returns:
        An ``httpx.AsyncClient`` configured with a default 60 s timeout.
    """
    base_url = os.environ.get("OPENZYNC_BASE_URL", "http://localhost:8000")
    return httpx.AsyncClient(base_url=base_url, timeout=60.0)
