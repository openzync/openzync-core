# PII Detection & Redaction

> **Phase:** 3 (NLP Enrichment) — P1 requirement
> **SRS References:** SEC-09 (input validation), SEC-07 (no secrets in logs), OQ-03 (LLM cost at scale — redact before hitting LLM)
> **Index:** 5.6 — Read before any NLP pipeline task; runs as a pre-processing step

---

## 1. Overview

PII (Personally Identifiable Information) detection runs **before** any LLM call in the NLP pipeline. It intercepts messages at ingestion time and applies configurable actions per organization:

| Action | Description |
|--------|-------------|
| `redact` | Replace PII with `[REDACTED]` — message proceeds to NLP |
| `mask` | Show first/last characters — e.g., `j***@e****.com` |
| `block` | Reject the message with HTTP 422 — no NLP processing |
| `none` | Pass through without modification |

**Key design decisions:**

1. **Non-LLM approach:** Uses `presidio-anonymizer` and regex patterns — lightweight, no LLM call cost
2. **Pre-pipeline:** Runs synchronously in the ingestion router before the ARQ task is enqueued
3. **Org-configurable:** Each org chooses its PII mode and which PII types to scan for
4. **No PII in logs:** Detection events are logged with type and action, but NOT the actual PII value

---

## 2. PII Types & Detection Methods

### 2.1 Supported PII Types

| Type | Pattern / Method | Example |
|------|-----------------|---------|
| `email` | Regex | `user@example.com` |
| `phone` | Regex (E.164, US, international) | `+1-555-123-4567` |
| `ssn` | Regex | `123-45-6789` |
| `credit_card` | Regex (Luhn checksum) | `4111-1111-1111-1111` |
| `address` | spaCy NER (LOC, GPE) | `123 Main St, New York, NY` |
| `name` | spaCy NER (PERSON) | `John Smith` |
| `ip_address` | Regex (IPv4, IPv6) | `192.168.1.1` |
| `date_of_birth` | Regex + context | `1990-01-15` |
| `api_key` | Regex (common patterns) | `sk-proj-xxxxxxxxxxxx` |

### 2.2 Detection Engine

