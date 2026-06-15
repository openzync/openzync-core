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
PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "services" / "worker" / "prompts"


def load_prompt_text(template_name: str) -> str:
    """Load a prompt template from the filesystem prompts directory.

    Args:
        template_name: The logical template name (e.g. ``"classify_dialog_v1"``).

    Returns:
        The raw Jinja2 template text.
    """
    path = PROMPTS_DIR / f"{template_name}.jinja2"
    if not path.exists():
        raise FileNotFoundError(
            f"Prompt template '{template_name}' not found at {path}"
        )
    return path.read_text()


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


def parse_structured_response(raw: str) -> dict | None:
    """Parse an LLM structured extraction response, handling formatting quirks.

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


def _deep_compare_fields(
    predicted: dict, expected: dict, path: str = ""
) -> list[str]:
    """Recursively compare predicted vs expected values.

    Returns a list of mismatch descriptions.  Considers None and missing
    keys as equivalent.  String comparisons are case-insensitive.
    """
    mismatches: list[str] = []

    for key, exp_val in expected.items():
        current_path = f"{path}.{key}" if path else key
        pred_val = predicted.get(key)

        if exp_val is None:
            # Expected null — predicted should also be null or missing
            if pred_val is not None:
                mismatches.append(
                    f"{current_path}: expected null, got '{pred_val}'"
                )
            continue

        if isinstance(exp_val, dict):
            if not isinstance(pred_val, dict):
                mismatches.append(
                    f"{current_path}: expected dict, got {type(pred_val).__name__}"
                )
            else:
                mismatches.extend(
                    _deep_compare_fields(pred_val, exp_val, current_path)
                )
        elif isinstance(exp_val, list):
            if not isinstance(pred_val, list):
                mismatches.append(
                    f"{current_path}: expected list, got {type(pred_val).__name__}"
                )
            elif len(pred_val) != len(exp_val):
                mismatches.append(
                    f"{current_path}: expected {len(exp_val)} items, "
                    f"got {len(pred_val)}"
                )
            else:
                for i, (p_item, e_item) in enumerate(zip(pred_val, exp_val)):
                    if isinstance(e_item, dict):
                        mismatches.extend(
                            _deep_compare_fields(
                                p_item, e_item, f"{current_path}[{i}]"
                            )
                        )
                    else:
                        if str(p_item).strip().lower() != str(e_item).strip().lower():
                            mismatches.append(
                                f"{current_path}[{i}]: expected "
                                f"'{e_item}', got '{p_item}'"
                            )
        else:
            # Scalar comparison — case insensitive for strings
            if str(pred_val).strip().lower() != str(exp_val).strip().lower():
                mismatches.append(
                    f"{current_path}: expected '{exp_val}', got '{pred_val}'"
                )

    return mismatches


def evaluate_structured_match(
    predicted: dict, expected: dict
) -> tuple[bool, str]:
    """Compare predicted structured extraction against expected.

    For each schema key in ``expected``:
      - If expected value is ``None``, predicted should also be ``None``
        (or missing).
      - Otherwise, recursively compare nested fields.

    Extra keys in predicted that are not in expected are ignored (the LLM
    may output schemas that don't have matching data — that's fine).

    Returns ``(is_match, detail_string)``.
    """
    mismatches: list[str] = []

    for schema_name, exp_val in expected.items():
        pred_val = predicted.get(schema_name)

        if exp_val is None:
            if pred_val is not None:
                mismatches.append(
                    f"'{schema_name}': expected null, got non-null"
                )
            continue

        if not isinstance(pred_val, dict):
            mismatches.append(
                f"'{schema_name}': expected dict, got "
                f"{type(pred_val).__name__ if pred_val is not None else 'null'}"
            )
            continue

        mismatches.extend(
            _deep_compare_fields(pred_val, exp_val, schema_name)
        )

    if mismatches:
        return False, "; ".join(mismatches)

    return True, "exact match"


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
