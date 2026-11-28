"""Pydantic schemas for PII detection configuration and statistics.

Stored per-user in ``organizations.quotas`` JSONB under the ``"pii"`` key.
Schemas must never import from ``models/``, ``services/``, or ``routers/``.
"""

from __future__ import annotations