```python
# packages/core/pii/detector.py

import re
from dataclasses import dataclass, field
from typing import Any

import spacy


@dataclass
class PIIDetection:
    """A single PII detection result."""
    type: str                       # email, phone, ssn, etc.
    value: str                      # the detected PII value
    start: int                      # character position in original text
    end: int                        # end character position
    confidence: float               # detection confidence (0.0 to 1.0)
    method: str = "regex"           # "regex" or "spacy_ner"


class PIIDetector:
    """PII detection using regex patterns + spaCy NER.

    Lightweight, no LLM calls. Uses presidio-anonymizer internally
    for pattern analysis and spaCy for NER-based detection.

    Configurable: which PII types to scan for, and minimum confidence threshold.
    """

    # Compiled regex patterns per PII type
    _PATTERNS: dict[str, re.Pattern] = {
        "email": re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
        "phone": re.compile(
            r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b'
        ),
        "ssn": re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
        "credit_card": re.compile(
            r'\b(?:\d{4}[-\s]?){3}\d{4}\b'
        ),
        "ip_address": re.compile(
            r'\b(?:\d{1,3}\.){3}\d{1,3}\b'
        ),
        "date_of_birth": re.compile(
            r'\b\d{4}[-/]\d{2}[-/]\d{2}\b'
        ),
        "api_key": re.compile(
            r'\b(sk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{36,}|AKIA[0-9A-Z]{16})\b'
        ),
    }

    def __init__(
        self,
        enabled_types: list[str] | None = None,
        min_confidence: float = 0.7,
        nlp_model: str = "en_core_web_trf",
    ) -> None:
        """Initialize the PII detector.

        Args:
            enabled_types: PII types to scan for. None = all types.
            min_confidence: Minimum confidence for a detection to be reported.
            nlp_model: spaCy model for NER-based detection.
        """
        self._enabled_types = set(enabled_types or list(self._PATTERNS.keys()))
        self._min_confidence = min_confidence
        try:
            self._nlp = spacy.load(nlp_model)
        except OSError:
            # Fallback to smaller model if transformer model not available
            self._nlp = spacy.load("en_core_web_sm")

    def detect(self, text: str) -> list[PIIDetection]:
        """Detect PII in the given text.

        Args:
            text: The raw text to scan.

        Returns:
            List of PIIDetection objects, sorted by start position.
        """
        detections: list[PIIDetection] = []

        # 1. Regex-based detection
        for pii_type, pattern in self._PATTERNS.items():
            if pii_type not in self._enabled_types:
                continue
            for match in pattern.finditer(text):
                detections.append(PIIDetection(
                    type=pii_type,
                    value=match.group(),
                    start=match.start(),
                    end=match.end(),
                    confidence=0.9,
                    method="regex",
                ))

        # 2. spaCy NER detection (name, address, organization)
        if "name" in self._enabled_types or "address" in self._enabled_types:
            doc = self._nlp(text)
            for ent in doc.ents:
                pii_type = self._ner_to_pii_type(ent.label_)
                if pii_type and pii_type in self._enabled_types:
                    detections.append(PIIDetection(
                        type=pii_type,
                        value=ent.text,
                        start=ent.start_char,
                        end=ent.end_char,
                        confidence=min(ent._.get("confidence", 0.8), 0.95),
                        method="spacy_ner",
                    ))

        # 3. Sort by position and remove overlaps (keep the longer match)
        detections.sort(key=lambda d: (d.start, -d.end))
        merged = self._merge_overlapping(detections)

        # 4. Filter by confidence
        return [d for d in merged if d.confidence >= self._min_confidence]

    def _ner_to_pii_type(self, ner_label: str) -> str | None:
        """Map spaCy NER labels to PII types."""
        mapping = {
            "PERSON": "name",
            "GPE": "address",
            "LOC": "address",
            "ORG": "organization",
        }
        return mapping.get(ner_label)

    def _merge_overlapping(self, detections: list[PIIDetection]) -> list[PIIDetection]:
        """Merge overlapping detections, keeping the longest span."""
        if not detections:
            return []

        merged = [detections[0]]
        for d in detections[1:]:
            prev = merged[-1]
            if d.start < prev.end:
                # Overlap: keep the longer one
                if (d.end - d.start) > (prev.end - prev.start):
                    merged[-1] = d
            else:
                merged.append(d)
        return merged
```

---

## 3. Redaction & Masking

### 3.1 Redaction Strategies

