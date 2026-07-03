"""Utility functions for the LongMemEval benchmark suite.

Provides dataset download, loading, and evaluation metrics for the
LongMemEval benchmark from HuggingFace (``xiaowu0162/longmemeval-cleaned``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.request import (
    urlretrieve,  # nosec: S310 — downloads from trusted HuggingFace
)

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

QUESTION_TYPE_CATEGORIES: dict[str, str] = {
    "single-session-user": "single-session-user",
    "single-session-assistant": "single-session-assistant",
    "single-session-preference": "single-session-preference",
    "multi-session": "multi-session",
    "temporal-reasoning": "temporal-reasoning",
    "knowledge-update": "knowledge-update",
}
"""Maps raw dataset question types to canonical category names.

Each of the six types maps to itself.  Unrecognised types will be classified
as ``"other"`` at query time.
"""

DATASET_FILES: dict[str, str] = {
    "s": "longmemeval_s_cleaned.json",
    "oracle": "longmemeval_oracle.json",
}
"""Available dataset variants and their remote filenames."""

_HF_BASE_URL: str = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/"
)
"""Base URL for the HuggingFace dataset repository."""


# ── Path helpers ─────────────────────────────────────────────────────────────


def get_dataset_dir() -> Path:
    """Return the absolute path to the local LongMemEval data directory.

    The directory is ``tests/benchmarks/data/longmemeval/`` resolved relative
    to this module's location on disk.

    Returns:
        Absolute path to the dataset cache directory.
    """
    return Path(__file__).resolve().parent / "data" / "longmemeval"


# ── Download helpers ─────────────────────────────────────────────────────────


def _progress_hook(block_count: int, block_size: int, total_size: int) -> None:
    """Log download progress at INFO level every 10%.

    Args:
        block_count: Number of blocks transferred so far.
        block_size: Size of each block in bytes.
        total_size: Total file size in bytes (may be 0 if unknown).
    """
    if total_size <= 0:
        return
    downloaded = block_count * block_size
    percent = min(int(downloaded / total_size * 100), 100)
    if percent % 10 == 0:
        logger.info(
            "LongMemEval download: %d%% (%d / %d bytes)",
            percent,
            downloaded,
            total_size,
        )


def _is_valid_json(path: Path) -> bool:
    """Check whether a file at *path* contains valid JSON.

    Tries to parse the file as JSON and returns ``True`` if it succeeds
    (handles large files by only parsing the first and last few KB).

    Args:
        path: Path to the file to check.

    Returns:
        ``True`` if the file contains valid, complete JSON.
    """
    try:
        with open(path) as f:
            json.load(f)
        return True
    except (json.JSONDecodeError, ValueError, OSError):
        return False


def download_dataset(variant: str) -> Path:
    """Download a LongMemEval dataset file from HuggingFace if not already cached.

    Validates the integrity of cached files by parsing them as JSON.  If a
    cached file is corrupted (e.g. truncated download), it is deleted and
    re-downloaded automatically.

    Args:
        variant: Dataset variant key — one of ``DATASET_FILES.keys()``.

    Returns:
        Path to the local cached file (guaranteed valid JSON).

    Raises:
        ValueError: If ``variant`` is not a recognised dataset key.
        RuntimeError: If the download fails after a retry.
    """
    if variant not in DATASET_FILES:
        raise ValueError(
            f"Unknown dataset variant '{variant}'. "
            f"Available variants: {list(DATASET_FILES)}"
        )

    dest_dir = get_dataset_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)

    filename = DATASET_FILES[variant]
    dest_path = dest_dir / filename

    # If cached file exists and is valid JSON, use it
    if dest_path.exists():
        if _is_valid_json(dest_path):
            logger.info(
                "LongMemEval dataset '%s' already cached at %s",
                variant,
                dest_path,
            )
            return dest_path
        logger.warning(
            "Cached file %s is corrupted — deleting and re-downloading",
            dest_path,
        )
        dest_path.unlink()

    # Download with retry
    url = _HF_BASE_URL + filename
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        logger.info(
            "Downloading LongMemEval '%s' from %s (attempt %d/%d) ...",
            variant,
            url,
            attempt,
            max_attempts,
        )
        # Remove partial download from a previous failed attempt
        if dest_path.exists():
            dest_path.unlink()

        try:
            urlretrieve(url, dest_path, reporthook=_progress_hook)  # noqa: S310
        except Exception as exc:
            logger.error(
                "Download attempt %d/%d failed: %s",
                attempt,
                max_attempts,
                exc,
            )
            if attempt == max_attempts:
                raise RuntimeError(
                    f"Failed to download {filename} after {max_attempts} attempts"
                ) from exc
            continue

        # Verify integrity
        if _is_valid_json(dest_path):
            logger.info("Downloaded LongMemEval '%s' to %s", variant, dest_path)
            return dest_path

        logger.warning(
            "Downloaded file is corrupted (truncated JSON) — retrying",
        )

    raise RuntimeError(
        f"Failed to download valid {filename} after {max_attempts} attempts"
    )


def load_dataset(variant: str) -> list[dict[str, Any]]:
    """Download (if needed) and load a LongMemEval dataset variant.

    Validates the cached file's integrity before loading.  Corrupted cache
    files are automatically deleted and re-downloaded.

    Args:
        variant: Dataset variant key — one of ``DATASET_FILES.keys()``.

    Returns:
        List of question entry dicts.

    Raises:
        RuntimeError: If the dataset cannot be downloaded or parsed.
    """
    path = download_dataset(variant)
    with open(path) as f:
        data: list[dict[str, Any]] = json.load(f)
    logger.info("Loaded LongMemEval '%s': %d questions", variant, len(data))
    return data


# ── Question type helpers ────────────────────────────────────────────────────


def get_question_type_category(question_type: str) -> str:
    """Map a raw question type string to a canonical category.

    Args:
        question_type: The raw question type label from the dataset.

    Returns:
        Canonical category name, or ``"other"`` if unrecognised.
    """
    return QUESTION_TYPE_CATEGORIES.get(question_type, "other")


def is_abstention(question_id: str) -> bool:
    """Check whether a question ID indicates an abstention variant.

    Abstention question IDs end with the suffix ``_abs``.

    Args:
        question_id: The question identifier string.

    Returns:
        ``True`` if the ID ends with ``_abs``.
    """
    return question_id.endswith("_abs")


# ── Evaluation metrics ───────────────────────────────────────────────────────


def compute_recall_at_k(
    search_results: list[dict[str, Any]], ground_truth: str, k: int
) -> bool:
    """Check whether key terms from the ground truth appear in top-*k* results.

    Extracts individual words from ``ground_truth``, filters out tokens of
    length 2 or fewer (stop words and punctuation fragments), and checks
    whether every remaining key term appears as a case-insensitive substring
    within the ``content`` field of any top-*k* result.

    Args:
        search_results: Ranked list of result dicts, each containing a
            ``content`` key.
        ground_truth: The expected answer text.
        k: Number of top results to consider.

    Returns:
        ``True`` if every key term is located in at least one of the top-*k*
        results.
    """
    top_k = search_results[:k]
    if not top_k:
        return False

    key_terms = {
        word.lower()
        for word in ground_truth.split()
        if len(word) > 2
    }
    if not key_terms:
        return False

    combined_content = " ".join(
        r.get("content", "") for r in top_k
    ).lower()

    return all(term in combined_content for term in key_terms)


def compute_accuracy(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate correctness results per question category.

    Args:
        results: List of result dicts.  Each must contain ``question_type``
            (``str``) and ``correct`` (``bool``) keys.

    Returns:
        A dict with:

        - ``overall_accuracy``: float — total correct divided by total.
        - ``per_category``: dict mapping each category name to
          ``{"correct": int, "total": int, "accuracy": float}``.
    """
    per_category: dict[str, dict[str, int]] = {}
    total_correct = 0
    total_count = len(results)

    for r in results:
        qtype = get_question_type_category(r.get("question_type", ""))
        correct = bool(r.get("correct", False))

        if qtype not in per_category:
            per_category[qtype] = {"correct": 0, "total": 0}

        per_category[qtype]["total"] += 1
        per_category[qtype]["correct"] += 1 if correct else 0
        total_correct += 1 if correct else 0

    return {
        "overall_accuracy": (
            round(total_correct / total_count, 4) if total_count > 0 else 0.0
        ),
        "per_category": {
            cat: {
                "correct": stats["correct"],
                "total": stats["total"],
                "accuracy": round(stats["correct"] / stats["total"], 4)
                if stats["total"] > 0
                else 0.0,
            }
            for cat, stats in sorted(per_category.items())
        },
    }
