"""PII detection value object — one detection result with character offsets."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PIIDetection:
    """A single PII detection result.

    Attributes:
        type: PII type identifier (e.g. ``"email"``, ``"ssn"``).
        value: The detected PII value (used for redaction, NEVER logged).
        start: Character offset where the detection begins in the source text.
        end: Character offset where the detection ends.
        confidence: Detection confidence score (0.0 to 1.0).
        method: Detection method — ``"regex"`` or ``"spacy_ner"``.
    """

    type: str
    value: str = field(repr=False)  # NEVER logged — redacted in __repr__
    start: int
    end: int
    confidence: float
    method: str = "regex"
