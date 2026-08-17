"""Unit tests for the permissions validator in the user + API-key schemas.

The unified RBAC model validates permission strings against
``core.rbac.ALL_PERMISSIONS`` at the schema boundary (fail-closed):
- Unknown strings → ``ValidationError`` (422 at the API).
- Duplicates are deduped.
- The result is sorted.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.api_keys import CreateApiKeyRequest
from schemas.users import CreateUserRequest, UpdateUserRequest

pytestmark = pytest.mark.unit


class TestUserPermissionsValidator:
    """CreateUserRequest.permissions — reject unknown, dedupe, sort."""

    def test_rejects_unknown_permission_string(self) -> None:
        """A string outside ALL_PERMISSIONS → ValidationError."""
        with pytest.raises(ValidationError) as exc:
            CreateUserRequest(
                external_id="u_1",
                permissions=["project:read", "not:a-permission"],
            )
        assert "Unknown permissions" in str(exc.value)

    def test_rejects_legacy_scope_strings(self) -> None:
        """Legacy scope vocabulary (``read``/``admin:write``) is rejected."""
        with pytest.raises(ValidationError):
            CreateUserRequest(external_id="u_1", permissions=["read"])
        with pytest.raises(ValidationError):
            CreateUserRequest(external_id="u_1", permissions=["admin:write"])

    def test_dedupes_and_sorts(self) -> None:
        """Duplicates collapse and the result is sorted."""
        req = CreateUserRequest(
            external_id="u_1",
            permissions=["members:write", "project:read", "project:read"],
        )
        assert req.permissions == ["members:write", "project:read"]

    def test_empty_list_allowed(self) -> None:
        """Empty permissions = wildcard via role — valid."""
        req = CreateUserRequest(external_id="u_1", permissions=[])
        assert req.permissions == []

    def test_all_permissions_accepted(self) -> None:
        """Every string in ALL_PERMISSIONS passes and sorts."""
        from core.rbac import ALL_PERMISSIONS

        req = CreateUserRequest(external_id="u_1", permissions=list(ALL_PERMISSIONS))
        assert req.permissions == sorted(ALL_PERMISSIONS)


class TestUpdateUserPermissionsValidator:
    """UpdateUserRequest.permissions — same vocabulary, optional field."""

    def test_rejects_unknown_permission_string(self) -> None:
        """A string outside ALL_PERMISSIONS → ValidationError."""
        with pytest.raises(ValidationError) as exc:
            UpdateUserRequest(permissions=["bogus:perm"])
        assert "Unknown permissions" in str(exc.value)

    def test_dedupes_and_sorts(self) -> None:
        """Duplicates collapse and the result is sorted."""
        req = UpdateUserRequest(
            permissions=["project:write", "project:write", "project:read"],
        )
        assert req.permissions == ["project:read", "project:write"]

    def test_absent_field_is_not_touched(self) -> None:
        """No permissions key → no change (sentinel-safe update)."""
        req = UpdateUserRequest(name="Alice")
        assert req.permissions is None


class TestApiKeyPermissionsValidator:
    """CreateApiKeyRequest.permissions — same vocabulary, dedupe, sort."""

    def test_rejects_unknown_permission_string(self) -> None:
        """A string outside ALL_PERMISSIONS → ValidationError."""
        with pytest.raises(ValidationError) as exc:
            CreateApiKeyRequest(name="key", permissions=["project:read", "nope"])
        assert "Unknown permissions" in str(exc.value)

    def test_rejects_legacy_scope_strings(self) -> None:
        """Legacy scope vocabulary is rejected for API keys too."""
        with pytest.raises(ValidationError):
            CreateApiKeyRequest(name="key", permissions=["write"])

    def test_dedupes_and_sorts(self) -> None:
        """Duplicates collapse and the result is sorted."""
        req = CreateApiKeyRequest(
            name="key",
            permissions=["project:write", "project:read", "project:write"],
        )
        assert req.permissions == ["project:read", "project:write"]

    def test_empty_list_allowed(self) -> None:
        """Empty permissions → member defaults seeded by the service layer."""
        req = CreateApiKeyRequest(name="key", permissions=[])
        assert req.permissions == []