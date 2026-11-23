"""In-process enrichment gate — MiniLM skip signal before the LLM call.

A fine-tuned ``BertForSequenceClassification`` (MiniLM-L6-v2) scores the
episode text; a score at or above threshold means the episode carries no
memorable content, so the worker stamps the LLM enrichment bits via the
existing mark-skipped path and returns without the single LLM call.
Embed + link still run as their own ARQ tasks.

Fail-open contract (explicit user approval): this module NEVER RAISES.
Every failure path — missing ``transformers``/``torch``, missing
``HF_TOKEN``, Hub download failure, inference error — logs
``enrichment_gate.fail_open`` (never logging the token) and degrades to
``skip=False`` so episodes enrich normally.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL: str = "OpenZync/enrichment-gating-v1"
"""Hub repo holding the fine-tuned gating classifier."""

DEFAULT_THRESHOLD: float = 0.5
"""Sigmoid score at or above which the episode skips LLM enrichment."""

MAX_LENGTH: int = 512
"""Truncation cap — MiniLM absolute positions stop at 512."""

# ── Module-level model cache (double-checked locking) ─────────────────────────
# Mirrors ``packages.reranker.sentence_transformers._MODEL_CACHE``: one loaded
# (tokenizer, model) pair per model name, shared across worker coroutines.

_GATE_CACHE: dict[str, tuple[Any, Any]] = {}
"""Lazy-loaded ``(tokenizer, model)`` pairs, keyed by Hub model name."""

_GATE_LOCKS: dict[str, asyncio.Lock] = {}
"""Per-model-name asyncio locks to prevent concurrent model loading."""


def _sigmoid(x: float) -> float:
    """Return the logistic sigmoid of *x*."""
    return 1.0 / (1.0 + math.exp(-x))


def _read_hf_token() -> str | None:
    """Read the Hub token at call time (never import time, never logged)."""
    return os.environ.get("HF_TOKEN") or None


def _load_pair(model_name: str, token: str | None) -> tuple[Any, Any]:
    """Synchronously load tokenizer + classification model onto CPU.

    Args:
        model_name: Hub repo id.
        token: Hub auth token (``None`` for public repos).

    Returns:
        The ``(tokenizer, model)`` pair in eval mode on CPU.
    """
    from transformers import (  # noqa: PLC0415 — optional, lazy like the reranker
        AutoModelForSequenceClassification,
        AutoTokenizer,
    )

    kwargs: dict[str, Any] = {"trust_remote_code": False}
    if token:
        kwargs["token"] = token
    tokenizer = AutoTokenizer.from_pretrained(model_name, **kwargs)
    model = AutoModelForSequenceClassification.from_pretrained(model_name, **kwargs)
    model.eval()
    model.cpu()
    return tokenizer, model


def _infer(tokenizer: Any, model: Any, text: str) -> float:
    """Synchronously run the classifier; returns the positive logit.

    Args:
        tokenizer: The loaded tokenizer.
        model: The loaded sequence-classification model (eval, CPU).
        text: Episode text to score.

    Returns:
        The positive-class logit (index 1; index 0 when single-logit head).
    """
    import torch  # noqa: PLC0415 — optional, lazy like the reranker

    max_len = tokenizer.model_max_length or MAX_LENGTH
    if not isinstance(max_len, int) or max_len <= 0 or max_len > MAX_LENGTH:
        max_len = MAX_LENGTH
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=max_len)
    with torch.no_grad():
        logits = model(**inputs).logits[0]
    positive = logits[1] if logits.numel() > 1 else logits[0]
    return float(positive.item())


async def _ensure_model(model_name: str) -> tuple[Any, Any] | None:
    """Load the gate pair with double-checked locking; ``None`` when unusable.

    Args:
        model_name: Hub repo id.

    Returns:
        The cached ``(tokenizer, model)`` pair, or ``None`` on any failure
        (fail-open — the caller degrades to ``skip=False``).
    """
    if model_name in _GATE_CACHE:
        return _GATE_CACHE[model_name]
    _GATE_LOCKS.setdefault(model_name, asyncio.Lock())
    async with _GATE_LOCKS[model_name]:
        if model_name in _GATE_CACHE:
            return _GATE_CACHE[model_name]
        try:
            loop = asyncio.get_running_loop()
            pair = await loop.run_in_executor(
                None, lambda: _load_pair(model_name, _read_hf_token())
            )
        except Exception:
            logger.error("enrichment_gate.fail_open", extra={"reason": "load_failed"})
            return None
        _GATE_CACHE[model_name] = pair
        return pair


async def prewarm(model: str = DEFAULT_MODEL) -> None:
    """Load the gate pair and run one warm inference; raise on any failure.

    Worker boot only — the opposite of :func:`score`'s fail-open contract.
    ``score`` never raises by design, so boot cannot use it to prove the
    model works; this calls :func:`_load_pair` / :func:`_infer` directly so
    an unknown Hub id or a broken download aborts startup loudly.  The
    loaded pair is cached for later :func:`score` calls.

    Args:
        model: Hub repo id or local directory (both work — ``from_pretrained``
            accepts either, so local-dir paths keep working).

    Raises:
        ValueError: If *model* is blank.
        Exception: Whatever the load or warm inference raises, unmodified.
    """
    if not model or not model.strip():
        raise ValueError("Enrichment gate 'model' must be a non-empty string.")
    loop = asyncio.get_running_loop()
    pair = await loop.run_in_executor(
        None, lambda: _load_pair(model, _read_hf_token())
    )
    _GATE_CACHE[model] = pair
    tokenizer, clf = pair
    await loop.run_in_executor(None, lambda: _infer(tokenizer, clf, "ready"))


async def score(text: str, model: str = DEFAULT_MODEL) -> float:
    """Score episode text; sigmoid of the positive logit.

    Args:
        text: Episode text to score.
        model: Hub repo id (defaults to the fine-tuned gate model).

    Returns:
        The ``skip`` signal in ``[0, 1]`` — ``0.0`` on any failure or
        blank text (fail-open: no signal means enrich normally). Never raises.
    """
    if not text or not text.strip():
        return 0.0
    pair = await _ensure_model(model)
    if pair is None:
        return 0.0
    tokenizer, clf = pair
    try:
        loop = asyncio.get_running_loop()
        logit = await loop.run_in_executor(None, lambda: _infer(tokenizer, clf, text))
    except Exception:
        logger.error("enrichment_gate.fail_open", extra={"reason": "infer_failed"})
        return 0.0
    return _sigmoid(logit)


async def should_skip(
    text: str,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    model: str = DEFAULT_MODEL,
) -> tuple[bool, float]:
    """Decide whether the episode skips LLM enrichment.

    Args:
        text: Episode text to score.
        threshold: Score at or above which the episode is skipped.
        model: Hub repo id.

    Returns:
        ``(skip, score)`` — ``skip`` is ``score >= threshold``;
        ``(False, 0.0)`` on any failure (fail-open). Never raises.
    """
    gate_score = await score(text, model)
    return gate_score >= threshold, gate_score
