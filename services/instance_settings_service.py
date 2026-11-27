"""Singleton instance-settings access behind a service boundary.

Thin delegate over :class:`InstanceSettingsRepository` so routers never
touch the repository directly — the admin registration endpoints
(``GET``/``PATCH /admin/system/registration``) flow through here.
"""

from __future__ import annotations

from models.instance_settings import InstanceSettings
from repositories.instance_settings_repository import InstanceSettingsRepository


class InstanceSettingsService:
    """Read/update the singleton instance-settings row.

    Args:
        repo: Repository for the fixed ``id = 1`` settings row.
    """

    def __init__(self, repo: InstanceSettingsRepository) -> None:
        self._repo = repo

    async def get_registration(self) -> InstanceSettings:
        """Return the singleton settings row (seeded lazily if absent).

        Returns:
            The single :class:`InstanceSettings` row.
        """
        return await self._repo.get()

    async def update_registration_mode(
        self, registration_mode: str
    ) -> InstanceSettings:
        """Update the platform registration mode.

        Args:
            registration_mode: New mode (``open``/``invite_only``/``approval``,
                validated at the schema boundary).

        Returns:
            The updated :class:`InstanceSettings` row.
        """
        return await self._repo.update(registration_mode=registration_mode)
