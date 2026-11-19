"""One-time platform setup — creates the root superadmin on demand.

Replaces the removed boot-time ``services.platform_seed`` auto-seed: the
root user is now created exactly once via ``POST /v1/auth/setup`` instead
of on every application start.  The insert is race-safe (``ON CONFLICT
DO NOTHING`` + affected-row check), so concurrent setup calls cannot
create duplicate roots — the loser raises instead of silently no-op-ing.
"""

from __future__ import annotations

import logging

from repositories.organization_repository import OrganizationRepository
from repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)


class SetupService:
    """Creates the platform root user exactly once.

    Args:
        user_repo: Repository for root-user lookups and inserts.
        org_repo: Repository for platform-org lookups and ensures.
    """

    def __init__(
        self,
        user_repo: UserRepository,
        org_repo: OrganizationRepository,
    ) -> None:
        self._user_repo = user_repo
        self._org_repo = org_repo

    async def create_root_once(self, password: str, org_name: str = "Platform") -> None:
        """Create the platform root superadmin if the platform is unset up.

        The platform org row is ensured first (idempotent, named
        ``org_name`` on first creation only), then the root row is
        inserted with ``ON CONFLICT DO NOTHING`` — the affected-row check
        is the single linearization point, so two racing calls cannot both
        succeed.

        Args:
            password: The operator-chosen root password (plaintext).
            org_name: Display name for the singleton platform org
                (applies only when the org row is first created).

        Raises:
            ConflictError: If the platform is already set up.  The message
                is generic — it reveals nothing about which row exists.
            ValidationError: If the password does not meet strength rules
                or the org name is blank.
        """
        from core.exceptions import ConflictError, ValidationError
        from services import auth_service as _facade  # via facade (patchable)
        from services.auth_service import AuthService

        if not org_name.strip():
            raise ValidationError("Organization name must not be blank.")

        if await self._org_repo.get_platform_org() is not None:
            if await self._user_repo.get_platform_root() is not None:
                raise ConflictError("Platform setup is not available.")

        AuthService._validate_password(password)

        await self._org_repo.ensure_platform_org(name=org_name.strip())
        inserted = await self._user_repo.create_platform_root(
            _facade.hash_password(password)
        )
        if not inserted:
            raise ConflictError("Platform setup is not available.")
        logger.info("platform_setup.root_created")
