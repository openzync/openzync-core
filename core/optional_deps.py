"""Startup probe for optional (extras-gated) dependencies.

PII (``openzync[pii]``) and OCR (``openzync[ocr]``) libraries are absent
from the production images on purpose — every consumer imports them
lazily and treats ``ImportError`` as a loud, non-retryable failure (never
a silent fallback).  This probe runs once at boot (API lifespan + worker
main) so a missing extra is visible in logs AND metrics instead of only
surfacing on the first job that needs it.
"""

from __future__ import annotations

import importlib.util
import logging

from prometheus_client import Gauge

logger = logging.getLogger(__name__)

OPTIONAL_DEPS: dict[str, str] = {
    "spacy": "pii",
    "en_core_web_sm": "pii",
    "fitz": "ocr",
    "pytesseract": "ocr",
    "PIL": "ocr",
}
"""Importable module → extras name that provides it."""

optional_dep_missing = Gauge(
    "openzync_optional_dep_missing",
    "1 when an optional dependency is unavailable at startup, else 0",
    labelnames=["dep"],
)


def check_optional_deps() -> dict[str, bool]:
    """Check which optional dependencies are importable.

    Returns:
        Mapping of module name → ``True`` when importable.
    """
    return {
        name: importlib.util.find_spec(name) is not None
        for name in OPTIONAL_DEPS
    }


def log_optional_deps(statuses: dict[str, bool] | None = None) -> dict[str, bool]:
    """Log and metric the optional-dependency probe result.

    Args:
        statuses: Pre-computed :func:`check_optional_deps` result
            (recomputed when ``None`` — keeps call sites one line).

    Returns:
        The per-module availability mapping.
    """
    statuses = statuses if statuses is not None else check_optional_deps()
    for name, available in statuses.items():
        optional_dep_missing.labels(dep=name).set(0 if available else 1)
        if not available:
            logger.warning(
                "startup.optional_dep_missing",
                extra={
                    "dep": name,
                    "extra": OPTIONAL_DEPS[name],
                    "detail": f"Install openzync[{OPTIONAL_DEPS[name]}] for this capability.",
                },
            )
    logger.info(
        "startup.optional_deps_checked",
        extra={"missing": sorted(n for n, ok in statuses.items() if not ok)},
    )
    return statuses