```python
# packages/core/pii/redactor.py

from dataclasses import dataclass
from typing import Callable


# Mapping of PII type → redaction strategy
REDACTION_STRATEGIES: dict[str, Callable[[str], str]] = {
    "email": lambda v: "[REDACTED EMAIL]",
    "phone": lambda v: "[REDACTED PHONE]",
    "ssn": lambda v: "[REDACTED SSN]",
    "credit_card": lambda v: "[REDACTED CARD]",
    "address": lambda v: "[REDACTED ADDRESS]",
    "name": lambda v: "[REDACTED NAME]",
    "ip_address": lambda v: "[REDACTED IP]",
    "date_of_birth": lambda v: "[REDACTED DOB]",
    "api_key": lambda v: "[REDACTED KEY]",
}

MASKING_STRATEGIES: dict[str, Callable[[str], str]] = {
    "email": lambda v: v[0] + "***@" + v.split("@")[1][0] + "***",
    "phone": lambda v: v[:2] + "***" + v[-2:],
    "ssn": lambda v: "***-**-" + v[-4:],
    "credit_card": lambda v: v[:4] + "-****-****-" + v[-4:],
    "name": lambda v: v[0] + "***",
    "ip_address": lambda v: v.split(".")[0] + ".***.***.***",
    "date_of_birth": lambda v: "****-**-" + v[-2:],
    "api_key": lambda v: v[:4] + "***...***" + v[-4:],
}


class PIIRedactor:
    """Apply PII redaction or masking to text."""

    def __init__(self, mode: str = "redact") -> None:
        """Initialize redactor.

        Args:
            mode: "redact" (replace with placeholder),
                  "mask" (show first/last chars),
                  "none" (passthrough)

        Raises:
            ValueError: If mode is invalid.
        """
        if mode not in ("redact", "mask", "none"):
            raise ValueError(f"Invalid PII mode: {mode}")
        self._mode = mode

    def apply(self, text: str, detections: list) -> str:
        """Apply PII redaction/masking to text.

        Processes detections in reverse order (by position) to avoid
        offset shifting from replacements.

        Args:
            text: Original text.
            detections: List of PIIDetection objects.

        Returns:
            Redacted/masked text.
        """
        if self._mode == "none":
            return text

        strategy = REDACTION_STRATEGIES if self._mode == "redact" else MASKING_STRATEGIES

        # Process in reverse to preserve offsets
        chars = list(text)
        for d in sorted(detections, key=lambda x: x.start, reverse=True):
            replacement = strategy.get(d.type, lambda v: "[REDACTED]")(d.value)
            chars[d.start:d.end] = list(replacement)

        return "".join(chars)
```

---

## 4. Middleware / Ingestion Hook

PII detection runs synchronously in the message ingestion router, **before** the ARQ task is enqueued. This ensures:

1. PII is never persisted to the database
2. PII is never sent to the LLM
3. Blocked messages never enter the NLP pipeline

### 4.1 Ingestion Hook

```python
# services/api/routers/memory.py — inside the POST /memory handler

from fastapi import APIRouter, Depends, HTTPException, status
from packages.core.pii.detector import PIIDetector, PIIMode
from packages.core.pii.redactor import PIIRedactor

router = APIRouter(prefix="/v1/users/{user_id}", tags=["memory"])


@router.post("/memory", status_code=status.HTTP_202_ACCEPTED)
async def ingest_memory(
    user_id: UUID,
    payload: MemoryIngestRequest,
    service: MemoryService = Depends(get_memory_service),
    org: Organization = Depends(get_current_org),
    user: User = Depends(get_current_user),
) -> dict:
    """Ingest messages into a user's memory.

    Runs PII detection BEFORE enqueuing the ARQ task.
    If PII mode is 'block', raises 422 if PII is detected.
    """
    pii_config = org.quotas.get("pii", {})

    if pii_config.get("mode", "none") != "none":
        detector = PIIDetector(
            enabled_types=pii_config.get("pii_types"),
            min_confidence=pii_config.get("min_confidence", 0.7),
        )
        redactor = PIIRedactor(mode=pii_config["mode"])

    processed_messages = []
    for msg in payload.messages:
        content = msg.content

        if pii_config.get("mode", "none") != "none":
            detections = detector.detect(content)

            if detections:
                # Log detection (without the actual PII value)
                logger.info(
                    "pii.detected",
                    detection_count=len(detections),
                    types=[d.type for d in detections],
                    action=pii_config["mode"],
                    message_length=len(content),
                )

                if pii_config["mode"] == "block":
                    # ⚠️ PII blocking: reject the message
                    detected_types = ", ".join(sorted(set(d.type for d in detections)))
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "code": "PII_DETECTED",
                            "message": (
                                f"Message contains PII ({detected_types}). "
                                f"Redact and resubmit."
                            ),
                            "pii_types": list(set(d.type for d in detections)),
                        },
                    )

                # Redact or mask
                content = redactor.apply(content, detections)

        processed_messages.append(Message(
            role=msg.role,
            content=content,
            created_at=msg.created_at,
            metadata=msg.metadata,
        ))

    # Proceed with normal ingestion (writes to DB, enqueues ARQ)
    result = await service.ingest_messages(
        user_id=user.id,
        messages=processed_messages,
        session_id=payload.session_id,
    )

    return {"status": "accepted", "episode_ids": result.episode_ids}
```

