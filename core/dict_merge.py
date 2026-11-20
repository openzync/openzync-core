"""Shared deep-merge helper for layered config dicts.

Single implementation used by both ``repositories.user_repository``
(recursive merge) and ``services.user_config_service`` (recursive merge)
so the two layers can never drift apart.

Removal semantics live in the callers, not here: ``None`` values in the
overlay are kept as-is.  Callers strip ``None`` first, where ``None``
removes that **top-level** key only — a nested ``None`` never deletes a
deep path (it replaces the nested value with ``None``, which the caller
then drops at the top level).
"""

from __future__ import annotations

from typing import Any


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Return ``base`` overlaid with ``overlay`` (nested dicts merge).

    Neither input is mutated.  Keys set to ``None`` in the overlay are
    kept as-is here — removal semantics (``None`` removes) live in the
    callers, which strip ``None`` before calling.

    Args:
        base: Lower-precedence mapping.
        overlay: Higher-precedence mapping (wins on conflict).

    Returns:
        A new merged dict.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
