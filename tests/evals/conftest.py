"""Shared fixtures and helpers for NLP evaluation tests.

Eval tests are slow — they call the actual LLM backend.  They are skipped
by default and only run when ``--run-eval`` is passed to pytest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

GOLDEN_DIR = Path(__file__).parent / "golden"


def load_golden(filename: str) -> list[dict[str, Any]]:
    """Load a golden dataset from the ``tests/evals/golden/`` directory.

    Args:
        filename: Name of the JSON file (e.g. ``"classification.json"``).

    Returns:
        Parsed list of test case dicts.
    """
    path = GOLDEN_DIR / filename
    if not path.exists():
        raise FileNotFoundError(
            f"Golden dataset '{filename}' not found at {path}"
        )
    with open(path) as f:
        return json.load(f)


def parse_classification_response(raw: str) -> dict | None:
    """Parse an LLM classification response, handling formatting quirks.

    Strips markdown code fences, finds the first JSON object, and parses it.
    """
    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0].strip()

    raw = raw.strip()
    json_start = raw.find("{")
    if json_start < 0:
        return None
    raw = raw[json_start:]

    depth = 0
    for i, ch in enumerate(raw):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                raw = raw[: i + 1]
                break

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def evaluate_classification_match(
    predicted: dict, expected: dict
) -> tuple[bool, str]:
    """Compare predicted classification against expected labels.

    Returns ``(is_match, detail_string)``.  Exact match is required on
    ``intent``, ``emotion``, ``valence``, and ``arousal``.  ``confidence``
    and ``reasoning`` are compared with tolerance.
    """
    mismatches: list[str] = []

    for field in ("intent", "emotion", "valence", "arousal"):
        pred_val = predicted.get(field)
        exp_val = expected.get(field)
        # Treat None and "" as equivalent
        if not pred_val and not exp_val:
            continue
        if str(pred_val).strip().lower() != str(exp_val).strip().lower():
            mismatches.append(
                f"{field}: expected '{exp_val}', got '{pred_val}'"
            )

    if mismatches:
        return False, "; ".join(mismatches)

    return True, "exact match"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add ``--run-eval`` flag to pytest CLI."""
    parser.addoption(
        "--run-eval",
        action="store_true",
        default=False,
        help="Run slow NLP evaluation tests (golden dataset)",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``eval`` marker."""
    config.addinivalue_line(
        "markers",
        "eval: NLP evaluation test (slow, requires LLM backend)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip eval tests by default unless ``--run-eval`` is passed."""
    if not config.getoption("--run-eval"):
        for item in items:
            if "eval" in item.keywords:
                item.add_marker(
                    pytest.mark.skip(reason="use --run-eval to run eval tests")
                )
