"""In-process enrichment gate — MiniLM should_enrich signal before the LLM call.

A fine-tuned ``BertForSequenceClassification`` (MiniLM-L6-v2) scores the
episode text as P(should_enrich) (Hub column ``should_enrich``); a score
below threshold means the episode carries no memorable content, so the
worker stamps the LLM enrichment bits via the existing mark-skipped
path and returns without the single LLM call.  High scores enrich.
Embed + link still run as their own ARQ tasks.

Fail-open contract (explicit user approval): this module NEVER RAISES.
Every failure path — missing ``transformers``/``torch``, missing
``HF_TOKEN``, Hub download failure, inference error — logs
``enrichment_gate.fail_open`` (never logging the token) and degrades to
``skip=False`` so episodes enrich normally.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL: str = "OpenZync/enrichment-gating-v1"
"""Hub repo holding the fine-tuned gating classifier."""

DEFAULT_THRESHOLD: float = 0.5
"""Minimum should_enrich score to run the LLM; below it the episode skips."""

MAX_LENGTH: int = 512
"""Truncation cap — MiniLM absolute positions stop at 512."""

# ── Module-level model cache (double-checked locking) ─────────────────────────
# Mirrors ``packages.reranker.sentence_transformers._MODEL_CACHE``: one loaded
# (tokenizer, model) pair per model name, shared across worker coroutines.

_MAX_GATE_MODELS: int = 2
"""Model-pair cap — each resident pair costs ~90MB RSS, so LRU-evict."""

_MAX_SCORE_ENTRIES: int = 2048
"""Score-cache cap — entries are ``(model, sha256(text))`` → score."""

_SCORE_TTL_S: float = 600.0
"""Score-cache TTL in seconds — scores are threshold-independent."""

_GATE_CACHE: OrderedDict[str, tuple[Any, Any]] = OrderedDict()
"""Lazy-loaded ``(tokenizer, model)`` pairs, keyed by Hub model name."""

_GATE_LOCK: asyncio.Lock = asyncio.Lock()
"""Single global lock for model loading — at most 2 cached pairs, so one
lock is simpler than a per-model dict and cannot leak lock objects."""

_SCORE_CACHE: OrderedDict[tuple[str, str], tuple[float, float]] = OrderedDict()
"""``(model, sha256(text))`` → ``(score, monotonic expiry)``; failures never cached."""

_INFER_POOL: ThreadPoolExecutor | None = None
"""Lazy singleton infer pool (max 2 concurrent infers); loads stay on default."""

_THRESHOLD_SATURATED_WARNED: bool = False
"""Once-per-process guard for the saturated-threshold (``>= 1.0``) warning."""


def _sigmoid(x: float) -> float:
    """Return the logistic sigmoid of *x*."""
    return 1.0 / (1.0 + math.exp(-x))


def _read_hf_token() -> str | None:
    """Read the Hub token at call time (never import time, never logged)."""
    return os.environ.get("HF_TOKEN") or None


def _infer_pool() -> ThreadPoolExecutor:
    """Return the lazy singleton infer pool (max 2 concurrent infers).

    Created on first use so importing this module never spawns threads;
    the stdlib joins pool threads at interpreter exit, so no ``atexit``
    handling is needed here.
    """
    global _INFER_POOL
    if _INFER_POOL is None:
        _INFER_POOL = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="enrich-gate"
        )
    return _INFER_POOL


def _store_gate_pair(model_name: str, pair: tuple[Any, Any]) -> None:
    """Insert a loaded pair into the bounded model cache with LRU eviction."""
    if model_name in _GATE_CACHE:
        _GATE_CACHE[model_name] = pair
        _GATE_CACHE.move_to_end(model_name)
        return
    while len(_GATE_CACHE) >= _MAX_GATE_MODELS:
        _GATE_CACHE.popitem(last=False)
    _GATE_CACHE[model_name] = pair


def _warn_threshold_saturated(threshold: float) -> None:
    """Warn once per process when threshold ``>= 1.0`` (everything skips).

    Sigmoid scores never reach ``1.0``, so such a threshold skips every
    scorable episode — always a misconfiguration.
    """
    global _THRESHOLD_SATURATED_WARNED
    if threshold >= 1.0 and not _THRESHOLD_SATURATED_WARNED:
        _THRESHOLD_SATURATED_WARNED = True
        logger.warning(
            "enrichment_gate.threshold_saturated",
            extra={"threshold": threshold},
        )


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
        _GATE_CACHE.move_to_end(model_name)
        return _GATE_CACHE[model_name]
    async with _GATE_LOCK:
        if model_name in _GATE_CACHE:
            _GATE_CACHE.move_to_end(model_name)
            return _GATE_CACHE[model_name]
        try:
            loop = asyncio.get_running_loop()
            pair = await loop.run_in_executor(
                None, lambda: _load_pair(model_name, _read_hf_token())
            )
        except Exception:
            logger.error("enrichment_gate.fail_open", extra={"reason": "load_failed"})
            return None
        _store_gate_pair(model_name, pair)
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
    _store_gate_pair(model, pair)
    tokenizer, clf = pair
    await loop.run_in_executor(_infer_pool(), lambda: _infer(tokenizer, clf, "ready"))


async def _score_inner(text: str, model: str = DEFAULT_MODEL) -> tuple[float, bool]:
    """Score text, returning ``(score, failed)``; never raises.

    Single path behind :func:`score` and :func:`should_skip`: blank-check →
    score-cache lookup → ensure → infer → sigmoid.  Scores are cached by
    ``(model, sha256(text))`` — they are threshold-independent, so the
    caller applies the threshold after lookup.  Failures are never cached.

    Args:
        text: Episode text to score.
        model: Hub repo id or local directory.

    Returns:
        ``(score, failed)`` — ``failed`` is True on load/infer failure
        (fail-open signal for the caller); blank text is ``(0.0, False)``
        and flows into the caller's threshold comparison.
    """
    if not text or not text.strip():
        return 0.0, False
    key = (model, hashlib.sha256(text.encode("utf-8")).hexdigest())
    now = time.monotonic()
    hit = _SCORE_CACHE.get(key)
    if hit is not None:
        cached_score, expires = hit
        if expires > now:
            _SCORE_CACHE.move_to_end(key)
            return cached_score, False
        del _SCORE_CACHE[key]
    pair = await _ensure_model(model)
    if pair is None:
        return 0.0, True
    tokenizer, clf = pair
    try:
        loop = asyncio.get_running_loop()
        logit = await loop.run_in_executor(
            _infer_pool(), lambda: _infer(tokenizer, clf, text)
        )
    except Exception:
        logger.error("enrichment_gate.fail_open", extra={"reason": "infer_failed"})
        return 0.0, True
    final = _sigmoid(logit)
    now = time.monotonic()
    # Lazy expiry only: evict at most 10 stale entries per call — a full
    # scan on every score would be O(n) per request against 2048 entries.
    evicted = 0
    for stale in [k for k, (_, exp) in _SCORE_CACHE.items() if exp <= now]:
        del _SCORE_CACHE[stale]
        evicted += 1
        if evicted >= 10:
            break
    _SCORE_CACHE[key] = (final, now + _SCORE_TTL_S)
    while len(_SCORE_CACHE) > _MAX_SCORE_ENTRIES:
        _SCORE_CACHE.popitem(last=False)
    return final, False


async def score(text: str, model: str = DEFAULT_MODEL) -> float:
    """Score episode text; sigmoid of the positive logit.

    The score is P(should_enrich): high means worth enriching, low
    means low-signal.

    Args:
        text: Episode text to score.
        model: Hub repo id (defaults to the fine-tuned gate model).

    Returns:
        The ``should_enrich`` signal in ``[0, 1]`` — ``0.0`` on any
        failure (fail-open: no signal means enrich normally) or blank
        text (nothing to extract; the caller skips at any threshold
        above zero). Never raises.
    """
    final, failed = await _score_inner(text, model)
    if failed:
        return 0.0
    return final


async def should_skip(
    text: str,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    model: str = DEFAULT_MODEL,
) -> tuple[bool, float]:
    """Decide whether the episode skips LLM enrichment.

    The gate model outputs P(should_enrich): the episode skips when the
    score falls *below* threshold (low-signal), and enriches at or
    above it.

    Args:
        text: Episode text to score.
        threshold: Minimum should_enrich score to run the LLM.
        model: Hub repo id.

    Returns:
        ``(skip, score)`` — ``skip`` is ``score < threshold``;
        ``(False, 0.0)`` on load/infer failure (fail-open must enrich).
        Blank text scores ``0.0`` and flows into the comparison (skips
        at any threshold above zero — nothing to extract). Never raises.
    """
    _warn_threshold_saturated(threshold)
    gate_score, failed = await _score_inner(text, model=model)
    if failed:
        # Fail-open must enrich: load/infer failures return explicitly
        # here and never reach the ``gate_score < threshold`` comparison
        # below, where a failure ``0.0`` would wrongly skip.
        return False, 0.0
    return gate_score < threshold, gate_score
