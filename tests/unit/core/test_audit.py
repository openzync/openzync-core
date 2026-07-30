"""Unit tests for audit metadata decorator (``core.audit``).

Covers:
- ``audit_action``: attaches metadata to handler functions
- ``get_audit_metadata``: reads metadata back from decorated functions
- Display name auto-generation from action
- Metadata isolation between decorated functions
- Functions without metadata return ``None``
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from core.audit import audit_action, get_audit_metadata


@pytest.mark.unit
class TestAuditAction:
    """``audit_action`` decorator attaches metadata to handler functions."""

    def test_sets_audit_attributes(self) -> None:
        """The decorator sets ``_audit_action``, ``_audit_resource``, and ``_audit_display``."""

        @audit_action("session.create", "session", "Session created")
        async def handler() -> dict[str, str]:
            return {"status": "ok"}

        assert handler._audit_action == "session.create"
        assert handler._audit_resource == "session"
        assert handler._audit_display == "Session created"

    def test_auto_generates_display_name(self) -> None:
        """When ``display`` is omitted, it's auto-generated from the action."""

        @audit_action("user.login", "user")
        async def handler() -> dict[str, str]:
            return {"status": "ok"}

        assert handler._audit_display == "User Login"

    def test_auto_generates_display_with_dots_and_underscores(self) -> None:
        """Action with dots and underscores generates a human-readable display name."""

        @audit_action("item.create_v2", "item")
        async def handler() -> dict[str, str]:
            return {"status": "ok"}

        assert handler._audit_display == "Item Create V2"

    def test_different_actions_have_independent_metadata(self) -> None:
        """Two decorated functions have independent metadata."""

        @audit_action("session.create", "session", "Session created")
        async def create_session() -> dict[str, str]:
            return {"status": "ok"}

        @audit_action("session.delete", "session", "Session deleted")
        async def delete_session() -> dict[str, str]:
            return {"status": "ok"}

        assert create_session._audit_action == "session.create"
        assert delete_session._audit_action == "session.delete"
        assert create_session._audit_display == "Session created"
        assert delete_session._audit_display == "Session deleted"

    def test_decorator_returns_same_function_type(self) -> None:
        """The decorator preserves the callable."""

        @audit_action("test.action", "test")
        async def handler() -> dict[str, str]:
            return {"status": "ok"}

        # Should still be callable
        assert callable(handler)

    def test_sync_function_works_with_decorator(self) -> None:
        """The decorator works with synchronous functions too."""

        @audit_action("sync.action", "sync")
        def sync_handler() -> str:
            return "done"

        assert sync_handler._audit_action == "sync.action"
        assert sync_handler() == "done"

    def test_decorator_without_display_uses_action(self) -> None:
        """When display is omitted, ``get_audit_metadata`` returns the auto-generated display."""

        @audit_action("test.action", "test")
        async def handler() -> dict[str, str]:
            return {"status": "ok"}

        metadata = get_audit_metadata(handler)
        assert metadata is not None
        assert metadata["display"] == "Test Action"


@pytest.mark.unit
class TestGetAuditMetadata:
    """``get_audit_metadata`` reads audit metadata from endpoint functions."""

    def test_returns_metadata_for_decorated_function(self) -> None:
        """A decorated function returns the expected metadata dict."""

        @audit_action("item.delete", "item", "Item deleted")
        async def handler() -> dict[str, str]:
            return {"status": "ok"}

        metadata = get_audit_metadata(handler)
        assert metadata == {
            "action": "item.delete",
            "resource": "item",
            "display": "Item deleted",
        }

    def test_returns_none_for_undecorated_function(self) -> None:
        """An undecorated function returns ``None``."""

        async def handler() -> dict[str, str]:
            return {"status": "ok"}

        metadata = get_audit_metadata(handler)
        assert metadata is None

    def test_returns_none_for_lambda(self) -> None:
        """A lambda (undecorated) returns ``None``."""
        metadata = get_audit_metadata(lambda: None)  # type: ignore[arg-type]
        assert metadata is None

    def test_returns_none_when_partial_attributes_set(self) -> None:
        """If only some attributes are set, ``None`` is returned."""

        def handler() -> None:
            pass

        handler._audit_action = "partial.action"  # type: ignore[attr-defined]
        # No _audit_resource set

        metadata = get_audit_metadata(handler)
        assert metadata is None

    def test_returns_metadata_for_sync_function(self) -> None:
        """Sync functions also return metadata."""

        @audit_action("sync.action", "sync", "Sync action")
        def handler() -> str:
            return "done"

        metadata = get_audit_metadata(handler)
        assert metadata == {
            "action": "sync.action",
            "resource": "sync",
            "display": "Sync action",
        }

    def test_display_fallback_to_action(self) -> None:
        """When display is ``None`` in storage, ``action`` is used as fallback."""

        @audit_action("fallback.test", "test")
        async def handler() -> dict[str, str]:
            return {"status": "ok"}

        metadata = get_audit_metadata(handler)
        assert metadata["display"] == "Fallback Test"

    def test_short_action_string(self) -> None:
        """A very short action still produces a valid display name."""

        @audit_action("a.b", "x")
        async def handler() -> dict[str, str]:
            return {"status": "ok"}

        metadata = get_audit_metadata(handler)
        assert metadata["display"] == "A B"

    def test_action_with_only_underscores(self) -> None:
        """Actions with only underscores work correctly."""

        @audit_action("test_action", "resource")
        async def handler() -> dict[str, str]:
            return {"status": "ok"}

        metadata = get_audit_metadata(handler)
        assert metadata["display"] == "Test Action"


@pytest.mark.unit
class TestAuditActionEdgeCases:
    """Edge cases for the audit decorator."""

    def test_empty_action_string(self) -> None:
        """An empty action string produces an empty display name."""

        @audit_action("", "resource")
        async def handler() -> dict[str, str]:
            return {"status": "ok"}

        assert handler._audit_action == ""
        assert handler._audit_display == ""

    def test_empty_resource_string(self) -> None:
        """An empty resource string is allowed."""

        @audit_action("test.action", "")
        async def handler() -> dict[str, str]:
            return {"status": "ok"}

        metadata = get_audit_metadata(handler)
        assert metadata is not None
        assert metadata["resource"] == ""

    def test_multiple_decorators_on_same_function(self) -> None:
        """Only the innermost audit decorator's metadata survives (standard decorator behavior)."""

        def passthrough(func: Callable) -> Callable:
            return func

        @passthrough
        @audit_action("inner.action", "inner", "Inner")
        async def handler() -> dict[str, str]:
            return {"status": "ok"}

        metadata = get_audit_metadata(handler)
        assert metadata is not None
        assert metadata["action"] == "inner.action"
