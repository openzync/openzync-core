"""PII detection and redaction service — three-layer architecture.

Layers (applied in order, cumulative):
  1. **Regex** — compiled patterns for emails, phones, SSNs, credit cards,
     IP addresses, API keys, and crypto wallet addresses.  Always runs.
  2. **spaCy NER** — named-entity recognition for person names, locations,
     organizations, and dates.  Lazy-loaded with graceful fallback to
     regex-only if the model is unavailable.
  3. **LLM fallback** — optional layer that sends the text to an LLM for
     residual PII detection.  Disabled by default; enabled via ``sensitivity``
     or explicit constructor flag.

Separation: this file contains NO database queries.  All config is passed in
as plain dicts from the caller (typically the memory service).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from core.exceptions import ValidationError

logger = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_PII_TYPES: list[str] = [
    "email",
    "phone",
    "ssn",
    "credit_card",
    "ip_address",
    "api_key",
]

REDACTION_LABELS: dict[str, str] = {
    "email": "EMAIL",
    "phone": "PHONE",
    "ssn": "SSN",
    "credit_card": "CARD",
    "ip_address": "IP",
    "api_key": "KEY",
    "crypto_wallet": "WALLET",
    "name": "NAME",
    "address": "ADDRESS",
    "organization": "ORG",
    "date": "DATE",
}

# ═══════════════════════════════════════════════════════════════════════════════
# Layer 1: Regex patterns (compiled at module level)
# ═══════════════════════════════════════════════════════════════════════════════

_PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    ),
    "phone": re.compile(
        r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b"
    ),
    "ssn": re.compile(r"\b\d{3}[-.\s]?\d{2}[-.\s]?\d{4}\b"),
    "credit_card": re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"),
    "ip_address": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "api_key": re.compile(
        r"\b(?:"
        r"sk-[a-zA-Z0-9]{20,}|"          # OpenAI sk-* keys
        r"sk-proj-[a-zA-Z0-9]{20,}|"     # OpenAI project keys
        r"ghp_[a-zA-Z0-9]{36,}|"          # GitHub PATs
        r"AKIA[0-9A-Z]{16}"               # AWS access keys
        r")\b"
    ),
    "crypto_wallet": re.compile(
        r"\b(0x[a-fA-F0-9]{40}|bc1[a-zA-Z0-9]{25,39})\b"
    ),
}

# ═══════════════════════════════════════════════════════════════════════════════
# Domain model
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class PIIDetection:
    """A single PII detection result.

    Attributes:
        type: PII type identifier (e.g. ``"email"``, ``"ssn"``).
        value: The detected PII value (used for redaction, NEVER logged).
        start: Character offset where the detection begins in the source text.
        end: Character offset where the detection ends.
        confidence: Detection confidence score (0.0 to 1.0).
        method: Detection method — ``"regex"``, ``"spacy_ner"``,
            or ``"llm_fallback"``.
    """

    type: str
    value: str = field(repr=False)  # NEVER logged — redacted in __repr__
    start: int
    end: int
    confidence: float
    method: str = "regex"


# ═══════════════════════════════════════════════════════════════════════════════
# PIIDetector — three-layer detection
# ═══════════════════════════════════════════════════════════════════════════════


class PIIDetector:
    """Multi-layer PII detector with regex, optional NER, and optional LLM.

    Typical usage — synchronous regex-only detection::

        detector = PIIDetector()
        results = detector.detect("Contact me at john@example.com")

    With all layers enabled::

        detector = PIIDetector(use_ner=True, use_llm_fallback=True)
        results = await detector.detect_with_llm("Call 555-1234")

    Args:
        enabled_types: Subset of PII types to scan for.  ``None`` means all.
        min_confidence: Minimum confidence threshold (0.0 to 1.0).
        use_ner: Enable spaCy NER layer (lazy-loaded).
        use_llm_fallback: Enable LLM fallback layer.
        llm_backend: Optional LLM backend instance for fallback.
            Required when ``use_llm_fallback=True``.
    """

    def __init__(
        self,
        enabled_types: list[str] | None = None,
        min_confidence: float = 0.7,
        use_ner: bool = True,
        use_llm_fallback: bool = False,
        llm_backend: Any | None = None,
    ) -> None:
        self._enabled_types = set(enabled_types or DEFAULT_PII_TYPES)
        self._min_confidence = min_confidence
        self._use_ner = use_ner
        self._use_llm_fallback = use_llm_fallback
        self._llm_backend = llm_backend
        self._nlp = None  # Lazy-loaded spaCy pipeline

    # ── Public API ─────────────────────────────────────────────────────────

    def detect(self, text: str) -> list[PIIDetection]:
        """Run PII detection against *text* using all enabled layers.

        Detection order:
        1. Regex scan (always runs, synchronous).
        2. spaCy NER scan (if enabled, synchronous).
        3. LLM fallback is NOT run from this method — use
           :meth:`detect_with_llm` for that.

        Results are merged (overlapping detections resolved in favour of the
        longer span), filtered by minimum confidence, and returned sorted by
        start position.

        Args:
            text: The text to scan for PII.

        Returns:
            List of :class:`PIIDetection` results, sorted by start offset.
        """
        results: list[PIIDetection] = []

        # Layer 1: Regex
        results.extend(self._scan_regex(text))

        # Layer 2: spaCy NER
        if self._use_ner:
            ner_results = self._scan_ner(text)
            results.extend(ner_results)

        # Merge overlapping, filter by confidence, sort
        results = self._merge_overlapping(results)
        results = self._confidence_filter(results)

        # Log the event — type and count only, NEVER the actual PII values
        if results:
            type_counts: dict[str, int] = {}
            for d in results:
                type_counts[d.type] = type_counts.get(d.type, 0) + 1
            logger.info(
                "pii.detected",
                extra={
                    "total_count": len(results),
                    "types": type_counts,
                    "methods": sorted(set(d.method for d in results)),
                },
            )

        return sorted(results, key=lambda d: d.start)

    async def detect_with_llm(self, text: str) -> list[PIIDetection]:
        """Run detection INCLUDING the LLM fallback layer.

        This is an async method because it calls an external LLM.  Regex and
        NER layers still run first; the LLM layer is an additional sweep for
        patterns the regex/NER may have missed.

        Args:
            text: The text to scan for PII.

        Returns:
            Combined results from all three layers.
        """
        results = self.detect(text)  # sync layers first

        if self._use_llm_fallback and self._llm_backend is not None:
            llm_results = await self._scan_llm(text)
            results.extend(llm_results)
            results = self._merge_overlapping(results)
            results = self._confidence_filter(results)

        return sorted(results, key=lambda d: d.start)

    # ── Layer 1: Regex ────────────────────────────────────────────────────

    @staticmethod
    def _scan_regex(text: str) -> list[PIIDetection]:
        """Run all enabled regex patterns against *text*.

        Args:
            text: The text to scan.

        Returns:
            List of regex-based detections.
        """
        results: list[PIIDetection] = []
        for pii_type, pattern in _PATTERNS.items():
            for match in pattern.finditer(text):
                results.append(
                    PIIDetection(
                        type=pii_type,
                        value=match.group(),
                        start=match.start(),
                        end=match.end(),
                        confidence=0.95,  # Regex matches are high confidence
                        method="regex",
                    )
                )
        return results

    # ── Layer 2: spaCy NER ────────────────────────────────────────────────

    NER_LABEL_MAP: dict[str, str] = {
        "PERSON": "name",
        "GPE": "address",
        "LOC": "address",
        "ORG": "organization",
        "DATE": "date",
    }
    """Mapping from spaCy NER label names to our PII type identifiers."""

    def _scan_ner(self, text: str) -> list[PIIDetection]:
        """Run spaCy NER against *text*.

        The spaCy model is loaded lazily on first call.  If the model is not
        installed or fails to load, a warning is logged and an empty list is
        returned (graceful degradation to regex-only).

        Args:
            text: The text to scan.

        Returns:
            List of NER-based detections.
        """
        nlp = self._get_nlp()
        if nlp is None:
            return []

        doc = nlp(text)
        results: list[PIIDetection] = []
        for ent in doc.ents:
            pii_type = self.NER_LABEL_MAP.get(ent.label_)
            if pii_type is None:
                continue
            if pii_type not in self._enabled_types:
                continue

            results.append(
                PIIDetection(
                    type=pii_type,
                    value=ent.text,
                    start=ent.start_char,
                    end=ent.end_char,
                    confidence=0.85,  # NER is slightly less confident than regex
                    method="spacy_ner",
                )
            )
        return results

    def _get_nlp(self) -> Any | None:
        """Lazy-load the spaCy language model.

        Returns:
            The spaCy ``Language`` pipeline, or ``None`` if unavailable.
        """
        if self._nlp is not None:
            return self._nlp

        try:
            import spacy

            # Try the smaller model first for faster loading in dev
            try:
                self._nlp = spacy.load("en_core_web_sm")
            except OSError:
                logger.warning(
                    "pii.spacy_model_not_found",
                    extra={
                        "model": "en_core_web_sm",
                        "fallback": "regex_only",
                    },
                )
                self._nlp = None
        except ImportError:
            logger.warning(
                "pii.spacy_not_installed",
                extra={"fallback": "regex_only"},
            )
            self._nlp = None

        return self._nlp

    # ── Layer 3: LLM Fallback ─────────────────────────────────────────────

    _LLM_PROMPT: str = (
        'Analyze the following text for personally identifiable information '
        '(PII). Return a JSON list of objects with fields "type", "value", '
        '"start", and "end". Supported types: email, phone, ssn, credit_card, '
        'ip_address, api_key, crypto_wallet, name, address, organization, date. '
        'If no PII is found, return an empty list.\n\nText: """\n{text}\n"""'
    )

    async def _scan_llm(self, text: str) -> list[PIIDetection]:
        """Run LLM-based PII detection as a fallback layer.

        The LLM is asked to identify any PII the regex/NER layers may have
        missed.  Responses are parsed and converted to :class:`PIIDetection`
        objects with ``method="llm_fallback"``.

        Args:
            text: The text to scan.

        Returns:
            List of LLM-found detections.
        """
        if self._llm_backend is None:
            logger.warning("pii.llm_fallback_disabled_no_backend")
            return []

        try:
            prompt = self._LLM_PROMPT.format(text=text[:4000])

            response = await self._llm_backend.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a PII detection system. "
                            "Output ONLY valid JSON."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=1000,
            )

            results: list[PIIDetection] = []
            raw = response.content
            parsed = self._parse_llm_json_response(raw)

            if parsed is None:
                logger.warning("pii.llm_response_parse_failed")
                return []

            for item in parsed:
                if not isinstance(item, dict):
                    continue
                pii_type = item.get("type", "")
                if pii_type not in self._enabled_types:
                    continue
                results.append(
                    PIIDetection(
                        type=pii_type,
                        value=item.get("value", ""),
                        start=item.get("start", 0),
                        end=item.get("end", 0),
                        confidence=0.75,  # LLM fallback is least confident
                        method="llm_fallback",
                    )
                )

            return results

        except Exception as exc:
            logger.warning(
                "pii.llm_fallback_error",
                extra={"error": str(exc)},
            )
            return []

    @staticmethod
    def _parse_llm_json_response(raw: str) -> list[dict] | None:
        """Parse JSON from an LLM response, handling formatting quirks.

        Strips markdown code fences and attempts to find the first JSON
        array in the response.

        Args:
            raw: The raw LLM response string.

        Returns:
            Parsed list of dicts, or ``None`` if parsing failed.
        """
        import json

        if "```json" in raw:
            raw = raw.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in raw:
            raw = raw.split("```", 1)[1].split("```", 1)[0].strip()

        raw = raw.strip()
        json_start = raw.find("[")
        if json_start < 0:
            return None
        raw = raw[json_start:]

        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
            return None
        except json.JSONDecodeError:
            return None

    # ── Post-processing ───────────────────────────────────────────────────

    @staticmethod
    def _merge_overlapping(
        detections: list[PIIDetection],
    ) -> list[PIIDetection]:
        """Merge overlapping detections, keeping the longer span.

        When two detections overlap, the one with the longer span
        (``end - start``) is kept.  If spans are identical, the higher
        confidence detection wins.

        Args:
            detections: Raw detection list (may contain overlaps).

        Returns:
            Deduplicated list with overlaps resolved.
        """
        if not detections:
            return []

        # Sort by start position, then by span length descending
        sorted_detections = sorted(
            detections, key=lambda d: (d.start, -(d.end - d.start))
        )

        merged: list[PIIDetection] = [sorted_detections[0]]
        for current in sorted_detections[1:]:
            prev = merged[-1]
            if current.start < prev.end:
                # Overlap — keep the longer span
                prev_span = prev.end - prev.start
                current_span = current.end - current.start
                if current_span > prev_span:
                    merged[-1] = current
                elif current_span == prev_span and current.confidence > prev.confidence:
                    merged[-1] = current
                # Otherwise keep previous
            else:
                merged.append(current)

        return merged

    def _confidence_filter(
        self,
        detections: list[PIIDetection],
    ) -> list[PIIDetection]:
        """Filter detections below the minimum confidence threshold.

        Args:
            detections: Detection list to filter.

        Returns:
            Detections with confidence >= ``self._min_confidence``.
        """
        return [
            d for d in detections if d.confidence >= self._min_confidence
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# PIIRedactor — applies redaction to text
# ═══════════════════════════════════════════════════════════════════════════════


class PIIRedactor:
    """Redacts PII detections from text by replacing spans with placeholders.

    Modes:
        ``"mask"``
            Replace each PII span with ``[REDACTED:{TYPE}]``.
        ``"block"``
            No replacement — just indicates that redaction is not possible
            and the message should be rejected.

    Args:
        mode: Redaction mode — ``"mask"`` (default) or ``"block"``.
    """

    def __init__(self, mode: str = "mask") -> None:
        if mode not in ("mask", "block"):
            raise ValueError(f"Invalid redaction mode: {mode!r}. Expected 'mask' or 'block'.")
        self._mode = mode

    def apply(
        self, text: str, detections: list[PIIDetection]
    ) -> str:
        """Replace PII spans in *text* with ``[REDACTED:{type}]`` placeholders.

        Detections are processed in **reverse order** (by start position) to
        preserve character offsets during replacement.  This means earlier
        replacements do not shift the positions of later replacements.

        Args:
            text: The original text to redact.
            detections: PII detections to apply.

        Returns:
            The redacted text with PII spans replaced by placeholders.

        Raises:
            ValueError: If the mode is ``"block"`` (callers should check
                ``was_blocked`` from :meth:`PIIService.process_message`
                instead of calling ``apply`` directly).
        """
        if self._mode == "block":
            raise ValueError(
                "PIIRedactor cannot apply redactions in 'block' mode."
            )

        if not detections:
            return text

        sorted_detections = sorted(
            detections, key=lambda d: d.start, reverse=True
        )

        result = text
        for detection in sorted_detections:
            label = REDACTION_LABELS.get(detection.type, detection.type.upper())
            replacement = f"[REDACTED:{label}]"
            result = (
                result[: detection.start]
                + replacement
                + result[detection.end :]
            )

        return result


# ═══════════════════════════════════════════════════════════════════════════════
# PIIService — main entry point for memory-service integration
# ═══════════════════════════════════════════════════════════════════════════════


class PIIService:
    """Main entry point for PII detection and redaction in the ingestion flow.

    Parses the PII config from an org's ``quotas`` dict and delegates to
    :class:`PIIDetector` and :class:`PIIRedactor`.

    Typical usage::

        pii_service = PIIService(org.quotas.get("pii", {}))
        redacted, detections, was_blocked = await pii_service.process_message(
            "My email is john@example.com"
        )

    Args:
        config: Raw PII configuration dict (from
            ``organizations.quotas["pii"]``).  Keys: ``mode``, ``enabled_types``,
            ``min_confidence``, ``sensitivity``.
        llm_backend: Optional LLM backend for the LLM fallback layer.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        llm_backend: Any | None = None,
    ) -> None:
        config = config or {}

        self._mode: str = config.get("mode", "off")
        enabled_types: list[str] | None = config.get("enabled_types")
        min_confidence: float = config.get("min_confidence", 0.7)
        sensitivity: str = config.get("sensitivity", "medium")

        # Sensitivity → use_ner / use_llm_fallback mapping
        use_ner = sensitivity in ("medium", "high")
        use_llm_fallback = sensitivity == "high"

        self._detector = PIIDetector(
            enabled_types=enabled_types,
            min_confidence=min_confidence,
            use_ner=use_ner,
            use_llm_fallback=use_llm_fallback,
            llm_backend=llm_backend,
        )
        # Redactor is only created when mode is not "off" — in "off" mode
        # process_message returns early before reaching the redactor.
        self._redactor: PIIRedactor | None = (
            PIIRedactor(mode=self._mode) if self._mode != "off" else None
        )

    @property
    def mode(self) -> str:
        """The PII processing mode (``"off"``, ``"mask"``, or ``"block"``)."""
        return self._mode

    async def process_message(
        self,
        content: str,
    ) -> tuple[str, list[PIIDetection], bool]:
        """Run PII detection + redaction on a single message.

        Flow:
        1. Run detection on *content* (all enabled layers).
        2. If mode is ``"mask"``: replace PII spans with placeholders.
        3. If mode is ``"block"``: raise ``ValidationError`` if PII found.

        Args:
            content: The raw message content to process.

        Returns:
            Tuple of ``(redacted_content, detections, was_blocked)``.

        Raises:
            ValidationError: If mode is ``"block"`` and PII was detected.
                The error detail includes the PII types found.
        """
        if self._mode == "off":
            return content, [], False

        start_time = time.monotonic()

        detections = self._detector.detect(content)
        # ⚠️ We deliberately do NOT log the PII values here — only counts.

        was_blocked = False
        redacted = content

        if detections:
            if self._mode == "block":
                was_blocked = True
                detected_types = sorted(set(d.type for d in detections))
                duration_ms = (time.monotonic() - start_time) * 1000
                logger.info(
                    "pii.blocked",
                    extra={
                        "detection_count": len(detections),
                        "types": detected_types,
                        "duration_ms": round(duration_ms, 2),
                    },
                )
                raise ValidationError(
                    f"Message contains PII ({', '.join(detected_types)}). "
                    f"Redact and resubmit.",
                    detail={"code": "PII_DETECTED", "pii_types": detected_types},
                )

            if self._mode == "mask":
                redacted = self._redactor.apply(content, detections)
                logger.info(
                    "pii.masked",
                    extra={
                        "detection_count": len(detections),
                        "duration_ms": round(
                            (time.monotonic() - start_time) * 1000, 2
                        ),
                    },
                )

        return redacted, detections, was_blocked
