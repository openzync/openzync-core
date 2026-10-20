"""ARQ queue-name construction.

The queue label is shared by the API (which enqueues jobs) and the worker
(which consumes them), so it lives in ``core/`` — the API-side modules
must not import from the worker package.
"""

from __future__ import annotations

QUEUE_TYPES: frozenset[str] = frozenset({"high", "low"})
"""The two queue types — ``high`` (real-time ingestion) and ``low`` (batch)."""


def get_queue_name(env: str, queue_type: str) -> str:
    """Generate a namespaced queue name.

    Pattern: ``OpenZync:{env}:queue:{queue_type}``

    Examples:
        ``OpenZync:dev:queue:high``
        ``OpenZync:prod:queue:low``

    Args:
        env: Environment name (e.g. ``dev``, ``staging``, ``prod``).
        queue_type: Queue type — one of ``"high"`` (real-time ingestion) or
            ``"low"`` (scheduled batch / background maintenance).

    Returns:
        Fully qualified queue name string.

    Raises:
        ValueError: If ``queue_type`` is not ``"high"`` or ``"low"``.
    """
    if queue_type not in QUEUE_TYPES:
        raise ValueError(f"queue_type must be 'high' or 'low', got {queue_type!r}")
    return f"OpenZync:{env}:queue:{queue_type}"
