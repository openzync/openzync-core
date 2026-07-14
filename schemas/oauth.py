"""Pydantic schemas for OAuth (Google/GitHub) authentication.

Covers OAuth login initiation, callback handling, account linking,
and token delivery. All schemas are request/response models only
— never stored or logged in plaintext.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OAuthInitResponse(BaseModel):
    """Response returned when initiating an OAuth login/link flow.

    Attributes:
        redirect_url: The fully constructed URL to redirect the browser to
            (e.g. ``https://accounts.google.com/o/oauth2/v2/auth?...``).
        state_token: The OAuth state token stored in Redis for CSRF
            protection. The client does not need to use this directly —
            it is embedded in the ``redirect_url`` as the ``state`` param.
    """

    redirect_url: str = Field(
        ...,
        description="Full OAuth provider authorization URL to redirect the browser to.",
        examples=["https://accounts.google.com/o/oauth2/v2/auth?client_id=..."],
    )
    state_token: str = Field(
        ...,
        description="OAuth state token (embedded in redirect_url, exposed for debugging).",
    )


class OAuthCallbackTokenResponse(BaseModel):
    """Token response delivered via URL query params after OAuth callback.

    When the OAuth callback succeeds, the backend redirects the browser to
    the frontend URL with these fields as query parameters. The frontend
    extracts them and stores them as it would for a normal login.

    Note: This is NOT an API response schema — it describes URL parameters.
    """

    access_token: str = Field(
        ..., description="JWT access token for API authentication."
    )
    refresh_token: str = Field(
        ..., description="Opaque refresh token for session renewal."
    )
    expires_in: int = Field(
        ..., description="Access token TTL in seconds.", examples=[1800]
    )
    token_type: str = Field(
        default="Bearer",
        description="Token type — always ``'Bearer'``.",
    )


class OAuthLinkRequest(BaseModel):
    """Request body for ``POST /v1/auth/oauth/{provider}/link``.

    Initiates the OAuth account-linking flow for an already-authenticated
    dashboard user. The frontend receives a ``redirect_url`` and redirects
    the browser to the OAuth provider.
    """

    provider: str = Field(
        ...,
        pattern=r"^(google|github)$",
        description="OAuth provider name: ``'google'`` or ``'github'``.",
        examples=["google"],
    )


class OAuthAccountResponse(BaseModel):
    """Public representation of a linked OAuth account.

    Returned by ``GET /v1/auth/oauth/accounts``.
    """

    id: UUID = Field(..., description="OAuthAccount UUID.")
    provider: str = Field(
        ..., description="OAuth provider name.", examples=["google"]
    )
    provider_user_id: str = Field(
        ..., description="User ID from the OAuth provider.", examples=["123456789"]
    )
    created_at: datetime = Field(
        ..., description="When this OAuth link was created."
    )

    model_config = ConfigDict(from_attributes=True)


class OAuthUnlinkRequest(BaseModel):
    """Request body for ``POST /v1/auth/oauth/unlink``.

    Removes a linked OAuth account from the authenticated user.
    """

    provider: str = Field(
        ...,
        pattern=r"^(google|github)$",
        description="OAuth provider name to unlink.",
        examples=["google"],
    )
