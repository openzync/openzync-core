"""Email service — async transactional email delivery via SMTP.

Uses ``aiosmtplib`` for non-blocking SMTP communication, and Jinja2 to
render email body templates stored in ``prompts/email/``.

Dependencies:
    - ``aiosmtplib`` — async SMTP client (added to requirements.txt).
    - ``Jinja2`` — template rendering (already a project dependency).

Usage::

    from core.config import get_settings
    from core.email import EmailConfig
    from services.email_service import EmailService, render_email_template

    config = EmailConfig.from_settings(get_settings())
    service = EmailService(config)
    html = await render_email_template("otp", {"code": "123456", "expiry_minutes": 10})
    await service.send_email("user@example.com", "Your OTP Code", html)
"""

from __future__ import annotations

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.email import EmailConfig, build_email_message
from core.exceptions import ExternalServiceError

logger = logging.getLogger(__name__)

# ── Jinja2 template loader for email templates ──────────────────────────────

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "prompts" / "email"
"""Directory containing Jinja2 email templates (*.jinja2)."""

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(),
)


async def render_email_template(
    template_name: str,
    context: dict[str, object] | None = None,
) -> str:
    """Render a Jinja2 email template with the given context.

    Args:
        template_name: Template filename without extension (e.g. ``"otp"``
            loads ``prompts/email/otp.html.jinja2``).
        context: Variables to inject into the template.

    Returns:
        Rendered HTML string.

    Raises:
        ExternalServiceError: If the template file is missing or invalid.
    """
    filename = f"{template_name}.html.jinja2"
    try:
        template = _env.get_template(filename)
    except Exception as exc:
        raise ExternalServiceError(
            f"Email template '{filename}' not found or invalid: {exc}",
        ) from exc

    return template.render(**(context or {}))


async def render_text_template(
    template_name: str,
    context: dict[str, object] | None = None,
) -> str:
    """Render the plain-text variant of an email template.

    Falls back to ``{name}.txt.jinja2`` or, if missing, to a stripped
    HTML version.

    Args:
        template_name: Template basename (e.g. ``"otp"``).
        context: Template variables.

    Returns:
        Rendered plain-text string.
    """
    filename = f"{template_name}.txt.jinja2"
    try:
        template = _env.get_template(filename)
    except Exception:
        # No plain-text template — the EmailMessage builder will strip HTML.
        return ""
    return template.render(**(context or {}))


# ── EmailService ────────────────────────────────────────────────────────────


class EmailService:
    """Async SMTP email delivery service.

    Creates a fresh SMTP connection per message (KISS — transactional
    email volume is low).  Connection pooling can be added later if
    throughput requirements increase.

    Args:
        config: SMTP configuration from ``EmailConfig``.
    """

    __slots__ = ("_config",)

    def __init__(self, config: EmailConfig) -> None:
        self._config = config

    async def send_email(
        self,
        to: str,
        subject: str,
        html_body: str,
        text_body: str | None = None,
    ) -> None:
        """Send an email via SMTP.

        Creates a fresh SMTP connection, authenticates (if credentials are
        configured), sends the message, and quits.

        Args:
            to: Recipient email address.
            subject: Email subject line.
            html_body: Rendered HTML body.
            text_body: Optional plain-text fallback.  If ``None``, the
                ``EmailMessage`` builder will auto-strip the HTML.

        Raises:
            ExternalServiceError: If the SMTP server cannot be reached or
                the message cannot be sent.
        """
        import aiosmtplib

        msg = build_email_message(
            to=to,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            from_addr=self._config.FROM_ADDR,
        )

        logger.info(
            "email.sending",
            extra={
                "to": _mask_email(to),
                "subject": subject,
                "smtp_host": self._config.HOST,
            },
        )

        try:
            await aiosmtplib.send(
                msg,
                hostname=self._config.HOST,
                port=self._config.PORT,
                username=self._config.USERNAME or None,
                password=self._config.PASSWORD or None,
                use_tls=self._config.USE_TLS,
                start_tls=self._config.START_TLS,
                timeout=30,
            )
        except Exception as exc:
            logger.error(
                "email.send_failed",
                extra={
                    "to": _mask_email(to),
                    "smtp_host": self._config.HOST,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                f"Failed to send email to {_mask_email(to)}: {exc}",
            ) from exc

        logger.info(
            "email.sent",
            extra={"to": _mask_email(to), "subject": subject},
        )


def _mask_email(email: str) -> str:
    """Mask an email address for logging (e.g. ``u**@example.com``).

    Args:
        email: The full email address.

    Returns:
        Masked version safe for logs.
    """
    local, _, domain = email.partition("@")
    if len(local) <= 1:
        return f"{local[0]}**@{domain}" if local else email
    return f"{local[0]}**{local[-1]}@{domain}"
