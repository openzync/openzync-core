"""Schema-level locale validation for the user and profile schemas.

Observed contract (live smoke test): ``{"locale":"en"}`` passes;
``{"locale":"xx"}`` → 422 with ``"Unsupported locale 'xx'. Supported: en."``.
The same rule applies to ``CreateUserRequest``, ``UpdateUserRequest``, and
``UpdateProfileRequest``.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from schemas.auth import DashboardUserResponse, UpdateProfileRequest
from schemas.users import CreateUserRequest, UpdateUserRequest

pytestmark = pytest.mark.unit

_EXTERNAL_ID = "ext-1"
_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

_OPTIONAL_LOCALE_SCHEMAS = [
    pytest.param(UpdateUserRequest, {}, id="update-user"),
    pytest.param(UpdateProfileRequest, {}, id="update-profile"),
]


class TestCreateUserRequestLocale:
    """``POST /v1/users`` — locale is required-optional with a default."""

    def test_defaults_to_en(self) -> None:
        assert CreateUserRequest(external_id=_EXTERNAL_ID).locale == "en"

    def test_accepts_supported_locale(self) -> None:
        assert CreateUserRequest(external_id=_EXTERNAL_ID, locale="en").locale == "en"

    def test_rejects_unsupported_locale_with_exact_message(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateUserRequest(external_id=_EXTERNAL_ID, locale="xx")

        errors = exc_info.value.errors()
        assert errors[0]["loc"] == ("locale",)
        assert errors[0]["msg"].endswith("Unsupported locale 'xx'. Supported: en.")


class TestUpdateUserRequestLocale:
    """``PATCH /v1/users/{id}`` — locale is optional; absent = no change."""

    def test_accepts_supported_locale(self) -> None:
        assert UpdateUserRequest(locale="en").locale == "en"

    def test_none_passes(self) -> None:
        assert UpdateUserRequest(locale=None).locale is None

    def test_rejects_unsupported_locale_with_exact_message(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            UpdateUserRequest(locale="xx")

        errors = exc_info.value.errors()
        assert errors[0]["loc"] == ("locale",)
        assert errors[0]["msg"].endswith("Unsupported locale 'xx'. Supported: en.")


class TestUpdateProfileRequestLocale:
    """``PATCH /v1/auth/me`` — the observed smoke-test surface."""

    def test_accepts_supported_locale(self) -> None:
        assert UpdateProfileRequest(locale="en").locale == "en"

    def test_none_passes(self) -> None:
        assert UpdateProfileRequest(locale=None).locale is None

    def test_rejects_unsupported_locale_with_exact_message(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            UpdateProfileRequest(locale="xx")

        errors = exc_info.value.errors()
        assert errors[0]["loc"] == ("locale",)
        assert errors[0]["msg"].endswith("Unsupported locale 'xx'. Supported: en.")


class TestDashboardUserResponseLocale:
    """``GET /v1/auth/me`` — legacy fields intact, locale defaulted to en."""

    def test_defaults_to_en(self) -> None:
        resp = DashboardUserResponse(
            id=UUID("00000000-0000-0000-0000-000000000002"),
            email="admin@acme.com",
            organization_id=_ORG_ID,
        )
        assert resp.locale == "en"

    def test_legacy_fields_present_alongside_locale(self) -> None:
        """Additive change — the legacy profile fields survive."""
        resp = DashboardUserResponse(
            id=UUID("00000000-0000-0000-0000-000000000002"),
            email="admin@acme.com",
            name="Admin",
            role="admin",
            organization_id=_ORG_ID,
            is_email_verified=True,
            mfa_enabled=False,
            must_change_password=True,
        )
        assert resp.locale == "en"
        assert resp.is_email_verified is True
        assert resp.mfa_enabled is False
        assert resp.must_change_password is True
        assert resp.name == "Admin"
