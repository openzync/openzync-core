"""Unit tests for dependencies/email.py — EmailService / OtpService factories."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request

pytestmark = pytest.mark.unit


class TestGetEmailService:
    """get_email_service: constructs EmailService from settings."""

    def test_creates_email_service_with_config(self) -> None:
        """Returns EmailService initialised with EmailConfig from settings."""
        # EmailService is imported lazily inside get_email_service() via
        #   from services.email_service import EmailService as _EmailService
        # so we patch services.email_service.EmailService (the real module).
        with (
            patch("dependencies.email.EmailConfig") as mock_email_config,
            patch("services.email_service.EmailService") as mock_email_service,
        ):
            mock_email_config.from_settings.return_value = "fake_config"
            mock_email_service.return_value = "email_service_instance"

            from dependencies.email import get_email_service

            result = get_email_service()

            mock_email_config.from_settings.assert_called_once()
            mock_email_service.assert_called_once_with("fake_config")
            assert result == "email_service_instance"

    def test_email_config_reads_from_settings(self) -> None:
        """EmailConfig.from_settings reads the correct settings fields."""
        from core.config import get_settings
        from core.email import EmailConfig

        settings = get_settings()
        config = EmailConfig.from_settings(settings)

        assert config.HOST == settings.SMTP_HOST
        assert config.PORT == settings.SMTP_PORT
        assert config.USERNAME == settings.SMTP_USERNAME
        assert config.FROM_ADDR == settings.SMTP_FROM_ADDR


class TestGetOtpService:
    """get_otp_service: constructs OtpService with Redis + EmailService."""

    @pytest.mark.asyncio
    async def test_creates_otp_service_with_deps(self) -> None:
        """Returns OtpService wired to redis and email_service."""
        # OtpService is imported lazily inside get_otp_service() via
        #   from services.otp_service import OtpService as _OtpService
        # so we patch services.otp_service.OtpService.
        with patch("services.otp_service.OtpService") as mock_otp_service:
            mock_otp_service.return_value = "otp_service_instance"

            from dependencies.email import get_otp_service

            request = MagicMock(spec=Request)
            redis = AsyncMock()
            email_service = MagicMock()

            result = await get_otp_service(request, redis, email_service)

            mock_otp_service.assert_called_once_with(
                redis=redis, email_service=email_service
            )
            assert result == "otp_service_instance"
