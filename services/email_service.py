"""Email service — async transactional email delivery via SMTP.

Uses ``aiosmtplib`` for non-blocking SMTP communication, and Jinja2 to
render email body templates stored in ``prompts/email/{locale}/``.

Template layout — one directory per locale, three files per email::

    prompts/email/en/otp.html.jinja2        # HTML body
    prompts/email/en/otp.txt.jinja2         # plain-text body
    prompts/email/en/otp.subject.jinja2     # subject line
    prompts/email/de/otp.html.jinja2        # German variant (when shipped)

Rendering is locale-aware with a hard ``en`` fallback: a missing locale
directory falls back to English, never to a raw key or an empty body.
Missing English templates are a loud ``ExternalServiceError`` — a broken
template must never silently produce an empty email.

Usage::

    from core.config import get_settings
    from core.email import EmailConfig
    from services.email_service import EmailService, render_email_template

    config = EmailConfig.from_settings(get_settings())
    service = EmailService(config)
    html = await render_email_template(
        "otp", {"code": "123456", "expiry_minutes": 10}, locale="de",
    )
    await service.send_email("user@example.com", "Your OTP Code", html)
"""

from __future__ import annotations

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, Template, select_autoescape

from core.email import EmailConfig, build_email_message
from core.exceptions import ExternalServiceError
from core.locales import DEFAULT_LOCALE

logger = logging.getLogger(__name__)

# ── Jinja2 template loader for email templates ──────────────────────────────

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "prompts" / "email"
"""Directory containing per-locale Jinja2 email template directories."""

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(),
)


def _load_template(locale: str, template_name: str, kind: str) -> tuple[Template, str]:
    """Load ``{locale}/{name}.{kind}.jinja2``, falling back to English.

    Args:
        locale: Requested BCP-47 locale tag (``core.locales.DEFAULT_LOCALE``
            when unknown).
        template_name: Template basename (e.g. ``"otp"``).
        kind: Template variant — ``html``, ``txt``, or ``subject``.

    Returns:
        A ``(template, effective_locale)`` tuple — ``effective_locale`` is
        the locale actually used, so templates can render the correct
        ``lang`` attribute even when falling back.

    Raises:
        TemplateNotFound: If neither the requested locale nor English have
            the template.
    """
    from jinja2 import TemplateNotFound

    candidates = [locale, DEFAULT_LOCALE] if locale != DEFAULT_LOCALE else [locale]
    last_error: Exception | None = None
    for cand in candidates:
        try:
            return _env.get_template(f"{cand}/{template_name}.{kind}.jinja2"), cand
        except Exception as exc:  # loader failures fall back — en is the floor
            last_error = exc
    raise TemplateNotFound(template_name) from last_error


async def render_email_template(
    template_name: str,
    context: dict[str, object] | None = None,
    locale: str = DEFAULT_LOCALE,
) -> str:
    """Render the HTML body of an email for the given locale.

    Args:
        template_name: Template basename (e.g. ``"otp"`` loads
            ``prompts/email/{locale}/otp.html.jinja2``).
        context: Variables to inject into the template (the effective
            locale is injected as ``locale`` for the ``lang`` attribute).
        locale: Recipient's BCP-47 locale tag — falls back to English when
            no locale-specific template exists.

    Returns:
        Rendered HTML string.

    Raises:
        ExternalServiceError: If neither the requested locale nor English
            has the template file.
    """
    try:
        template, effective_locale = _load_template(locale, template_name, "html")
    except Exception as exc:
        raise ExternalServiceError(
            f"Email template '{template_name}' not found for locale "
            f"'{locale}' (or '{DEFAULT_LOCALE}' fallback): {exc}",
        ) from exc

    return template.render(locale=effective_locale, **(context or {}))


async def render_text_template(
    template_name: str,
    context: dict[str, object] | None = None,
    locale: str = DEFAULT_LOCALE,
) -> str:
    """Render the plain-text variant of an email for the given locale.

    Falls back to ``{locale}/{name}.txt.jinja2`` → ``en/{name}.txt.jinja2``,
    or — if neither exists — to an empty string (the ``EmailMessage``
    builder strips the HTML body as a plain-text fallback).

    Args:
        template_name: Template basename (e.g. ``"otp"``).
        context: Template variables.
        locale: Recipient's BCP-47 locale tag.

    Returns:
        Rendered plain-text string, or ``""`` when no text template exists.
    """
    try:
        template, _effective_locale = _load_template(locale, template_name, "txt")
    except Exception:
        # No plain-text template — the EmailMessage builder will strip HTML.
        return ""
    return template.render(**(context or {}))


async def render_subject_template(
    template_name: str,
    context: dict[str, object] | None = None,
    locale: str = DEFAULT_LOCALE,
) -> str:
    """Render the subject line of an email for the given locale.

    The subject is a first-class template (``{locale}/{name}.subject.jinja2``)
    so subject lines localize together with the bodies — never a hardcoded
    English string at the call site.

    Args:
        template_name: Template basename (e.g. ``"otp"``).
        context: Template variables (e.g. ``org_name`` for invites).
        locale: Recipient's BCP-47 locale tag — falls back to English.

    Returns:
        The rendered subject line.

    Raises:
        ExternalServiceError: If neither the requested locale nor English
            has the subject template.
    """
    try:
        template, _effective_locale = _load_template(locale, template_name, "subject")
    except Exception as exc:
        raise ExternalServiceError(
            f"Email subject template '{template_name}' not found for locale "
            f"'{locale}' (or '{DEFAULT_LOCALE}' fallback): {exc}",
        ) from exc

    return template.render(**(context or {})).strip()


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
