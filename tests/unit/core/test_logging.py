"""Unit tests for structured logging setup (core/logging.py).

Covers:
  - ``setup_logging``: dev vs. prod renderer, log-level parsing, idempotency.
  - ``add_pii_redaction``: redacts known sensitive field names (whole-word match).
  - ``bind_request_context`` + ``_add_context_from_vars``: context-var propagation.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.mark.unit
class TestLoggingSetup:
    """setup_logging() — renderer selection, processor chain, idempotency."""

    # ── Renderer selection ─────────────────────────────────────────────────

    def test_dev_uses_console_renderer(self) -> None:
        """Development environment selects ConsoleRenderer."""
        from core.logging import setup_logging

        with patch("structlog.configure") as mock_configure:
            setup_logging("development", "DEBUG")
            call_kwargs = mock_configure.call_args.kwargs
            processors = call_kwargs["processors"]
            assert any("ConsoleRenderer" in str(p) for p in processors)

    def test_prod_uses_json_renderer(self) -> None:
        """Production environment selects JSONRenderer."""
        from core.logging import setup_logging

        with patch("structlog.configure") as mock_configure:
            setup_logging("production", "INFO")
            call_kwargs = mock_configure.call_args.kwargs
            processors = call_kwargs["processors"]
            assert any("JSONRenderer" in str(p) for p in processors)

    def test_staging_uses_json_renderer(self) -> None:
        """Staging environment also selects JSONRenderer."""
        from core.logging import setup_logging

        with patch("structlog.configure") as mock_configure:
            setup_logging("staging", "INFO")
            call_kwargs = mock_configure.call_args.kwargs
            processors = call_kwargs["processors"]
            assert any("JSONRenderer" in str(p) for p in processors)

    def test_unknown_env_defaults_to_console(self) -> None:
        """Unknown environment falls back to ConsoleRenderer."""
        from core.logging import setup_logging

        with patch("structlog.configure") as mock_configure:
            setup_logging("ci", "WARNING")
            call_kwargs = mock_configure.call_args.kwargs
            processors = call_kwargs["processors"]
            assert any("ConsoleRenderer" in str(p) for p in processors)

    # ── Processor chain ────────────────────────────────────────────────────

    def test_processor_chain_includes_pii_redaction(self) -> None:
        """PII redaction processor is in the configured chain."""
        from core.logging import add_pii_redaction, setup_logging

        with patch("structlog.configure") as mock_configure:
            setup_logging("development", "DEBUG")
            processors = mock_configure.call_args.kwargs["processors"]
            assert add_pii_redaction in processors

    def test_processor_chain_includes_context_injection(self) -> None:
        """Context-var injection processor is in the chain."""
        from core.logging import _add_context_from_vars, setup_logging

        with patch("structlog.configure") as mock_configure:
            setup_logging("development", "DEBUG")
            processors = mock_configure.call_args.kwargs["processors"]
            assert _add_context_from_vars in processors

    def test_processor_chain_has_timestamper(self) -> None:
        """TimeStamper (ISO, UTC) is configured."""
        from core.logging import setup_logging

        with patch("structlog.configure") as mock_configure:
            setup_logging("development", "DEBUG")
            processors = mock_configure.call_args.kwargs["processors"]
            timestamps = [
                p for p in processors if "TimeStamper" in type(p).__name__
            ]
            assert len(timestamps) == 1

    def test_structlog_configure_kwargs(self) -> None:
        """Verify the full structlog.configure() call signature."""
        from core.logging import setup_logging

        with patch("structlog.configure") as mock_configure:
            setup_logging("development", "DEBUG")
            kwargs = mock_configure.call_args.kwargs
            assert kwargs["context_class"] is dict
            assert kwargs["cache_logger_on_first_use"] is True
            assert "logger_factory" in kwargs
            assert "wrapper_class" in kwargs

    # ── Log level ──────────────────────────────────────────────────────────

    def test_log_level_passed_to_basic_config(self) -> None:
        """log_level string is passed through to logging.basicConfig."""
        from core.logging import setup_logging

        with (
            patch("structlog.configure"),
            patch("logging.basicConfig") as mock_basic_config,
        ):
            setup_logging("development", "WARNING")
            mock_basic_config.assert_called_once_with(
                format="%(message)s", level="WARNING"
            )

    def test_invalid_log_level_fallback(self) -> None:
        """Invalid log level string does not raise — Python logging treats
        unknown levels as a string and still configures."""
        from core.logging import setup_logging

        with (
            patch("structlog.configure"),
            patch("logging.basicConfig") as mock_basic_config,
        ):
            # Should not raise; logging.basicConfig validates at emit time
            setup_logging("development", "VERBOSE")
            mock_basic_config.assert_called_once_with(
                format="%(message)s", level="VERBOSE"
            )

    # ── Idempotency ────────────────────────────────────────────────────────

    def test_setup_logging_can_be_called_twice(self) -> None:
        """Calling setup_logging twice does not raise."""
        from core.logging import setup_logging

        with (
            patch("structlog.configure"),
            patch("logging.basicConfig"),
        ):
            setup_logging("development", "DEBUG")
            setup_logging("production", "INFO")  # second call — should not raise

    # ── CaptureWarnings ────────────────────────────────────────────────────

    def test_logging_capture_warnings_enabled(self) -> None:
        """logging.captureWarnings(True) is called."""
        from core.logging import setup_logging

        with (
            patch("structlog.configure"),
            patch("logging.captureWarnings") as mock_capture,
        ):
            setup_logging("development", "DEBUG")
            mock_capture.assert_called_once_with(True)


@pytest.mark.unit
class TestPiiRedaction:
    """add_pii_redaction() — sensitive field detection and redaction."""

    def test_redacts_password_field(self) -> None:
        """Field named 'password' is redacted."""
        from core.logging import add_pii_redaction

        event = {"password": "s3cret!", "user": "alice"}
        result = add_pii_redaction(None, None, event)
        assert result["password"] == "***REDACTED***"
        assert result["user"] == "alice"

    def test_redacts_token_fields(self) -> None:
        """Fields containing 'token' are redacted (access_token, refresh_token)."""
        from core.logging import add_pii_redaction

        event = {"access_token": "abc.def", "refresh_token": "ghi.jkl", "scope": "read"}
        result = add_pii_redaction(None, None, event)
        assert result["access_token"] == "***REDACTED***"
        assert result["refresh_token"] == "***REDACTED***"
        assert result["scope"] == "read"

    def test_redacts_api_key_fields(self) -> None:
        """Fields 'api_key' and 'api_key_name' are redacted."""
        from core.logging import add_pii_redaction

        event = {"api_key": "oz_live_xxx", "api_key_name": "prod-key"}
        result = add_pii_redaction(None, None, event)
        assert result["api_key"] == "***REDACTED***"
        assert result["api_key_name"] == "***REDACTED***"

    def test_redacts_secret_and_auth_fields(self) -> None:
        """Fields named 'client_secret' and 'authorization' are redacted.
        Note: 'auth_provider' is NOT redacted because '_' is a word character
        so \\bauth\\b does not match at the underscore boundary."""
        from core.logging import add_pii_redaction

        event = {
            "client_secret": "hunter2",
            "authorization": "Bearer xxx",
            "auth_provider": "google",
        }
        result = add_pii_redaction(None, None, event)
        assert result["client_secret"] == "***REDACTED***"
        assert result["authorization"] == "***REDACTED***"
        # 'auth_provider' does NOT match because \b requires a word/non-word
        # boundary and '_' is a word character — "auth" is not isolated.
        assert result["auth_provider"] == "google"

    def test_redacts_private_key_field(self) -> None:
        """Field 'private_key' is redacted."""
        from core.logging import add_pii_redaction

        event = {"private_key": "-----BEGIN RSA PRIVATE KEY-----"}
        result = add_pii_redaction(None, None, event)
        assert result["private_key"] == "***REDACTED***"

    def test_keeps_safe_fields_unchanged(self) -> None:
        """Fields not matching sensitive patterns are left intact."""
        from core.logging import add_pii_redaction

        event = {
            "request_id": "r-123",
            "user_id": "u-456",
            "org_id": "o-789",
            "name": "Alice",
            "email": "alice@example.com",  # 'email' is NOT in SENSITIVE_KEYS
        }
        result = add_pii_redaction(None, None, event)
        assert result == event

    def test_sensitive_pattern_is_case_insensitive(self) -> None:
        """Sensitive field detection is case-insensitive."""
        from core.logging import add_pii_redaction

        event = {"Password": "case-test", "TOKEN": "case-test-2"}
        result = add_pii_redaction(None, None, event)
        assert result["Password"] == "***REDACTED***"
        assert result["TOKEN"] == "***REDACTED***"

    def test_word_boundary_on_underscore(self) -> None:
        """'_' is a word character so \\b does NOT break at underscore boundaries.
        'user_passwords' does NOT match because 'password' is followed by 's'
        (a word char) — no word boundary."""
        from core.logging import add_pii_redaction

        # 'password' is surrounded by word chars → no match
        event = {"user_passwords": "abc123", "safe": "ok"}
        result = add_pii_redaction(None, None, event)
        assert result["user_passwords"] == "abc123"  # NOT redacted
        assert result["safe"] == "ok"

    def test_underscore_is_word_character_no_boundary(self) -> None:
        """'_' is a word character — no \\b boundary when adjacent to
        underscores, so 'user_password' is NOT redacted (but 'user-password' is).
        """
        from core.logging import add_pii_redaction

        event_under = {"user_password": "hunter2"}
        result = add_pii_redaction(None, None, event_under)
        assert result["user_password"] == "hunter2"  # NOT redacted

        event_hyphen = {"user-password": "hunter2"}
        result = add_pii_redaction(None, None, event_hyphen)
        assert result["user-password"] == "***REDACTED***"  # hyphen is word boundary

    def test_sensitive_word_isolated_by_nonword_chars(self) -> None:
        """A sensitive word isolated by non-word characters or string start/end
        IS redacted."""
        from core.logging import add_pii_redaction

        event = {"password!": "abc", "my.token": "xyz", "API_KEY!": "key123"}
        result = add_pii_redaction(None, None, event)
        assert result["password!"] == "***REDACTED***"
        assert result["my.token"] == "***REDACTED***"
        assert result["API_KEY!"] == "***REDACTED***"

    def test_handles_empty_event_dict(self) -> None:
        """Empty event dict returns unchanged."""
        from core.logging import add_pii_redaction

        result = add_pii_redaction(None, None, {})
        assert result == {}


@pytest.mark.unit
class TestRequestContext:
    """bind_request_context() and _add_context_from_vars()."""

    @staticmethod
    def _reset_context_vars() -> None:
        """Clear all three context vars to their default empty values.

        ContextVars persist across tests within the same thread/context,
        so we must reset them to avoid state leaking between test methods.
        """
        from core.logging import _org_id, _request_id, _user_id

        _request_id.set("")
        _org_id.set("")
        _user_id.set("")

    def test_binds_request_id(self) -> None:
        """request_id is set and appears in event dict."""
        self._reset_context_vars()
        from core.logging import bind_request_context, _add_context_from_vars

        bind_request_context(request_id="req-001")
        event_dict = _add_context_from_vars(None, None, {})
        assert event_dict["request_id"] == "req-001"
        """request_id is set and appears in event dict."""
        from core.logging import bind_request_context, _add_context_from_vars

        bind_request_context(request_id="req-001")
        event_dict = _add_context_from_vars(None, None, {})
        assert event_dict["request_id"] == "req-001"

    def test_binds_org_id(self) -> None:
        """org_id is set when provided."""
        self._reset_context_vars()
        from core.logging import bind_request_context, _add_context_from_vars

        bind_request_context(request_id="req-002", org_id="org-42")
        event_dict = _add_context_from_vars(None, None, {})
        assert event_dict["org_id"] == "org-42"
        assert event_dict["request_id"] == "req-002"

    def test_binds_user_id(self) -> None:
        """user_id is set when provided."""
        self._reset_context_vars()
        from core.logging import bind_request_context, _add_context_from_vars

        bind_request_context(request_id="req-003", user_id="usr-7")
        event_dict = _add_context_from_vars(None, None, {})
        assert event_dict["user_id"] == "usr-7"
        assert event_dict["request_id"] == "req-003"

    def test_binds_all_context_vars(self) -> None:
        """All three context vars are set when all are provided."""
        self._reset_context_vars()
        from core.logging import bind_request_context, _add_context_from_vars

        bind_request_context(request_id="req-004", org_id="o-1", user_id="u-1")
        event_dict = _add_context_from_vars(None, None, {})
        assert event_dict == {"request_id": "req-004", "org_id": "o-1", "user_id": "u-1"}

    def test_skips_empty_optional_fields(self) -> None:
        """Optional fields not provided are omitted from event dict."""
        self._reset_context_vars()
        from core.logging import bind_request_context, _add_context_from_vars

        bind_request_context(request_id="req-005")
        event_dict = _add_context_from_vars(None, None, {})
        assert "org_id" not in event_dict
        assert "user_id" not in event_dict

    def test_context_var_isolation(self) -> None:
        """Context vars are per-context — second bind overrides first."""
        self._reset_context_vars()
        from core.logging import bind_request_context, _add_context_from_vars

        bind_request_context(request_id="first")
        bind_request_context(request_id="second")
        event_dict = _add_context_from_vars(None, None, {})
        assert event_dict["request_id"] == "second"

    def test_empty_request_id_not_injected(self) -> None:
        """Default empty request_id is not added to event dict."""
        self._reset_context_vars()
        from core.logging import _add_context_from_vars

        event_dict = _add_context_from_vars(None, None, {})
        assert "request_id" not in event_dict