### 4.2 Org Configuration

```json
// organizations.quotas JSONB — PII configuration
{
  "pii": {
    "mode": "redact",
    "pii_types": ["email", "phone", "ssn", "credit_card", "name", "ip_address"],
    "min_confidence": 0.7
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `pii.mode` | string | `"none"` | `"redact"`, `"mask"`, `"block"`, or `"none"` |
| `pii.pii_types` | array | *all types* | Which PII types to scan for |
| `pii.min_confidence` | float | `0.7` | Minimum detection confidence |

---

## 5. Sensitive Data in Logs — Prohibited

**Under no circumstances should actual PII values be logged.** Detection events are logged with:

```python
logger.info(
    "pii.detected",
    # ✅ DO log:
    detection_count=len(detections),
    types=list(set(d.type for d in detections)),       # ["email", "phone"]
    action=pii_config["mode"],                         # "redact"
    message_length=len(content),                        # 142
    org_id=str(org.id),                                # for per-org metrics

    # ❌ NEVER log:
    # values=[d.value for d in detections],            # NO — contains actual PII
    # content=content,                                  # NO — may not be fully redacted
    # user_email=extracted_email,                       # NO
)
```

### 5.1 Automated Log Scrubbing

Add a log processor that scrubs any accidentally leaked PII:

```python
# packages/core/logging/pii_scrubber.py

import re

# Same patterns as the detector — applied to log output
PII_PATTERNS = [
    (r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '[EMAIL SCRUBBED]'),
    (r'\b\d{3}-\d{2}-\d{4}\b', '[SSN SCRUBBED]'),
    (r'\b(?:\d{4}[-\s]?){3}\d{4}\b', '[CARD SCRUBBED]'),
]


def scrub_pii(log_message: str) -> str:
    """Scrub PII patterns from log messages as a safety net."""
    for pattern, replacement in PII_PATTERNS:
        log_message = re.sub(pattern, replacement, log_message)
    return log_message
```

---

## 6. Performance & Latency Budget

PII detection must not add measurable latency to the ingestion path. The total PII detection budget is **≤ 10ms** per message.

| Component | Budget | Notes |
|-----------|--------|-------|
| Regex pattern matching | ≤ 2ms | 7 compiled regex patterns — O(n) scan |
| spaCy NER | ≤ 8ms | `en_core_web_sm` model, no GPU needed |
| Redaction/masking | ≤ 1ms | String replacement in reverse order |
| **Total** | **≤ 11ms** | Well under the 200ms ingestion SLA |

**Caching:** NER results cannot be cached per-message (each message is unique). However, the spaCy model is loaded once at worker start and reused.

---

## 7. Error Handling

| Failure Mode | Detection | Action |
|-------------|-----------|--------|
| spaCy model not loaded | `OSError` at startup | Fall back to regex-only detection. Log warning at startup. |
| Regex compilation failure | `re.error` at startup | Fail fast — crash the worker. Misconfigured pattern. |
| Blocked message | PII detected in `block` mode | Return HTTP 422 with PII types list. Do NOT log the actual PII. |
| Unknown PII type in config | Not in supported types list | Log warning, skip the type. Continue with known types. |

**Fallback on spaCy failure:**

```python
# If spaCy model fails to load, use regex-only detection
class PIIDetector:
    def __init__(self, ...):
        self._nlp = None
        try:
            self._nlp = spacy.load(nlp_model)
        except OSError:
            logger.warning(
                "pii.spacy_model_unavailable",
                model=nlp_model,
                action="falling_back_to_regex_only",
            )
