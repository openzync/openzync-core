"""Per-org addon management — enable flags, settings, panels, health.

Platform install state stays in :class:`services.addon_service.AddonService`;
this module owns the org tier: per-org enable flags (effective only when the
platform kill-switch is on), server-side-validated settings values, and
panel descriptors. ``secret`` setting values live in OpenBao under the org
namespace and are masked on every read except the audited reveal endpoint.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from core.addons import registry as _registry
from core.addons.loader import publish_org_state
from core.addons.settings_schema import (
    MASKED_SECRET,
    mask_secrets,
    secret_keys,
    split_secrets,
    validate_settings_values,
)
from core.exceptions import (
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    ValidationError,
)
from core.openbao_exceptions import (
    OpenBaoError,
    OpenBaoNamespaceError,
    OpenBaoSecretNotFoundError,
)
from models.addon_state import AddonState
from repositories.addon_org_settings_repository import AddonOrgSettingsRepository
from repositories.addon_repository import AddonRepository
from schemas.addon_org import (
    AddonPanelsResponse,
    AddonSettingsResponse,
    OrgAddonHealthResponse,
    OrgAddonResponse,
)

logger = logging.getLogger(__name__)


class AddonOrgService:
    """Manage one org's view of platform-installed addons."""

    def __init__(
        self,
        org_repo: AddonOrgSettingsRepository,
        addon_repo: AddonRepository,
        app: Any,
    ) -> None:
        """Store collaborators.

        Args:
            org_repo: Per-org flags/values repository (RLS session).
            addon_repo: Platform addon rows (visible under any tenant).
            app: The FastAPI application (degraded map, Redis, OpenBao).
        """
        self._org_repo = org_repo
        self._addon_repo = addon_repo
        self._app = app

    async def list_for_org(self, org_id: uuid.UUID) -> list[OrgAddonResponse]:
        """List installed addons with this org's flags and settings metadata.

        Args:
            org_id: The organization UUID.

        Returns:
            One entry per installed addon.
        """
        rows = await self._addon_repo.get_all()
        org_rows = {r.addon_id: r for r in await self._org_repo.get_all_for_org(org_id)}
        degraded: dict[str, str] = getattr(self._app.state, "degraded", {})
        return [
            self._to_response(
                row, org_rows.get(row.id), degraded.get(f"addon:{row.id}")
            )
            for row in rows
        ]

    async def set_org_enabled(
        self, addon_id: str, org_id: uuid.UUID, enabled: bool
    ) -> OrgAddonResponse:
        """Flip one org's addon flag (platform must have it on).

        Args:
            addon_id: The addon slug.
            org_id: The organization UUID.
            enabled: The new per-org flag value.

        Returns:
            The updated org view.

        Raises:
            NotFoundError: If the addon is not installed.
            ConflictError: If it is degraded or disabled platform-wide.
        """
        row = await self._require_platform_usable(addon_id)
        org_row = await self._org_repo.set_enabled(addon_id, org_id, enabled)
        await publish_org_state(self._app, org_id, addon_id, enabled)
        degraded: dict[str, str] = getattr(self._app.state, "degraded", {})
        logger.info(
            "addon.org_enabled_flipped",
            extra={"addon_id": addon_id, "org_id": str(org_id), "enabled": enabled},
        )
        return self._to_response(row, org_row, degraded.get(f"addon:{addon_id}"))

    async def get_settings(
        self, addon_id: str, org_id: uuid.UUID
    ) -> AddonSettingsResponse:
        """Return the settings schema plus this org's masked values.

        Args:
            addon_id: The addon slug.
            org_id: The organization UUID.

        Returns:
            Schema and values (secrets masked as ``***``).

        Raises:
            NotFoundError: If not installed, or no settings declared.
            ExternalServiceError: If stored secrets are unreadable.
        """
        row = await self._require_installed(addon_id)
        schema = self._require_schema(row)
        org_row = await self._org_repo.get(addon_id, org_id)
        plain = dict(org_row.values) if org_row is not None else {}
        merged = {**plain, **await self._read_secrets(row, org_id, schema)}
        return AddonSettingsResponse(
            id=row.id, schema=schema, values=mask_secrets(schema, merged)
        )

    async def update_settings(
        self, addon_id: str, org_id: uuid.UUID, values: dict[str, Any]
    ) -> AddonSettingsResponse:
        """Validate and store one org's settings (secrets → OpenBao).

        A submitted ``***`` keeps the stored secret; secrets stay in the
        org namespace and never touch Postgres.

        Args:
            addon_id: The addon slug.
            org_id: The organization UUID.
            values: The org-supplied values object.

        Returns:
            The stored schema and masked values.

        Raises:
            NotFoundError: If not installed, or no settings declared.
            ValidationError: On any schema violation.
            ExternalServiceError: If secrets cannot be read or written.
        """
        row = await self._require_installed(addon_id)
        schema = self._require_schema(row)
        skeys = secret_keys(schema)
        stored = await self._read_secrets(row, org_id, schema)
        candidate = dict(values)
        for key in skeys:
            if candidate.get(key) == MASKED_SECRET:
                if key in stored:
                    candidate[key] = stored[key]
                else:
                    candidate.pop(key, None)
        validated = validate_settings_values(schema, candidate)
        plain, secrets = split_secrets(schema, validated)
        await self._org_repo.upsert(addon_id, org_id, values=plain)
        if skeys:
            await self._write_secrets(addon_id, org_id, secrets)
        return AddonSettingsResponse(
            id=row.id, schema=schema, values=mask_secrets(schema, validated)
        )

    async def reveal_secrets(
        self, addon_id: str, org_id: uuid.UUID, keys: list[str]
    ) -> dict[str, Any | None]:
        """Return plaintext secrets for explicit keys (audited by the router).

        Args:
            addon_id: The addon slug.
            org_id: The organization UUID.
            keys: The secret names to reveal.

        Returns:
            ``{key: value}`` (``None`` for never-set keys).

        Raises:
            NotFoundError: If not installed, or no settings declared.
            ValidationError: If a key is not a declared secret.
            ExternalServiceError: If secrets are unreadable.
        """
        row = await self._require_installed(addon_id)
        schema = self._require_schema(row)
        skeys = secret_keys(schema)
        unknown = [k for k in keys if k not in skeys]
        if unknown:
            raise ValidationError(f"Not secret settings: {unknown}.")
        stored = await self._read_secrets(row, org_id, schema)
        return {k: stored.get(k) for k in keys}

    async def get_panels(self, addon_id: str, org_id: uuid.UUID) -> AddonPanelsResponse:
        """Return panel descriptors from the manifest snapshot.

        Args:
            addon_id: The addon slug.
            org_id: The organization UUID.

        Returns:
            Panels plus platform/org enable state.

        Raises:
            NotFoundError: If the addon is not installed.
        """
        row = await self._require_installed(addon_id)
        org_enabled = await self._org_repo.is_enabled(addon_id, org_id)
        return AddonPanelsResponse(
            id=row.id,
            platform_enabled=row.enabled,
            org_enabled=org_enabled,
            panels=list(row.manifest.get("panels", [])),
        )

    async def health_for_org(
        self, addon_id: str, org_id: uuid.UUID
    ) -> OrgAddonHealthResponse:
        """Return the per-org health view (no probes — cheap metadata only).

        Args:
            addon_id: The addon slug.
            org_id: The organization UUID.

        Returns:
            Enable state plus load/degraded info.

        Raises:
            NotFoundError: If the addon is not installed.
        """
        row = await self._require_installed(addon_id)
        degraded: dict[str, str] = getattr(self._app.state, "degraded", {})
        org_enabled = await self._org_repo.is_enabled(addon_id, org_id)
        return OrgAddonHealthResponse(
            id=row.id,
            platform_enabled=row.enabled,
            org_enabled=org_enabled,
            status=row.status,
            loaded=_registry.get_addon(self._app, addon_id) is not None,
            degraded=degraded.get(f"addon:{addon_id}"),
        )

    async def _require_installed(self, addon_id: str) -> AddonState:
        """Return the platform row or 404.

        Args:
            addon_id: The addon slug.

        Raises:
            NotFoundError: If the addon is not installed.
        """
        row = await self._addon_repo.get_by_id(addon_id)
        if row is None:
            raise NotFoundError(f"Addon {addon_id!r} is not installed.")
        return row

    async def _require_platform_usable(self, addon_id: str) -> AddonState:
        """Return the platform row when an org may flip its flag.

        Args:
            addon_id: The addon slug.

        Raises:
            NotFoundError: If the addon is not installed.
            ConflictError: If degraded or disabled platform-wide (409).
        """
        row = await self._require_installed(addon_id)
        degraded: dict[str, str] = getattr(self._app.state, "degraded", {})
        if f"addon:{addon_id}" in degraded:
            raise ConflictError(f"Addon {addon_id!r} failed to load.")
        if not row.enabled:
            raise ConflictError(f"Addon {addon_id!r} is disabled platform-wide.")
        return row

    def _require_schema(self, row: AddonState) -> dict[str, Any]:
        """Return the settings schema or 404 with a clear message.

        Args:
            row: The platform addon row.

        Raises:
            NotFoundError: When the addon declares no settings.
        """
        if not row.settings_schema:
            raise NotFoundError(f"Addon {row.id!r} declares no settings.")
        return row.settings_schema

    def _secret_path(self, addon_id: str) -> str:
        """Return the OpenBao logical path for one addon's org secrets."""
        return f"addon/{addon_id}/settings"

    def _bao(self) -> Any:
        """Return the OpenBao client or fail loudly (no silent plaintext).

        Raises:
            ExternalServiceError: When the secrets backend is unavailable.
        """
        bao = getattr(self._app.state, "openbao_client", None)
        if bao is None:
            raise ExternalServiceError("Secrets backend unavailable.")
        return bao

    async def _read_secrets(
        self, row: AddonState, org_id: uuid.UUID, schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Read stored secrets (``{}`` when none or schema has no secrets).

        Args:
            row: The platform addon row.
            org_id: The organization UUID.
            schema: The parsed settings schema.

        Raises:
            ExternalServiceError: On backend errors other than missing data.
        """
        if not secret_keys(schema):
            return {}
        bao = self._bao()
        try:
            data = await bao.read_secret(
                self._secret_path(row.id), namespace=bao.org_namespace(org_id)
            )
        except (OpenBaoSecretNotFoundError, OpenBaoNamespaceError):
            return {}
        except OpenBaoError as exc:
            raise ExternalServiceError(f"Addon secrets unreadable: {exc}") from exc
        secrets = data.get("secrets", {})
        return dict(secrets) if isinstance(secrets, dict) else {}

    async def _write_secrets(
        self, addon_id: str, org_id: uuid.UUID, secrets: dict[str, Any]
    ) -> None:
        """Replace one org's stored secrets (full-replace, never merged).

        Args:
            addon_id: The addon slug.
            org_id: The organization UUID.
            secrets: The complete new secrets dict (may be empty).

        Raises:
            ExternalServiceError: If the write fails.
        """
        bao = self._bao()
        try:
            await bao.create_org_namespace(org_id)
            await bao.write_secret(
                self._secret_path(addon_id),
                {"secrets": secrets},
                namespace=bao.org_namespace(org_id),
            )
        except OpenBaoError as exc:
            raise ExternalServiceError(f"Addon secrets unwritable: {exc}") from exc

    def _to_response(
        self,
        row: AddonState,
        org_row: Any | None,
        degraded: str | None,
    ) -> OrgAddonResponse:
        """Merge a platform row with one org's flag.

        Args:
            row: The platform addon row.
            org_row: The org's row, or ``None`` (disabled, no values).
            degraded: The load failure reason, if any.

        Returns:
            The org-facing addon entry.
        """
        return OrgAddonResponse(
            id=row.id,
            version=row.version,
            platform_enabled=row.enabled,
            org_enabled=org_row is not None and org_row.enabled,
            status=row.status,
            degraded=degraded,
            has_settings=bool(row.settings_schema),
            panels=list(row.manifest.get("panels", [])),
        )
