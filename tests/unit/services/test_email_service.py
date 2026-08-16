"""Unit tests for email_service — async SMTP delivery with Jinja2 templates."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jinja2 import TemplateNotFound

from core.email import EmailConfig
from core.exceptions import ExternalServiceError
from services.email_service import (
    EmailService,
    _mask_email,
    render_email_template,
    render_subject_template,
    render_text_template,
)


class TestMaskEmail:
    """Tests for the private _mask_email() helper."""

    def test_typical_email(self) -> None:
        assert _mask_email("user@example.com") == "u**r@example.com"

    def test_short_local_part(self) -> None:
        assert _mask_email("a@b.co") == "a**@b.co"

    def test_single_char_local(self) -> None:
        assert _mask_email("x@domain.com") == "x**@domain.com"

    def test_no_at_sign(self) -> None:
        """No @ sign still formats but with empty domain."""
        result = _mask_email("invalid")
        # partition("invalid", "@") → ("invalid", "", ""), so
        # local="invalid", domain="" → f"{local[0]}**{local[-1]}@{domain}"
        assert result == "i**d@"


class TestRenderEmailTemplate:
    """Tests for the standalone render_email_template() function."""

    async def test_successful_render(self) -> None:
        """Template renders with provided context."""
        mock_template = MagicMock()
        mock_template.render.return_value = "<html>Hello John</html>"

        with patch("services.email_service._env") as mock_env:
            mock_env.get_template.return_value = mock_template
            result = await render_email_template("welcome", {"name": "John"})

        assert result == "<html>Hello John</html>"
        mock_env.get_template.assert_called_once_with("en/welcome.html.jinja2")
        mock_template.render.assert_called_once_with(locale="en", name="John")

    async def test_locale_missing_falls_back_to_en(self) -> None:
        """A locale without its own template falls back to the en variant."""
        mock_env = MagicMock()
        mock_env.get_template.side_effect = [
            TemplateNotFound("de/welcome.html.jinja2"),
            MagicMock(render=MagicMock(return_value="<html>en fallback</html>")),
        ]

        with patch("services.email_service._env", mock_env):
            result = await render_email_template("welcome", locale="de")

        assert result == "<html>en fallback</html>"
        assert mock_env.get_template.call_args_list[0].args == (
            "de/welcome.html.jinja2",
        )
        assert mock_env.get_template.call_args_list[1].args == (
            "en/welcome.html.jinja2",
        )

    async def test_template_not_found_raises_error(self) -> None:
        """Missing template raises ExternalServiceError."""
        with patch("services.email_service._env") as mock_env:
            mock_env.get_template.side_effect = TemplateNotFound(
                "en/welcome.html.jinja2"
            )

            with pytest.raises(ExternalServiceError, match="Email template.*not found"):
                await render_email_template("nonexistent")

    async def test_empty_context(self) -> None:
        """Calling without context still works (empty render)."""
        mock_template = MagicMock()
        mock_template.render.return_value = "<html></html>"

        with patch("services.email_service._env") as mock_env:
            mock_env.get_template.return_value = mock_template

            result = await render_email_template("empty")
            assert result == "<html></html>"
            mock_template.render.assert_called_once_with(locale="en")


class TestRenderTextTemplate:
    """Tests for the standalone render_text_template() function."""

    async def test_returns_plain_text(self) -> None:
        """Existing .txt.jinja2 template returns rendered text."""
        mock_template = MagicMock()
        mock_template.render.return_value = "Hello in plain text"

        with patch("services.email_service._env") as mock_env:
            mock_env.get_template.return_value = mock_template

            result = await render_text_template("welcome")
            assert result == "Hello in plain text"
            mock_env.get_template.assert_called_once_with("en/welcome.txt.jinja2")

    async def test_missing_text_template_returns_empty(self) -> None:
        """No .txt.jinja2 template returns empty fallback string."""
        with patch("services.email_service._env") as mock_env:
            mock_env.get_template.side_effect = TemplateNotFound(
                "en/welcome.txt.jinja2"
            )

            result = await render_text_template("welcome")
            assert result == ""


class TestRenderSubjectTemplate:
    """Tests for the render_subject_template() function."""

    async def test_renders_subject_line(self) -> None:
        """Subject template renders with context, whitespace stripped."""
        mock_template = MagicMock()
        mock_template.render.return_value = "Welcome {{ name }}\n"

        with patch("services.email_service._env") as mock_env:
            mock_env.get_template.return_value = mock_template

            result = await render_subject_template(
                "welcome", {"name": "John"}, locale="en"
            )
            assert result == "Welcome {{ name }}"
            mock_env.get_template.assert_called_once_with(
                "en/welcome.subject.jinja2"
            )
            mock_template.render.assert_called_once_with(name="John")

    async def test_subject_missing_raises(self) -> None:
        """A missing subject template is loud — never an empty subject."""
        with patch("services.email_service._env") as mock_env:
            mock_env.get_template.side_effect = TemplateNotFound(
                "en/welcome.subject.jinja2"
            )

            with pytest.raises(
                ExternalServiceError, match="subject template.*not found"
            ):
                await render_subject_template("nonexistent")


class TestRealTemplateRendering:
    """Renders the shipped ``prompts/email/en/`` templates — no mocking.

    These assert the actual files the product ships: every email type must
    render non-empty HTML/text/subject, a locale without templates must
    fall back to English, and a missing English template must be loud.
    """

    @pytest.mark.parametrize(
        ("template_name", "context"),
        [
            pytest.param(
                "otp",
                {"code": "483926", "expiry_minutes": 10},
                id="otp",
            ),
            pytest.param(
                "password_changed",
                {"name": "Alice"},
                id="password_changed",
            ),
            pytest.param(
                "invite",
                {
                    "org_name": "Acme Corp",
                    "invitee_name": "Alice",
                    "inviter_name": "Bob",
                    "link": "https://app.openzync.tech/invite/abc123",
                    "expiry_hours": 72,
                },
                id="invite",
            ),
        ],
    )
    async def test_all_three_email_types_render_non_empty(
        self, template_name: str, context: dict[str, object]
    ) -> None:
        """Each shipped email type has working html/txt/subject templates."""
        html = await render_email_template(template_name, context, locale="en")
        text = await render_text_template(template_name, context, locale="en")
        subject = await render_subject_template(template_name, context, locale="en")

        assert html.strip()
        assert text.strip()
        assert subject.strip()

    async def test_locale_without_templates_falls_back_to_en(self) -> None:
        """A locale with no template directory renders the English variant."""
        html = await render_email_template(
            "otp", {"code": "483926", "expiry_minutes": 10}, locale="de"
        )

        assert html.strip()
        assert 'lang="en"' in html  # effective locale injected for the lang attr

    async def test_html_autoescapes_user_controlled_values(self) -> None:
        """User-controlled values are escaped in HTML, untouched in plain text.

        Regression for a shipping blocker: ``select_autoescape`` never fired
        for ``*.html.jinja2`` templates, so ``org_name`` etc. rendered raw
        into HTML emails (XSS via ``<img onerror=...>``).
        """
        payload: dict[str, object] = {
            "org_name": "<img src=x onerror=alert(1)>",
            "invitee_name": "<b>bold</b>",
            "inviter_name": "Bob",
            "link": "https://app.openzync.tech/invite/abc123",
            "expiry_hours": 72,
        }
        html = await render_email_template("invite", payload, locale="en")
        text = await render_text_template("invite", payload, locale="en")

        # HTML body: markup escaped, so it can never execute.
        assert "<img src=x onerror=alert(1)>" not in html
        assert "&lt;img src=x onerror=alert(1)&gt;" in html
        assert "<b>bold</b>" not in html
        assert "&lt;b&gt;bold&lt;/b&gt;" in html

        # Plain text is not HTML — raw values are correct, not double-escaped.
        assert "<img src=x onerror=alert(1)>" in text
        assert "<b>bold</b>" in text

    async def test_missing_en_template_raises(self) -> None:
        """A missing English template is a loud ExternalServiceError — never
        an empty string or a raw template key."""
        with pytest.raises(ExternalServiceError, match="not found"):
            await render_email_template("no_such_email", locale="en")
        with pytest.raises(ExternalServiceError, match="not found"):
            await render_subject_template("no_such_email", locale="en")

    async def test_missing_text_template_returns_empty(self) -> None:
        """No .txt.jinja2 → empty string (the builder HTML-strips as fallback)."""
        assert await render_text_template("no_such_email", locale="en") == ""


class TestEmailService:
    """Tests for EmailService — send_email with mocked SMTP."""

    def _make_config(self) -> EmailConfig:
        return EmailConfig(
            host="smtp.example.com",
            port=587,
            username="user",
            password="pass",
            from_addr="noreply@openzync.tech",
            use_tls=False,
            start_tls=True,
        )

    async def test_send_email_with_html_only(self) -> None:
        """send_email dispatches an HTML-only message successfully."""
        config = self._make_config()
        service = EmailService(config)

        with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
            await service.send_email(
                to="user@example.com",
                subject="Welcome",
                html_body="<h1>Welcome!</h1>",
            )

        mock_send.assert_awaited_once()
        call_kwargs = mock_send.await_args[1]
        assert call_kwargs["hostname"] == "smtp.example.com"
        assert call_kwargs["port"] == 587
        assert call_kwargs["username"] == "user"
        assert call_kwargs["password"] == "pass"

    async def test_send_email_with_html_and_plain_text(self) -> None:
        """send_email with both HTML and plain text works."""
        config = self._make_config()
        service = EmailService(config)

        with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
            await service.send_email(
                to="user@example.com",
                subject="Welcome",
                html_body="<h1>Welcome!</h1>",
                text_body="Welcome!",
            )

        mock_send.assert_awaited_once()

    async def test_smtp_error_raises_external_service_error(self) -> None:
        """SMTP failure raises ExternalServiceError — not silently swallowed."""
        config = self._make_config()
        service = EmailService(config)

        with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
            mock_send.side_effect = ConnectionRefusedError("SMTP refused connection")

            with pytest.raises(ExternalServiceError, match="Failed to send email"):
                await service.send_email(
                    to="user@example.com",
                    subject="Welcome",
                    html_body="<h1>Welcome!</h1>",
                )

    async def test_send_email_no_auth(self) -> None:
        """When username/password are empty, None is passed to aiosmtplib."""
        config = EmailConfig(
            host="smtp.example.com",
            port=25,
            username="",
            password="",
            from_addr="noreply@openzync.tech",
            use_tls=False,
            start_tls=False,
        )
        service = EmailService(config)

        with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
            await service.send_email(
                to="user@example.com",
                subject="Test",
                html_body="<p>Test</p>",
            )

        call_kwargs = mock_send.await_args[1]
        assert call_kwargs["username"] is None
        assert call_kwargs["password"] is None
