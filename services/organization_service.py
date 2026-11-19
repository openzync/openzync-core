"""Organization service — singleton platform-org management.

Single platform org: per-user org creation and the approval queue are
removed.  This service keeps only platform-org reads/toggles and the
superadmin cross-org member listing.  Raw inserts live in
``OrganizationRepository.create_organization`` — never ``db.add`` here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from core.exceptions import NotFoundError
from models.organization import Organization
from models.user import User
from repositories.organization_repository import OrganizationRepository
from repositories.user_repository import UserRepository

if TYPE_CHECKING:
    from core.sorting import SortSpec

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class OrgCodeInfo:
    """Org-code admin view — the join code plus its registration toggle."""

    org_code: str
    join_enabled: bool


class OrganizationService:
    """Business logic for the singleton platform organization.

    Args:
        repo: Repository for organization DB access.
    """

    def __init__(self, repo: OrganizationRepository) -> None:
        self._repo = repo
        self._db = repo.session

    async def get_platform_org(self) -> Organization:
        """Return the singleton platform org.

        Returns:
            The platform Organization.

        Raises:
            NotFoundError: If the platform org is missing (setup incomplete).
        """
        org = await self._repo.get_platform_org()
        if org is None:
            raise NotFoundError("Platform organization is not set up.")
        return org

    async def list_org_members(
        self,
        org_id: UUID,
        page: int = 1,
        limit: int = 50,
        sort: SortSpec | None = None,
    ) -> tuple[list[User], int]:
        """List a target org's dashboard users (superadmin cross-org view).

        Args:
            org_id: The target organization UUID.
            page: 1-based page number.
            limit: Page size (clamped to 1..200 by the repository).
            sort: Validated sort spec (forwarded opaque to the repository).

        Returns:
            A tuple of ``(users_on_page, total_matching_count)``.

        Raises:
            NotFoundError: If no organization with this UUID exists.
        """
        org = await self._repo.get_by_id(org_id)
        if org is None:
            raise NotFoundError(f"Organization {org_id} not found.")
        return await UserRepository(self._db).list_by_org(
            org_id,
            page=page,
            limit=limit,
            sort=sort,
        )

    # ── Org join toggle (self-registration switch) ────────────────────────────

    async def get_org_code(self, org_id: UUID) -> OrgCodeInfo:
        """Return the organization's current join code and registration state.

        Args:
            org_id: The organization UUID.

        Returns:
            An ``OrgCodeInfo`` with the org code and ``join_enabled`` flag.

        Raises:
            NotFoundError: If no organization with this UUID exists.
        """
        org = await self._repo.get_by_id(org_id)
        if org is None:
            raise NotFoundError(f"Organization {org_id} not found.")
        return OrgCodeInfo(org_code=org.org_code, join_enabled=org.join_enabled)

    async def set_join_enabled(self, org_id: UUID, enabled: bool) -> OrgCodeInfo:
        """Toggle whether the org accepts new members via self-registration.

        Args:
            org_id: The organization UUID.
            enabled: Whether self-registration is accepted.

        Returns:
            The fresh ``OrgCodeInfo`` (code unchanged, toggle updated).

        Raises:
            NotFoundError: If no organization with this UUID exists.
        """
        org = await self._repo.set_join_enabled(org_id, enabled)
        return OrgCodeInfo(org_code=org.org_code, join_enabled=org.join_enabled)
