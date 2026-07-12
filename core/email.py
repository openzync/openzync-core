"""Email configuration — SMTP settings from runtime Settings.

SMTP host, port, credentials, and sending address are read from the
:class:`core.config.Settings` singleton, which is populated from OpenBao
at startup.  This module provides the :class:`EmailConfig` typed config
object and a helper to build :class:`email.message.EmailMessage` instances.

Usage::

    from core.config import get_settings
    from core.email import EmailConfig, build_email_message

    config = EmailConfig.from_settings(get_settings())
    msg = build_email_message(
        to="user@example.com",
        subject="Your OTP code",
        html_body="<p>Your code is 123456</p>",
        text_body="Your code is 123456",
        from_addr=config.FROM_ADDR,
    )
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.config import Settings


class EmailConfig:
    """Typed SMTP configuration extracted from runtime Settings.

    Attributes:
        HOST: SMTP server hostname.
        PORT: SMTP server port.
        USERNAME: SMTP username (empty string = no auth).
        PASSWORD: SMTP password (empty string = no auth).
        FROM_ADDR: ``From:`` address for outgoing emails.
        USE_TLS: Use implicit TLS (SMTPS) on connect.
        START_TLS: Use STARTTLS to upgrade to TLS after connect.
    """

    __slots__ = (
        "HOST",
        "PORT",
        "USERNAME",
        "PASSWORD",
        "FROM_ADDR",
        "USE_TLS",
        "START_TLS",
    )

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        from_addr: str,
        use_tls: bool,
        start_tls: bool,
    ) -> None:
        self.HOST = host
        self.PORT = port
        self.USERNAME = username
        self.PASSWORD = password
        self.FROM_ADDR = from_addr
        self.USE_TLS = use_tls
        self.START_TLS = start_tls

    @classmethod
    def from_settings(cls, settings: Settings) -> EmailConfig:
        """Build an ``EmailConfig`` from the runtime ``Settings`` singleton.

        Args:
            settings: The initialised ``Settings`` instance.

        Returns:
            A new ``EmailConfig`` with values from ``settings``.
        """
        return cls(
            host=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USERNAME,
            password=settings.SMTP_PASSWORD,
            from_addr=settings.SMTP_FROM_ADDR,
            use_tls=settings.SMTP_USE_TLS,
            start_tls=settings.SMTP_START_TLS,
        )


def build_email_message(
    to: str,
    subject: str,
    html_body: str,
    text_body: str | None = None,
    from_addr: str = "noreply@openzync.tech",
) -> EmailMessage:
    """Build a :class:`EmailMessage` with both HTML and plain-text parts.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        html_body: HTML body content.
        text_body: Optional plain-text fallback.  If ``None``, a crude
            HTML-stripped version of ``html_body`` is used.
        from_addr: Sender address.

    Returns:
        A fully-formed :class:`EmailMessage` ready for ``aiosmtplib``.
    """
    import re

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject

    # Plain-text fallback
    if text_body is None:
        text_body = re.sub(r"<[^>]+>", "", html_body)
        text_body = re.sub(r"\n\s*\n", "\n", text_body).strip()

    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    return msg
