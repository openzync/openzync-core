"""Unit tests for core.locales — the shipped locale allowlist.

The allowlist is the contract between the schema validators, the
``users.locale`` column default, and the ``prompts/email/{locale}/``
template tree.  Tests here pin the set and prove every allowed locale
ships the full template set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.locales import ALLOWED_LOCALES, DEFAULT_LOCALE

pytestmark = pytest.mark.unit

_TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "prompts" / "email"
_EMAIL_TYPES = ("otp", "password_changed", "invite")
_KINDS = ("html", "txt", "subject")


class TestAllowedLocales:
    """Pin the shipped locale set."""

    def test_default_locale_is_en(self) -> None:
        assert DEFAULT_LOCALE == "en"

    def test_default_locale_is_allowed(self) -> None:
        assert DEFAULT_LOCALE in ALLOWED_LOCALES

    def test_allowed_locales_exactly_en(self) -> None:
        """Only ``en`` is shippable — adding a locale requires its templates."""
        assert set(ALLOWED_LOCALES) == {"en"}

    def test_every_allowed_locale_has_full_template_set(self) -> None:
        """Each allowed locale must ship {html,txt,subject} for every email type."""
        for locale in ALLOWED_LOCALES:
            for name in _EMAIL_TYPES:
                for kind in _KINDS:
                    template = _TEMPLATE_DIR / locale / f"{name}.{kind}.jinja2"
                    assert template.is_file(), f"missing template: {template}"
