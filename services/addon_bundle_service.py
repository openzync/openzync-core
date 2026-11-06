"""Addon bundle service — install/upgrade/uninstall from upload bundles.

Accepts ``.zip`` / ``.tar.gz`` bundles, validates MIME + size + member
paths, verifies the ed25519 manifest signature against the platform key in
OpenBao, then loads under a versioned namespace and atomically publishes.
Uninstall keeps addon tables unless ``purge_data=True`` with explicit
confirmation; purge drops only manifest-declared tables (allowlisted
identifiers, advisory-locked).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError
from starlette.datastructures import UploadFile

from core.addons import registry as _registry
from core.addons.bundles import extract_bundle, read_settings_schema
from core.addons.loader import (
    ADDONS_DIR,
    build_record,
    load_hooks_module,
    mount_record,
    publish_state,
    unmount_record,
)
from core.addons.manifest import parse_manifest, verify_manifest_signature
from core.exceptions import ConflictError, NotFoundError, ValidationError
from repositories.addon_repository import AddonRepository

logger = logging.getLogger(__name__)


class AddonBundleService:
    """Install, upgrade, and uninstall addons from upload bundles."""

    def __init__(self, repo: AddonRepository, app: Any) -> None:
        """Store collaborators.

        Args:
            repo: Addon state repository (superadmin session).
            app: The FastAPI application (mount + degraded + OpenBao).
        """
        self._repo = repo
        self._app = app

    async def install(
        self,
        bundle: UploadFile,
        expected_name: str,
        expected_version: str,
    ) -> Any:
        """Install a new addon from an uploaded bundle.

        Args:
            bundle: The uploaded ``.zip`` / ``.tar.gz`` bundle.
            expected_name: Addon slug the bundle must declare.
            expected_version: Version the bundle must declare.

        Returns:
            The installed addon row.

        Raises:
            ValidationError: On bad MIME/size/paths, manifest mismatch, or
                signature failure.
            ConflictError: If the addon id is already installed.
        """
        raw_toml, stage = await extract_bundle(bundle)
        try:
            manifest = parse_manifest(raw_toml)
            self._check_expected(manifest, expected_name, expected_version)
            if await self._repo.exists(manifest.id):
                raise ConflictError(f"Addon {manifest.id!r} is already installed.")
            await self._verify_signature(raw_toml, manifest.signature)
            hooks = load_hooks_module(manifest.id, manifest.version, stage / "hooks.py")
            record = build_record(manifest, hooks)
            settings_schema = read_settings_schema(stage)
            try:
                row = await self._repo.create(
                    manifest.id,
                    manifest.version,
                    self._manifest_snapshot(manifest),
                    settings_schema=settings_schema,
                )
            except IntegrityError as exc:
                # PK is the arbiter — the exists() check above races.
                raise ConflictError(
                    f"Addon {manifest.id!r} is already installed."
                ) from exc
            self._promote_stage(stage, manifest.id)
            mount_record(self._app, record)
            _registry.put_record(self._app, record)
            await publish_state(self._app, manifest.id, True)
            logger.info("addon.installed", extra={"addon_id": manifest.id})
            return row
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    async def upgrade(self, addon_id: str, bundle: UploadFile) -> Any:
        """Upgrade an installed addon (new major coexists, then swaps).

        Args:
            addon_id: The installed addon slug.
            bundle: The new-version bundle.

        Returns:
            The updated addon row.

        Raises:
            NotFoundError: If not installed.
            ValidationError: On bundle/signature failure or id mismatch.
        """
        row = await self._repo.get_by_id(addon_id)
        if row is None:
            raise NotFoundError(f"Addon {addon_id!r} is not installed.")
        raw_toml, stage = await extract_bundle(bundle)
        try:
            manifest = parse_manifest(raw_toml)
            if manifest.id != addon_id:
                raise ValidationError(
                    f"Bundle declares {manifest.id!r}, expected {addon_id!r}."
                )
            await self._verify_signature(raw_toml, manifest.signature)
            hooks = load_hooks_module(manifest.id, manifest.version, stage / "hooks.py")
            record = build_record(manifest, hooks)
            settings_schema = read_settings_schema(stage)
            row = await self._repo.update(
                addon_id, version=manifest.version, enabled=True, status="enabled"
            )
            await self._repo.set_settings_schema(addon_id, settings_schema)
            self._promote_stage(stage, manifest.id)
            unmount_record(self._app, addon_id)
            mount_record(self._app, record)
            _registry.put_record(self._app, record)
            await publish_state(self._app, manifest.id, True)
            return row
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    async def uninstall(
        self, addon_id: str, purge_data: bool = False, confirm: str = ""
    ) -> None:
        """Uninstall an addon; tables survive unless purged explicitly.

        Args:
            addon_id: The installed addon slug.
            purge_data: Also drop manifest-declared tables.
            confirm: Must equal the addon id when ``purge_data`` is set.

        Raises:
            NotFoundError: If not installed.
            ValidationError: If purge requested without exact confirmation.
        """
        row = await self._repo.get_by_id(addon_id)
        if row is None:
            raise NotFoundError(f"Addon {addon_id!r} is not installed.")
        await self._repo.acquire_uninstall_lock(addon_id)
        if purge_data:
            if confirm != addon_id:
                raise ValidationError(
                    "Data purge requires confirm=<addon id> — refusing."
                )
            for table in row.manifest.get("tables", []):
                await self._repo.drop_addon_table(table)
        unmount_record(self._app, addon_id)
        _registry.drop_record(self._app, addon_id)
        await publish_state(self._app, addon_id, False)
        await self._repo.delete(addon_id)
        shutil.rmtree(ADDONS_DIR / addon_id, ignore_errors=True)
        logger.info(
            "addon.uninstalled", extra={"addon_id": addon_id, "purged": purge_data}
        )

    def _check_expected(self, manifest: Any, name: str, version: str) -> None:
        """Fail closed when the bundle identity differs from the request.

        Args:
            manifest: The parsed manifest.
            name: The expected addon slug.
            version: The expected version.

        Raises:
            ValidationError: On mismatch.
        """
        if manifest.id != name or manifest.version != version:
            raise ValidationError(
                f"Bundle declares {manifest.id}@{manifest.version}, "
                f"expected {name}@{version}."
            )

    async def _verify_signature(self, raw: bytes, signature_hex: str) -> None:
        """Verify the manifest against the platform OpenBao signing key.

        Args:
            raw: The raw ``addon.toml`` bytes.
            signature_hex: The manifest's declared signature.

        Raises:
            ValidationError: When no key is configured or verification fails.
        """
        bao = getattr(self._app.state, "openbao_client", None)
        if bao is None:
            raise ValidationError("Secrets backend unavailable — refusing install.")
        try:
            secret = await bao.read_secret("addon/signing-pubkey")
        except Exception as exc:
            raise ValidationError(f"Signing key unreadable: {exc}") from exc
        pubkey = ""
        if isinstance(secret, dict):
            pubkey = str(secret.get("public_key", "") or secret.get("value", ""))
        verify_manifest_signature(raw, signature_hex, pubkey)

    def _promote_stage(self, stage: Path, addon_id: str) -> None:
        """Move the staged bundle into the live addons directory.

        Args:
            stage: The staging directory.
            addon_id: The addon slug.
        """
        dest = ADDONS_DIR / addon_id
        ADDONS_DIR.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(stage), str(dest))

    def _manifest_snapshot(self, manifest: Any) -> dict:
        """Render the manifest as a JSON-safe snapshot for the DB row.

        Args:
            manifest: The parsed manifest.

        Returns:
            The snapshot dict.
        """
        return {
            "name": manifest.id,
            "version": manifest.version,
            "min_core_version": manifest.min_core_version,
            "hooks": list(manifest.hooks),
            "permissions": list(manifest.permissions),
            "tables": list(manifest.tables),
            "panels": [
                {"id": p.id, "title": p.title, "path": p.path}
                for p in manifest.panels
            ],
        }
