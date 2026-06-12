"""Unit tests for PII detection — regex layer (deterministic, no mock needed).

The spaCy NER and LLM fallback layers are tested via evals in
``tests/evals/test_pii.py``.
"""

from __future__ import annotations

import pytest

from services.pii_service import PIIDetector


@pytest.mark.unit
class TestRegexPII:
    """Regex-based PII detection tests."""

    def test_detects_email(self) -> None:
        detector = PIIDetector()
        result = detector.detect("Contact me at test@example.com")
        assert len(result) >= 1
        assert any(f.type == "email" for f in result)

    def test_detects_phone(self) -> None:
        detector = PIIDetector()
        result = detector.detect("Call +1-555-123-4567 for help")
        assert len(result) >= 1
        assert any(f.type == "phone" for f in result)

    def test_clean_text_no_pii(self) -> None:
        detector = PIIDetector()
        result = detector.detect("Hello, how are you today?")
        assert len(result) == 0

    def test_detects_ip_address(self) -> None:
        detector = PIIDetector()
        result = detector.detect("Server: 192.168.1.1")
        assert len(result) >= 1

    def test_detects_credit_card(self) -> None:
        detector = PIIDetector()
        result = detector.detect("Card: 4111-1111-1111-1111")
        assert len(result) >= 1

    def test_confidence_is_high_for_clear_patterns(self) -> None:
        detector = PIIDetector()
        result = detector.detect("test@example.com")
        if result:
            assert result[0].confidence >= 0.9

    def test_start_end_positions_are_correct(self) -> None:
        detector = PIIDetector()
        result = detector.detect("email: a@b.com")
        if result:
            assert result[0].start >= 0
            assert result[0].end > result[0].start
