"""Email sending workers — verification emails, password resets, etc.

All email jobs are ARQ tasks that use ``aiosmtplib`` to send via SMTP.
They are self-contained — they read SMTP config from environment at runtime
and do not require a DB session.

Task list:
- ``send_verification_email`` — Send a verification link after signup.
"""

from __future__ import annotations

import logging
from typing import Any
from pathlib import Path

import aiosmtplib
from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

# Jinja2 environment for rendering email templates.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "worker" / "prompts"
_JINJA_ENV = Environment(loader=FileSystemLoader(str(_PROMPTS_DIR)))


def _render_template(template_name: str, **kwargs: Any) -> str:
    """Render a Jinja2 email template.

    Args:
        template_name: Name of the template file (e.g. ``email_verification.jinja2``).
        **kwargs: Variables to inject into the template.

    Returns:
        Rendered email body (plain text).
    """
    template = _JINJA_ENV.get_template(template_name)
    return template.render(**kwargs)


def _get_subject(rendered_body: str) -> str:
    """Extract the email subject from the rendered body.

    The subject is the first line (after ``Subject: `` prefix).
    Returns the first line stripped of the Subject prefix.
    """
    first_line = rendered_body.split("\n", 1)[0]
    if first_line.startswith("Subject: "):
        return first_line[len("Subject: "):]
    return "No subject"


def _get_body(rendered_body: str) -> str:
    """Extract the email body (everything after the subject line)."""
    parts = rendered_body.split("\n", 1)
    return parts[1].strip() if len(parts) > 1 else ""


async def send_email(
    to_email: str,
    subject: str,
    body: str,
) -> None:
    """Send an email via SMTP using ``aiosmtplib``.

    Args:
        to_email: Recipient email address.
        subject: Email subject line.
        body: Email body (plain text).

    Raises:
        RuntimeError: If SMTP is not configured (host empty).
        aiosmtplib.SMTPException: If sending fails.
    """
    from core.config import settings

    host = settings.SMTP_HOST
    if not host:
        logger.warning("email.smtp_not_configured", extra={"to": to_email})
        return

    message = (
        f"From: {settings.SMTP_FROM_EMAIL}\r\n"
        f"To: {to_email}\r\n"
        f"Subject: {subject}\r\n"
        f"\r\n"
        f"{body}"
    )

    try:
        await aiosmtplib.send(
            message,
            hostname=host,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USERNAME or None,
            password=settings.SMTP_PASSWORD or None,
            start_tls=True,
        )
        logger.info("email.sent", extra={"to": to_email, "subject": subject})
    except Exception as exc:
        logger.error(
            "email.send_failed",
            extra={"to": to_email, "subject": subject, "error": str(exc)},
        )
        raise


async def send_verification_email(
    ctx: dict[str, Any],
    *,
    email: str,
    token: str,
    org_name: str,
) -> None:
    """ARQ task — send an email verification link.

    Args:
        ctx: ARQ context (unused — required by ARQ contract).
        email: Recipient email address.
        token: Raw verification token (to be included in the link).
        org_name: Organization name for the email greeting.
    """
    from core.config import settings

    base_url = settings.APP_BASE_URL.rstrip("/")
    verification_link = f"{base_url}/v1/auth/verify?token={token}"

    rendered = _render_template(
        "email_verification.jinja2",
        verification_link=verification_link,
        org_name=org_name,
        email=email,
    )
    subject = _get_subject(rendered)
    body = _get_body(rendered)

    await send_email(to_email=email, subject=subject, body=body)
