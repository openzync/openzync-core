"""Unit tests for PII detection — regex layer (deterministic, no mock needed).

The spaCy NER and LLM fallback layers are tested via evals in
``tests/evals/test_pii.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from core.exceptions import ExternalServiceError, ValidationError
from services.pii_service import PIIDetection, PIIDetector, PIIRedactor, PIIService


@pytest.mark.unit
class TestRegexPII:
    """Regex-based PII detection tests."""

    @staticmethod
    def _make_detector() -> PIIDetector:
        """Create a PIIDetector with NER disabled — only regex is tested here."""
        return PIIDetector(use_ner=False)

    def test_detects_email(self) -> None:
        detector = self._make_detector()
        result = detector.detect("Contact me at test@example.com")
        assert len(result) >= 1
        assert any(f.type == "email" for f in result)

    def test_detects_phone(self) -> None:
        detector = self._make_detector()
        result = detector.detect("Call +1-555-123-4567 for help")
        assert len(result) >= 1
        assert any(f.type == "phone" for f in result)

    def test_clean_text_no_pii(self) -> None:
        detector = self._make_detector()
        result = detector.detect("Hello, how are you today?")
        assert len(result) == 0

    def test_detects_ip_address(self) -> None:
        detector = self._make_detector()
        result = detector.detect("Server: 192.168.1.1")
        assert len(result) >= 1

    def test_detects_credit_card(self) -> None:
        detector = self._make_detector()
        result = detector.detect("Card: 4111-1111-1111-1111")
        assert len(result) >= 1

    def test_confidence_is_high_for_clear_patterns(self) -> None:
        detector = self._make_detector()
        result = detector.detect("test@example.com")
        if result:
            assert result[0].confidence >= 0.9

    def test_start_end_positions_are_correct(self) -> None:
        detector = self._make_detector()
        result = detector.detect("email: a@b.com")
        if result:
            assert result[0].start >= 0
            assert result[0].end > result[0].start

    # ── New regex tests (appended) ─────────────────────────────────────────

    def test_detects_ssn(self) -> None:
        """SSN pattern '123-45-6789' is detected."""
        detector = self._make_detector()
        result = detector.detect("My SSN is 123-45-6789")
        assert len(result) >= 1
        assert any(f.type == "ssn" for f in result)

    def test_detects_api_key_openai(self) -> None:
        """OpenAI API key (sk- prefix) is detected."""
        detector = self._make_detector()
        result = detector.detect("Key: sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        assert len(result) >= 1
        assert any(f.type == "api_key" for f in result)

    def test_detects_crypto_wallet(self) -> None:
        """Ethereum address (0x-prefixed) is detected."""
        detector = self._make_detector()
        result = detector.detect(
            "ETH: 0x0123456789012345678901234567890123456789"
        )
        assert len(result) >= 1
        assert any(f.type == "crypto_wallet" for f in result)

    def test_no_false_positives_on_normal_text(self) -> None:
        """Normal text with years and plain words has no detections."""
        detector = self._make_detector()
        result = detector.detect("The project was completed in 2024.")
        assert len(result) == 0


@pytest.mark.unit
class TestPIIRedactor:
    """PIIRedactor — redaction mode and edge-case tests."""

    def test_redactor_mask_mode_replaces_email(self) -> None:
        """Mask mode replaces an email span with [REDACTED:EMAIL]."""
        redactor = PIIRedactor(mode="mask")
        detections = [
            PIIDetection(
                type="email",
                value="test@example.com",
                start=8,
                end=24,
                confidence=0.95,
                method="regex",
            ),
        ]
        result = redactor.apply("Contact test@example.com", detections)
        assert result == "Contact [REDACTED:EMAIL]"

    def test_redactor_mask_mode_multiple_detections(self) -> None:
        """Multiple PII spans of different types are all redacted."""
        redactor = PIIRedactor(mode="mask")
        detections = [
            PIIDetection(
                type="email",
                value="test@example.com",
                start=7,
                end=23,
                confidence=0.95,
                method="regex",
            ),
            PIIDetection(
                type="phone",
                value="555-123-4567",
                start=32,
                end=44,
                confidence=0.95,
                method="regex",
            ),
        ]
        text = "Email: test@example.com, Phone: 555-123-4567"
        result = redactor.apply(text, detections)
        assert "[REDACTED:EMAIL]" in result
        assert "[REDACTED:PHONE]" in result
        assert "test@example.com" not in result
        assert "555-123-4567" not in result

    def test_redactor_no_detections_returns_original(self) -> None:
        """Empty detections list returns the original text unchanged."""
        redactor = PIIRedactor(mode="mask")
        text = "Hello, this is a clean message."
        result = redactor.apply(text, [])
        assert result is text  # Same object returned

    def test_redactor_block_mode_raises_error(self) -> None:
        """Block mode raises ValueError when apply() is called."""
        redactor = PIIRedactor(mode="block")
        detections = [
            PIIDetection(
                type="email",
                value="test@example.com",
                start=0,
                end=16,
                confidence=0.95,
                method="regex",
            ),
        ]
        with pytest.raises(ValueError, match="PIIRedactor cannot apply"):
            redactor.apply("test@example.com", detections)

    def test_redactor_invalid_mode_raises_value_error(self) -> None:
        """Constructing PIIRedactor with an invalid mode raises ValueError."""
        with pytest.raises(ValueError, match="Invalid redaction mode"):
            PIIRedactor(mode="invalid")

    def test_redactor_processes_reverse_order(self) -> None:
        """Detections are processed right-to-left so earlier offsets stay valid."""
        redactor = PIIRedactor(mode="mask")
        text = "a@b.com 555-123-4567"
        detections = [
            PIIDetection(
                type="email",
                value="a@b.com",
                start=0,
                end=7,
                confidence=0.95,
                method="regex",
            ),
            PIIDetection(
                type="phone",
                value="555-123-4567",
                start=8,
                end=20,
                confidence=0.95,
                method="regex",
            ),
        ]
        result = redactor.apply(text, detections)
        assert result == "[REDACTED:EMAIL] [REDACTED:PHONE]"


@pytest.mark.unit
class TestMergeOverlapping:
    """PIIDetector._merge_overlapping — overlapping resolution logic."""

    def test_merge_overlapping_empty_list(self) -> None:
        """Empty detection list returns an empty list."""
        assert PIIDetector._merge_overlapping([]) == []

    def test_merge_overlapping_prefers_longer_span(self) -> None:
        """When detections overlap, the longer span is kept."""
        detections = [
            PIIDetection(
                type="email",
                value="test@example.com",
                start=0,
                end=16,
                confidence=0.9,
                method="regex",
            ),
            PIIDetection(
                type="name",
                value="test@exampl",
                start=0,
                end=11,
                confidence=0.85,
                method="spacy_ner",
            ),
        ]
        result = PIIDetector._merge_overlapping(detections)
        assert len(result) == 1
        assert result[0].type == "email"  # Longer span wins

    def test_merge_overlapping_identical_spans_higher_confidence_wins(self) -> None:
        """Same span — higher confidence detection wins."""
        detections = [
            PIIDetection(
                type="regex",
                value="test@example.com",
                start=0,
                end=16,
                confidence=0.8,
                method="regex",
            ),
            PIIDetection(
                type="ner",
                value="test@example.com",
                start=0,
                end=16,
                confidence=0.9,
                method="spacy_ner",
            ),
        ]
        result = PIIDetector._merge_overlapping(detections)
        assert len(result) == 1
        assert result[0].type == "ner"  # Higher confidence wins
        assert result[0].confidence == 0.9

    def test_merge_overlapping_no_overlap(self) -> None:
        """Non-overlapping detections are all kept."""
        detections = [
            PIIDetection(
                type="email",
                value="a@b.com",
                start=0,
                end=7,
                confidence=0.95,
                method="regex",
            ),
            PIIDetection(
                type="phone",
                value="555-1234",
                start=10,
                end=18,
                confidence=0.95,
                method="regex",
            ),
        ]
        result = PIIDetector._merge_overlapping(detections)
        assert len(result) == 2


@pytest.mark.unit
class TestPIIService:
    """PIIService — config parsing, mode dispatch, and edge cases."""

    def test_mode_property(self) -> None:
        """mode property reflects the config value."""
        assert PIIService({"mode": "mask"}).mode == "mask"
        assert PIIService({"mode": "off"}).mode == "off"

    def test_process_message_mode_off(self) -> None:
        """mode='off' returns content unchanged with empty detections."""
        service = PIIService({"mode": "off"})
        result, detections, blocked = asyncio.run(
            service.process_message("My email is test@example.com")
        )
        assert result == "My email is test@example.com"
        assert detections == []
        assert blocked is False

    def test_process_message_mode_mask(self) -> None:
        """mode='mask' redacts PII and returns detections."""
        service = PIIService(
            {"mode": "mask", "sensitivity": "low"}
        )
        result, detections, blocked = asyncio.run(
            service.process_message("My email is test@example.com")
        )
        assert "[REDACTED:EMAIL]" in result
        assert "test@example.com" not in result
        assert len(detections) >= 1
        assert not blocked

    def test_process_message_mode_block_raises(self) -> None:
        """mode='block' with PII raises ValidationError."""
        service = PIIService(
            {"mode": "block", "sensitivity": "low"}
        )
        with pytest.raises(ValidationError, match="PII"):
            asyncio.run(
                service.process_message("My email is test@example.com")
            )

    def test_process_message_block_mode_no_pii_passes(self) -> None:
        """mode='block' with no PII passes through unchanged."""
        service = PIIService(
            {"mode": "block", "sensitivity": "low"}
        )
        result, detections, blocked = asyncio.run(
            service.process_message("Hello, how are you?")
        )
        assert result == "Hello, how are you?"
        assert len(detections) == 0
        assert not blocked

    def test_process_message_empty_config_defaults_to_off(self) -> None:
        """Empty config defaults to mode='off'."""
        service = PIIService({})
        assert service.mode == "off"
        result, detections, blocked = asyncio.run(
            service.process_message("test@example.com")
        )
        assert result == "test@example.com"
        assert detections == []

    def test_process_message_handles_none_config(self) -> None:
        """None config defaults to mode='off'."""
        service = PIIService(None)
        assert service.mode == "off"
        result, detections, blocked = asyncio.run(
            service.process_message("test@example.com")
        )
        assert result == "test@example.com"
        assert detections == []

    def test_constructor_with_custom_types(self) -> None:
        """Custom enabled_types and sensitivity are accepted."""
        config = {
            "mode": "mask",
            "enabled_types": ["email", "phone"],
            "min_confidence": 0.5,
            "sensitivity": "high",
        }
        service = PIIService(config)
        assert service.mode == "mask"


@pytest.mark.unit
class TestNERPII:
    """NER-based PII detection — all spaCy calls are mocked.

    These tests patch ``PIIDetector._get_nlp`` to return a mock spaCy
    pipeline, avoiding the need for the ``en_core_web_sm`` model or even
    the spaCy package itself to be installed in the test environment.
    """

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _make_mock_nlp(entities: list[dict]) -> MagicMock:
        """Build a mock spaCy ``Language`` pipeline that yields *entities*.

        Each entity dict must have keys ``label_``, ``text``, ``start_char``,
        and ``end_char``.
        """
        mock_doc = MagicMock()
        mock_doc.ents = []
        for ent_data in entities:
            ent = MagicMock()
            ent.label_ = ent_data["label_"]
            ent.text = ent_data["text"]
            ent.start_char = ent_data["start_char"]
            ent.end_char = ent_data["end_char"]
            mock_doc.ents = [*mock_doc.ents, ent]

        mock_nlp = MagicMock()
        mock_nlp.return_value = mock_doc  # nlp(text) → mock_doc
        return mock_nlp

    # ── Detection tests ────────────────────────────────────────────────────

    def test_detects_name_with_ner(self) -> None:
        """NER detects PERSON entity as 'name' type."""
        mock_nlp = self._make_mock_nlp([
            {
                "label_": "PERSON",
                "text": "John Doe",
                "start_char": 11,
                "end_char": 19,
            },
        ])
        with patch.object(PIIDetector, "_get_nlp", return_value=mock_nlp):
            detector = PIIDetector(
                use_ner=True,
                min_confidence=0.0,
                enabled_types=["name"],
            )
            results = detector.detect("My name is John Doe")

        assert len(results) >= 1
        assert any(r.type == "name" for r in results)
        assert any(r.value == "John Doe" for r in results)

    def test_detects_organization_with_ner(self) -> None:
        """NER detects ORG entity as 'organization' type."""
        mock_nlp = self._make_mock_nlp([
            {
                "label_": "ORG",
                "text": "Acme Corp",
                "start_char": 10,
                "end_char": 19,
            },
        ])
        with patch.object(PIIDetector, "_get_nlp", return_value=mock_nlp):
            detector = PIIDetector(
                use_ner=True,
                min_confidence=0.0,
                enabled_types=["organization"],
            )
            results = detector.detect("I work at Acme Corp")

        assert len(results) >= 1
        assert any(r.type == "organization" for r in results)

    def test_detects_location_with_ner(self) -> None:
        """NER detects GPE entity as 'address' type."""
        mock_nlp = self._make_mock_nlp([
            {
                "label_": "GPE",
                "text": "Paris",
                "start_char": 14,
                "end_char": 19,
            },
        ])
        with patch.object(PIIDetector, "_get_nlp", return_value=mock_nlp):
            detector = PIIDetector(
                use_ner=True,
                min_confidence=0.0,
                enabled_types=["address"],
            )
            results = detector.detect("She lives in Paris")

        assert len(results) >= 1
        assert any(r.type == "address" for r in results)

    def test_detects_date_with_ner(self) -> None:
        """NER detects DATE entity as 'date' type."""
        mock_nlp = self._make_mock_nlp([
            {
                "label_": "DATE",
                "text": "next Monday",
                "start_char": 17,
                "end_char": 28,
            },
        ])
        with patch.object(PIIDetector, "_get_nlp", return_value=mock_nlp):
            detector = PIIDetector(
                use_ner=True,
                min_confidence=0.0,
                enabled_types=["date"],
            )
            results = detector.detect("The meeting is next Monday")

        assert len(results) >= 1
        assert any(r.type == "date" for r in results)

    # ── Filter / skip tests ───────────────────────────────────────────────

    def test_ner_skips_unmapped_labels(self) -> None:
        """Labels not in NER_LABEL_MAP (e.g. 'LAW') are skipped."""
        mock_nlp = self._make_mock_nlp([
            {
                "label_": "LAW",  # Not in NER_LABEL_MAP → skipped
                "text": "Some Law",
                "start_char": 0,
                "end_char": 9,
            },
        ])
        with patch.object(PIIDetector, "_get_nlp", return_value=mock_nlp):
            detector = PIIDetector(
                use_ner=True,
                min_confidence=0.0,
                enabled_types=["email", "phone", "name", "address",
                               "organization", "date"],
            )
            results = detector.detect("Some Law reference")

        assert len(results) == 0

    def test_ner_skips_disabled_types(self) -> None:
        """Entity types not in enabled_types are skipped."""
        mock_nlp = self._make_mock_nlp([
            {
                "label_": "PERSON",  # Maps to 'name'
                "text": "Jane Doe",
                "start_char": 0,
                "end_char": 8,
            },
        ])
        with patch.object(PIIDetector, "_get_nlp", return_value=mock_nlp):
            # Only "email" is enabled — PERSON → "name" is filtered out
            detector = PIIDetector(
                use_ner=True,
                min_confidence=0.0,
                enabled_types=["email"],
            )
            results = detector.detect("Jane Doe")

        assert len(results) == 0

    # ── Error-path tests ──────────────────────────────────────────────────

    def test_ner_spacy_not_installed_raises(self) -> None:
        """ExternalServiceError when spaCy is not installed.

        We patch ``_get_nlp`` to raise ExternalServiceError directly, matching
        what the real ``_get_nlp`` does when ``import spacy`` fails.
        """
        def _raise_import_error() -> MagicMock:
            raise ExternalServiceError(
                "PII NER model (spaCy) is not installed. "
                "PII detection requires NER to be available."
            )

        with patch.object(
            PIIDetector, "_get_nlp", side_effect=_raise_import_error,
        ):
            detector = PIIDetector(use_ner=True)
            detector._nlp = None  # Force re-load attempt

            with pytest.raises(ExternalServiceError, match="spaCy"):
                detector.detect("Hello world")

    def test_ner_model_load_failed_raises(self) -> None:
        """ExternalServiceError when NER model fails to load."""
        def _raise_os_error() -> MagicMock:
            raise ExternalServiceError(
                "PII NER model (en_core_web_sm) failed to load. "
                "PII detection requires NER to be available."
            )

        with patch.object(
            PIIDetector, "_get_nlp", side_effect=_raise_os_error,
        ):
            detector = PIIDetector(use_ner=True)
            detector._nlp = None  # Force re-load attempt

            with pytest.raises(ExternalServiceError, match="NER model"):
                detector.detect("Hello world")
