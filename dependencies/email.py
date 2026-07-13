"""Email and OTP service dependency factories for FastAPI route injection.

Provides ``Depends``-compatible factory functions that construct the
``EmailService`` and ``OtpService`` with their required dependencies.

Usage in a router::

    from fastapi import APIRouter, Depends
    from dependencies.email import get_email_service, get_otp_service
    from services.email_service import EmailService
    from services.otp_service import OtpService

    router = APIRouter()

    @router.post("/send-otp")
    async def send_otp(
        otp_service: OtpService = Depends(get_otp_service),
    ):
        ...
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import Depends, Request

from core.config import get_settings
from core.email import EmailConfig
from core.redis import get_redis

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from services.email_service import EmailService
    from services.otp_service import OtpService

logger = logging.getLogger(__name__)


def get_email_service() -> EmailService:
    """Dependency that yields a pre-configured ``EmailService``.

    The ``EmailService`` is stateless (per-message SMTP connections) so it
    can be created fresh each time.  Configuration is read from the runtime
    ``Settings`` singleton populated from OpenBao.

    Returns:
        An initialised ``EmailService`` instance.
    """
    from services.email_service import EmailService as _EmailService

    config = EmailConfig.from_settings(get_settings())
    return _EmailService(config)


async def get_otp_service(
    request: Request,
    redis: Redis = Depends(get_redis),  # noqa: B008
    email_service: EmailService = Depends(get_email_service),  # noqa: B008
) -> OtpService:
    """Dependency that yields an initialised ``OtpService``.

    Reads the Redis client from the dependency chain and wires in the
    ``EmailService`` for OTP delivery.

    Args:
        request: Incoming HTTP request (for app.state access).
        redis: Async Redis client from dependency injection.
        email_service: Stateless email service for sending OTP emails.

    Returns:
        An initialised ``OtpService`` instance.

    Raises:
        RuntimeError: If Redis is not available on ``app.state``.
    """
    from services.otp_service import OtpService as _OtpService

    return _OtpService(redis=redis, email_service=email_service)
