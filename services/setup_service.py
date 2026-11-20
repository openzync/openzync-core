"""One-time platform setup — creates the root superadmin on demand.

Replaces the removed boot-time ``services.platform_seed`` auto-seed: the
root user is now created exactly once via ``POST /v1/auth/setup`` instead
of on every application start.  The insert is race-safe (``ON CONFLICT
DO NOTHING`` + affected-row check), so concurrent setup calls cannot
create duplicate roots — the loser raises instead of silently no-op-ing.
"""

from __future__ import annotations

import asyncio
import logging

from repositories.instance_settings_repository import InstanceSettingsRepository
from repositories.organization_repository import OrganizationRepository
from repositories.user_repository import UserRepository
from utils.password import hash_password

logger = logging.getLogger(__name__)


class SetupService:
    """Creates the platform root user exactly once.

    Args:
        user_repo: Repository for root-user lookups and inserts.
        org_repo: Repository for platform-org lookups and ensures.
        settings_repo: Repository for the singleton settings row
            (registration mode).  Injected via
            ``dependencies.services.get_setup_service``.
    """

    def __init__(
        self,
        user_repo: UserRepository,
        org_repo: OrganizationRepository,
        settings_repo: InstanceSettingsRepository | None = None,
    ) -> None:
        self._user_repo = user_repo
        self._org_repo = org_repo
        # Optional for direct construction in scripts — the DI factory
        # always wires it; the fallback below only fires outside requests.
        self._settings_repo = settings_repo

    def _settings(self) -> InstanceSettingsRepository:
        """Return the injected settings repo (fallback constructs one)."""
        if self._settings_repo is not None:
            return self._settings_repo
        return InstanceSettingsRepository(self._org_repo.session)

    async def get_registration_mode(self) -> str:
        """Return the platform registration mode (defaults ``invite_only``).

        Thin delegate — the single implementation lives in
        :meth:`InstanceSettingsRepository.get_registration_mode` so the
        router stays a pure HTTP adapter.

        Returns:
            One of ``open``, ``invite_only``, ``approval``.
        """
        return await self._settings().get_registration_mode()

    async def create_root_once(self, password: str, org_name: str = "Platform") -> None:
        """Create the platform root superadmin if the platform is unset up.

        The org row is ensured first (idempotent, named ``org_name`` on
        first creation only), then the root row is inserted with ``ON
        CONFLICT DO NOTHING`` — the affected-row check is the single
        linearization point, so two racing calls cannot both succeed.  No
        pre-check-then-insert: a pre-check TOCTOUs against a concurrent
        setup and would report "not set up" spuriously.

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
        from services.auth_service import AuthService

        if not org_name.strip():
            raise ValidationError("Organization name must not be blank.")

        AuthService._validate_password(password)

        await self._org_repo.ensure_platform_org(name=org_name.strip())
        # bcrypt blocks the event loop — offload to a worker thread.
        pw_hash = await asyncio.to_thread(hash_password, password)
        inserted = await self._user_repo.create_platform_root(pw_hash)
        if not inserted:
            raise ConflictError("Platform setup is not available.")
        logger.info("platform_setup.root_created")