```

---

## 8. Metrics & Observability

### 8.1 Prometheus Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `openzep_pii_detections_total` | Counter | `org_id, pii_type, action` | PII detection events |
| `openzep_pii_blocked_messages_total` | Counter | `org_id` | Messages rejected by block mode |
| `openzep_pii_detection_duration_seconds` | Histogram | — | Time spent in PII detection per message |
| `openzep_pii_redacted_characters_total` | Counter | `org_id` | Total characters redacted |

### 8.2 Structured Logging

```python
# Detection event
logger.info(
    "pii.detected",
    detection_count=len(detections),
    types=list(set(d.type for d in detections)),
    action=pii_config["mode"],
    message_length=len(content),
    duration_ms=round(duration * 1000),
)

# Block event
logger.warning(
    "pii.blocked",
    types=list(set(d.type for d in detections)),
    message_length=len(content),
)

# spaCy fallback
logger.warning(
    "pii.spacy_model_unavailable",
    model=nlp_model,
)
```

---

## 9. Testing Guide

### 9.1 Unit Tests

```python
# tests/unit/pii/test_detector.py

import pytest
from packages.core.pii.detector import PIIDetector


class TestPIIDetector:

    @pytest.fixture
    def detector(self) -> PIIDetector:
        """Detector with all PII types enabled, regex-only = no spaCy dep in unit tests."""
        return PIIDetector(enabled_types=["email", "phone", "ssn", "credit_card", "ip_address"])

    def test_detect_email(self, detector: PIIDetector) -> None:
        detections = detector.detect("Contact me at user@example.com")
        assert len(detections) == 1
        assert detections[0].type == "email"

    def test_detect_multiple_emails(self, detector: PIIDetector) -> None:
        detections = detector.detect("a@b.com and c@d.com")
        assert len(detections) == 2

    def test_detect_ssn(self, detector: PIIDetector) -> None:
        detections = detector.detect("My SSN is 123-45-6789")
        assert len(detections) == 1
        assert detections[0].type == "ssn"

    def test_detect_credit_card(self, detector: PIIDetector) -> None:
        detections = detector.detect("Card: 4111-1111-1111-1111")
        assert len(detections) == 1
        assert detections[0].type == "credit_card"

    def test_detect_phone(self, detector: PIIDetector) -> None:
        detections = detector.detect("Call +1-555-123-4567")
        assert len(detections) == 1
        assert detections[0].type == "phone"

    def test_detect_ip(self, detector: PIIDetector) -> None:
        detections = detector.detect("Server: 192.168.1.1")
        assert len(detections) == 1
        assert detections[0].type == "ip_address"

    def test_no_false_positives_normal_text(self, detector: PIIDetector) -> None:
        detections = detector.detect("Hello, how are you? I am fine.")
        assert len(detections) == 0

    def test_confidence_filter(self, detector: PIIDetector) -> None:
        """Default min_confidence=0.7 should keep confidence=0.9 detections."""
        detections = detector.detect("Email: test@test.com")
        assert all(d.confidence >= 0.7 for d in detections)

    def test_overlapping_detections_merged(self, detector: PIIDetector) -> None:
        """Overlapping detections should be merged, keeping the longer match."""
        # This would require a contrived case where regexes overlap
        # Real-world test: email and name on same address
        detections = detector.detect("John.Doe@company.com")
        assert len(detections) == 1  # email match, not name
        assert detections[0].type == "email"
```

### 9.2 Redaction Tests

```python
# tests/unit/pii/test_redactor.py

import pytest
from packages.core.pii.redactor import PIIRedactor
from packages.core.pii.detector import PIIDetection


