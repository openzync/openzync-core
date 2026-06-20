"""Shared LLM configuration — OpenRouter with free model.

All benchmarks use OpenRouter's ``openai/gpt-oss-120b:free`` model for
both answer generation and judging.  The ``AsyncOpenAI`` client is
configured to point at the OpenRouter API endpoint.

Override via ``.env`` file (project root) or environment variables:
    LLM_API_KEY       — OpenRouter or other API key (default: ``nokey`` for free models)
    LLM_BASE_URL      — API base URL (default: ``https://openrouter.ai/api/v1``)
    LLM_MODEL         — Model identifier (default: ``openai/gpt-oss-120b:free``)
    LLM_JUDGE_MODEL   — Separate model for judging (default: ``LLM_MODEL``)
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

# ── Load .env from project root ────────────────────────────────────────────────
_dotenv_path = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(dotenv_path=_dotenv_path)

# ── Constants from environment ─────────────────────────────────────────────────

LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
"""OpenAI-compatible API base URL (default: OpenRouter)."""

LLM_API_KEY: str = os.getenv("LLM_API_KEY") or os.getenv("OPENROUTER_API_KEY", "no-key-required")
"""API key for the LLM provider.  Falls back to OPENROUTER_API_KEY."""

LLM_MODEL: str = os.getenv("LLM_MODEL", "openai/gpt-oss-120b:free")
"""Model used for answer generation."""

LLM_JUDGE_MODEL: str = os.getenv("LLM_JUDGE_MODEL", LLM_MODEL)
"""Model used for judging (separate from answer model, defaults to same)."""

LLM_DEFAULT_TEMPERATURE: float = 0.3
"""Default temperature for answer generation."""

LLM_JUDGE_TEMPERATURE: float = 0.0
"""Judge temperature — deterministic for consistent grading."""


# ── Client factory ─────────────────────────────────────────────────────────────


def create_llm_client(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 120.0,
) -> AsyncOpenAI:
    """Create an ``AsyncOpenAI`` client configured for the LLM provider.

    Args:
        api_key: Override ``LLM_API_KEY`` env var.
        base_url: Override ``LLM_BASE_URL`` env var.
        timeout: Request timeout in seconds (default 120s for free models).

    Returns:
        A configured ``AsyncOpenAI`` instance.
    """
    return AsyncOpenAI(
        api_key=api_key or LLM_API_KEY,
        base_url=base_url or LLM_BASE_URL,
        timeout=timeout,
    )
