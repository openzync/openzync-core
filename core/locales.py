"""Supported UI locales — BCP-47 lowercase tags.

Single source of truth for which locales OpenZync ships.  Email templates
live under ``prompts/email/{locale}/``; a locale in this set is guaranteed
to have at least a fallback (``en``) template.  Adding a locale means
shipping its templates AND adding its tag here — the allowlist keeps the
two in sync so a user can never pick a locale with no email copy.
"""

from __future__ import annotations

DEFAULT_LOCALE: str = "en"
"""Locale used when no preference is stored (``users.locale`` default)."""

ALLOWED_LOCALES: frozenset[str] = frozenset({"en"})
"""Locales users may select.  Grow as per-locale templates are shipped."""