class TestPIIRedactor:

    @pytest.fixture
    def redactor_redact(self) -> PIIRedactor:
        return PIIRedactor(mode="redact")

    @pytest.fixture
    def redactor_mask(self) -> PIIRedactor:
        return PIIRedactor(mode="mask")

    def test_redact_email(self, redactor_redact: PIIRedactor) -> None:
        detections = [PIIDetection(type="email", value="user@example.com",
                                    start=0, end=16, confidence=0.9, method="regex")]
        result = redactor_redact.apply("user@example.com", detections)
        assert result == "[REDACTED EMAIL]"

    def test_mask_email(self, redactor_mask: PIIRedactor) -> None:
        detections = [PIIDetection(type="email", value="user@example.com",
                                    start=0, end=16, confidence=0.9, method="regex")]
        result = redactor_mask.apply("user@example.com", detections)
        assert "@" in result  # still has email structure
        assert "user" not in result  # local part masked

    def test_multiple_redactions(self, redactor_redact: PIIRedactor) -> None:
        text = "Email: user@test.com, Phone: 555-123-4567"
        detections = [
            PIIDetection(type="email", value="user@test.com",
                          start=7, end=20, confidence=0.9, method="regex"),
            PIIDetection(type="phone", value="555-123-4567",
                          start=28, end=40, confidence=0.9, method="regex"),
        ]
        result = redactor_redact.apply(text, detections)
        assert "[REDACTED EMAIL]" in result
        assert "[REDACTED PHONE]" in result
        assert "user@test.com" not in result

    def test_no_pii_passthrough(self, redactor_redact: PIIRedactor) -> None:
        text = "Hello, this is a normal message."
        result = redactor_redact.apply(text, [])
        assert result == text

    def test_mode_none_passthrough(self) -> None:
        redactor = PIIRedactor(mode="none")
        text = "Email: user@test.com"
        detections = [PIIDetection(type="email", value="user@test.com",
                                    start=7, end=20, confidence=0.9, method="regex")]
        result = redactor.apply(text, detections)
        assert result == text  # unchanged
```

### 9.3 Integration Tests

- Detection on a full conversation with multiple PII types
- Block mode returns 422 with correct error format
- Redacted content is what gets persisted and sent to LLM
- Verify PII values are NOT present in log output
- spaCy model fallback on missing model

---

## 10. Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PII_ENABLED_TYPES` | *all* | Comma-separated list of PII types to scan |
| `PII_DEFAULT_MODE` | `none` | Default PII mode for new orgs |
| `PII_MIN_CONFIDENCE` | `0.7` | Minimum detection confidence |
| `PII_SPACY_MODEL` | `en_core_web_trf` | spaCy NER model — `en_core_web_sm` for faster, `en_core_web_trf` for accuracy |
| `PII_REGEX_ONLY` | `false` | Skip spaCy NER (faster, but misses names/addresses) |

---

## 11. Open Questions

| ID | Question | Status |
|----|----------|--------|
| PII-01 | Should PII detection run on assistant messages too, or only user messages? | **Decision:** Both — assistant messages may contain user PII that the assistant repeats back. |
| PII-02 | Should we add PII scanning in the LLM response path (to catch leaked PII from the LLM)? | **Decision:** Phase 4 — add a post-response scan for the dashboard warning panel. |
| PII-03 | Support for non-English PII (e.g., European phone formats, Chinese ID numbers)? | **Decision:** Phase 4 — community contributions welcome. Phase 3 covers US-centric. |

---

## 12. Related Documents

| Document | Why |
|----------|-----|
| [02-entity-extraction.md](02-entity-extraction.md) | Entity extraction receives redacted content |
| [03-fact-extraction.md](03-fact-extraction.md) | Fact extraction receives redacted content |
| [03-core-memory/01-message-ingestion.md](../03-core-memory/01-message-ingestion.md) | PII detection runs in the ingestion router |
| [08-api-gateway/02-error-handling.md](../08-api-gateway/02-error-handling.md) | 422 PII_BLOCKED error response format |
| [12-observability/01-structured-logging.md](../12-observability/01-structured-logging.md) | Log PII scrubbing processor |
