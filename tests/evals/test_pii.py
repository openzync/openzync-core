"""PII detection accuracy eval — golden dataset regression test.

Measures the regex-based PII detector against a curated golden dataset of 56
test cases covering emails, phone numbers, SSNs, credit cards, IP addresses,
API keys, crypto wallets, mixed PII, and clean messages (false positive check).

Gate: G3.2 — 100% of known PII patterns must be caught with zero false
positives on clean messages.

This test is fast (no LLM calls) but is still gated behind ``--run-eval``
for consistency with other eval tests.
"""

from __future__ import annotations

import logging

import pytest

from tests.evals.conftest import load_golden

logger = logging.getLogger(__name__)

ACCURACY_THRESHOLD = 1.0  # 100% required for regex-based detection


@pytest.mark.eval
def test_pii_detection_accuracy() -> None:
    """Measure PII detection accuracy against the golden dataset.

    Evaluates:
    - All expected PII types are found with at least the expected count.
    - Clean messages (``expected_no_false_positives=true``) produce zero
      detections.
    """
    from services.pii_service import PIIDetector

    dataset = load_golden("pii_test_cases.json")
    detector = PIIDetector()  # All types enabled, default min_confidence

    total = len(dataset)
    passed = 0
    errors: list[dict] = []

    logger.info(
        "eval.pii.starting",
        extra={"samples": total, "threshold": ACCURACY_THRESHOLD},
    )

    for i, item in enumerate(dataset):
        content = item["content"]
        expected = item["expected_detections"]
        is_clean = item.get("expected_no_false_positives", False)
        detections = detector.detect(content)

        case_errors: list[str] = []

        # ── Check that each expected type is found with at least the
        #    required count ────────────────────────────────────────────────
        for expected_det in expected:
            pii_type = expected_det["type"]
            expected_count = expected_det["count"]
            actual_count = sum(
                1 for d in detections if d.type == pii_type
            )
            if actual_count < expected_count:
                case_errors.append(
                    f"'{pii_type}': expected {expected_count} {'instance' if expected_count == 1 else 'instances'}, "
                    f"got {actual_count}"
                )

        # ── False positive check for clean messages ──────────────────────
        if is_clean and detections:
            false_types = sorted(set(d.type for d in detections))
            case_errors.append(
                f"false positive(s): detected {len(detections)} PII instances "
                f"types={false_types} in clean message"
            )

        if case_errors:
            errors.append(
                {
                    "index": i,
                    "id": item["id"],
                    "description": item["description"],
                    "content_preview": content[:80],
                    "errors": case_errors,
                    "detections": [
                        {"type": d.type, "value_preview": d.value[:20], "start": d.start}
                        for d in detections[:5]  # log up to 5 for debugging
                    ],
                }
            )
        else:
            passed += 1

    accuracy = passed / total if total > 0 else 0.0
    logger.info(
        "eval.pii.completed",
        extra={
            "accuracy": round(accuracy, 4),
            "correct": passed,
            "total": total,
            "errors": len(errors),
        },
    )

    # Log first 10 errors for debugging
    for err in errors[:10]:
        logger.warning(
            "eval.pii.error",
            extra={
                "id": err["id"],
                "description": err["description"],
                "errors": "; ".join(err["errors"]),
                "detection_count": len(err.get("detections", [])),
            },
        )

    assert accuracy >= ACCURACY_THRESHOLD, (
        f"PII detection accuracy {accuracy:.2%} ({passed}/{total}) "
        f"below threshold {ACCURACY_THRESHOLD:.0%}. "
        f"{len(errors)} samples had errors (see warnings for details)."
    )
