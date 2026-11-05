"""Auth tokens — refresh rotation and token issuance."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from core.exceptions import (
    AuthenticationError,
)
from schemas.auth import (
    TokenResponse,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════

def _access_token_ttl() -> timedelta:
    """Lazy access to JWT access token TTL from settings."""
    from services import auth_service as _facade  # via facade (patchable)
    return timedelta(minutes=_facade.get_settings().JWT_ACCESS_TOKEN_TTL_MINUTES)


def _refresh_token_ttl() -> timedelta:
    """Lazy access to JWT refresh token TTL from settings."""
    from services import auth_service as _facade  # via facade (patchable)
    return timedelta(days=_facade.get_settings().JWT_REFRESH_TOKEN_TTL_DAYS)

_JWT_ALGORITHM = "HS256"
_MFA_SESSION_TTL_SEC = 600  # 10 minutes — MFA pending session lifetime
_REFRESH_FAMILY_WALK_LIMIT = 30  # bounded loop for rotation-family revocation
class TokenMixin:
    """Composed into :class:`AuthService`."""

    _repo: Any
    _otp_service: Any
    _redis: Any
    _org_repo: Any
    _email_service: Any
    _bao_client: Any

    # ── Refresh ─────────────────────────────────────────────────────────────

    async def refresh(self, raw_token: str) -> TokenResponse:
        """Rotate a refresh token and issue a new token pair.

        The presented token is claimed with a single conditional UPDATE,
        so exactly one of two concurrent requests with the same token
        wins.  A loser — or any replay of an already-rotated token —
        triggers revocation of the entire rotation family before a
        generic rejection, so a stolen token cannot keep derived tokens
        alive.

        Args:
            raw_token: The opaque refresh token string from the client.

        Returns:
            A new ``TokenResponse`` with fresh access and refresh tokens.

        Raises:
            AuthenticationError: If the refresh token is invalid, expired,
                reused, or the owning user is deactivated.
        """
        token_hash = self._hash_refresh_token(raw_token)

        # Atomic claim — a concurrent request with the same token loses here.
        if not await self._repo.revoke_refresh_token_if_current(token_hash):
            await self._revoke_family(token_hash)
            raise AuthenticationError(
                "Refresh token is invalid or has expired."
            )

        stored = await self._repo.get_refresh_token_by_hash(token_hash)
        if stored is None:
            # Unreachable: the conditional UPDATE only matches existing rows.
            raise AuthenticationError("Refresh token is invalid or has expired.")

        # The token row is the only thing that knows the tenant \u2014 bind the
        # session to it now, so the rotation writes below are org-scoped.
        await self._org_repo.set_tenant_context(stored.organization_id)

        # Look up the user to get the actual role
        user_id = uuid.UUID(stored.user_id)
        user = await self._repo.get_user_by_id(user_id)
        if user is None:
            raise AuthenticationError("User no longer exists.")
        if not user.is_active or user.is_deleted:
            raise AuthenticationError("This account has been deactivated.")
        role = user.role if user.role is not None else "member"

        new_tokens = await self.issue_tokens(
            user_id=user_id,
            organization_id=stored.organization_id,
            role=role,
        )

        # Chain the claimed token to its successor (rotation audit trail).
        new_refresh_hash = self._hash_refresh_token(new_tokens.refresh_token)
        new_stored = await self._repo.get_refresh_token_by_hash(
            new_refresh_hash
        )
        if new_stored is not None:
            await self._repo.set_refresh_token_rotated_by(
                stored.id,
                new_stored.id,
            )

        return new_tokens

    async def _revoke_family(self, token_hash: str) -> None:
        """Revoke the entire rotation family of a reused refresh token.

        Walks the ``rotated_by`` successor chain from the presented token
        forward to the leaf and revokes every linked token.  Ancestors are
        already revoked by rotation, so this walk covers every live token
        in the family.  Bounded to ``_REFRESH_FAMILY_WALK_LIMIT`` hops.

        Args:
            token_hash: SHA-256 hash of the presented (rejected) token.
        """
        presented = await self._repo.get_refresh_token_by_hash(token_hash)
        if presented is None:
            return  # token never existed — nothing to walk

        logger.warning(
            "security.refresh_token_reuse",
            extra={
                "user_id": presented.user_id,
                "token_id": str(presented.id),
            },
        )

        family_ids: list[uuid.UUID] = [presented.id]
        seen: set[uuid.UUID] = {presented.id}
        node = presented
        for _ in range(_REFRESH_FAMILY_WALK_LIMIT):
            if node.rotated_by is None or node.rotated_by in seen:
                break
            seen.add(node.rotated_by)
            family_ids.append(node.rotated_by)
            successor = await self._repo.get_refresh_token_by_id(
                node.rotated_by
            )
            if successor is None:
                break
            node = successor

        await self._repo.revoke_refresh_token_ids(family_ids)
        # Persist the revocation NOW — the caller raises immediately after,
        # and the request-scoped session dependency would otherwise roll
        # the family kill back together with the error.
        await self._repo.commit()
    # ── Internal helpers ────────────────────────────────────────────────────

    async def issue_tokens(
        self,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        role: str,
    ) -> TokenResponse:
        """Generate and persist an access + refresh token pair.

        Public so sibling services (e.g. ``InviteService``) can log a user
        in without re-implementing JWT issuance.  All internal auth flows
        call this too — it is the single token-issuance path.

        The access token carries a ``mcp`` claim (must-change-password)
        **read from the DB row at issue time** — every issuance path
        (login, refresh, verify-email, invite-accept, change-password)
        gets the flag's current value.  Refresh never copies claims from
        the old token; a flag cleared by ``change_password`` produces
        ``mcp: false`` on the very next issuance.

        Args:
            user_id: The authenticated user's UUID.
            organization_id: The user's organization UUID.
            role: User role for JWT claims.

        Returns:
            A ``TokenResponse`` with fresh tokens.
        """
        # Bind the session to the token's tenant — the refresh-token insert
        # below is org-scoped, and every issuance path (login, refresh,
        # verify-email, invite-accept, passwordless login) lands here.
        await self._org_repo.set_tenant_context(organization_id)

        # Must-change-password flag — read from the source of truth at
        # issue time so a rotated/stale token can never outlive the flag.
        user = await self._repo.get_user_by_id(user_id)
        must_change_password = (
            bool(user.must_change_password) if user is not None else False
        )

        # Use naive UTC datetime for DB storage (refresh_token.expires_at
        # is TIMESTAMP WITHOUT TIME ZONE).
        now = datetime.now(UTC).replace(tzinfo=None)

        # Access token
        from services import auth_service as _facade  # via facade (patchable)
        access_token = _facade.create_jwt_token(
            data={
                "sub": str(user_id),
                "org_id": str(organization_id),
                "role": role,
                "mcp": must_change_password,
                "type": "access",
            },
            secret=_facade.get_settings().SECRET_KEY,
            expires_delta=_access_token_ttl(),
        )

        # Refresh token (opaque — stored as SHA-256 hash)
        raw_refresh = _facade.secrets.token_hex(32)
        refresh_hash = self._hash_refresh_token(raw_refresh)
        refresh_expires = now + _refresh_token_ttl()

        await self._repo.create_refresh_token(
            user_id=user_id,
            organization_id=organization_id,
            token_hash=refresh_hash,
            expires_at=refresh_expires,
        )

        return TokenResponse(
            access_token=access_token,
            refresh_token=raw_refresh,
            expires_in=int(_access_token_ttl().total_seconds()),
        )

    @staticmethod
    def _hash_refresh_token(raw: str) -> str:
        """Deterministic SHA-256 hash of a refresh token for DB storage.

        Args:
            raw: The opaque refresh token string.

        Returns:
            Hex-encoded SHA-256 digest.
        """
        return hashlib.sha256(raw.encode()).hexdigest()
