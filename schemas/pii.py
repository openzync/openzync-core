"""Pydantic schemas for PII detection configuration and statistics.

Stored per-org in ``organizations.quotas`` JSONB under the ``"pii"`` key.
Schemas must never import from ``models/``, ``services/``, or ``routers/``.
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, Field


class PIIMode(str, enum.Enum):
    """PII processing modes for an organization.

    ``off``
        No PII detection is performed.
    ``mask``
        Detected PII is replaced with ``[REDACTED:{type}]`` placeholders.
    ``block``
        Messages containing PII are rejected with a ``ValidationError``.
    """

    OFF = "off"
    MASK = "mask"
    BLOCK = "block"


class PIIConfig(BaseModel):
    """Per-org PII configuration, stored in ``organizations.quotas`` JSONB.

    Example stored value::

        {
          "pii": {
            "mode": "mask",
            "enabled_types": ["email", "phone", "ssn", "credit_card"],
            "min_confidence": 0.7,
            "sensitivity": "medium"
          }
        }

    Attributes:
        mode: Processing mode — ``off``, ``mask``, or ``block``.
        enabled_types: Which PII types to scan for.  Defaults to all
            regex-supported types.
        min_confidence: Minimum detection confidence threshold (0.0 to 1.0).
            Detections below this threshold are discarded.
        sensitivity: Detection depth — ``low`` (regex only),
            ``medium`` (regex + NER), ``high`` (regex + NER + LLM fallback).
    """

    mode: PIIMode = PIIMode.OFF
    enabled_types: list[str] = Field(
        default_factory=lambda: [
            "email",
            "phone",
            "ssn",
            "credit_card",
            "ip_address",
            "api_key",
        ],
        description="Which PII types to scan for. "
        "Defaults to all regex-supported types.",
    )
    min_confidence: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Minimum detection confidence (0.0 to 1.0). "
        "Detections below this threshold are discarded.",
    )
    sensitivity: str = Field(
        default="medium",
        pattern=r"^(low|medium|high)$",
        description="Sensitivity level: "
        "low=regex only, medium=regex+NER, high=regex+NER+LLM.",
    )


class PIIStats(BaseModel):
    """PII detection statistics for a single message processing.

    Attributes:
        detections_count: Number of PII instances detected.
        types_found: Ordered list of unique PII types found (sorted).
        action_taken: What action was taken — ``"none"``, ``"masked"``,
            or ``"blocked"``.
        duration_ms: Time taken for detection + redaction in milliseconds.
    """

    detections_count: int = 0
    types_found: list[str] = []
    action_taken: str = "none"  # "none", "masked", "blocked"
    duration_ms: float = 0.0
