"""Observation service — backward-compatible re-export.

The implementation lives in :mod:`services.observations` (facade
:class:`~services.observations.ObservationService` composing focused
mixins). This module preserves the original import path for workers
and tests.
"""

from __future__ import annotations

from services.observations import (
    BehavioralPattern,
    CoOccurrencePattern,
    ObservationService,
    TemporalGapPattern,
    _is_burst,
    _is_monotonic,
    _stddev,
)

__all__ = [
    "BehavioralPattern",
    "CoOccurrencePattern",
    "ObservationService",
    "TemporalGapPattern",
    "_is_burst",
    "_is_monotonic",
    "_stddev",
]
