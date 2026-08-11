"""Platform org + root user seed — idempotent, fail-fast.

Runs once at application startup (lifespan) after the DB engine, Redis,
and the OpenBao client are up.  Creates:

1. The platform organization — ``id=PLATFORM_ORG_ID``, ``name="SYSTEM"``,
   ``status='approved'``, ``join_enabled=false``, plus its OpenBao
   namespace (idempotent).
2. The root user — ``email='root'``, ``role='superadmin'``,
   ``must_change_password=True``, password from ``OZ_ROOT_PASSWORD``.
   The must-change flag forces a real password at first login; a LOUD
   ``security.root_default_credentials`` warning is logged whenever the
   default credential is in effect.

Idempotent: if the platform org already exists (by id or by the exact
``SYSTEM`` name), this is a no-op.

RLS: the insert runs with ``app.bypass_rls='true'`` + ``app.org_id`` set
to the platform UUID (same pattern as the 0044 data-migration backfill),
so the seed works regardless of the DB role's table ownership.

Fail-fast: any error propagates to the lifespan and aborts startup — a
silent seed failure would leave the platform with no way to bootstrap a
superadmin.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import text

from core.config import PLATFORM_ORG_ID, get_settings
from core.org_codes import generate_org_code
from models.organization import Organization
from models.user import User
from utils.password import hash_password

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from core.openbao import OpenBaoClient

logger = logging.getLogger(__name__)

_PLATFORM_ORG_NAME = "SYSTEM"
"""The platform org is always named SYSTEM (exact match)."""


async def ensure_platform_root(
    db: AsyncSession,  # noqa: F821
    bao_client: OpenBaoClient,  # noqa: F821
) -> None:
    """Create the platform org + root user if they do not exist yet.

    Args:
        db: A fresh async DB session (not org-scoped).
        bao_client: Authenticated OpenBao client (namespace bootstrap).

    Raises:
        Exception: Any failure propagates — the caller (app lifespan)
            must fail loudly; there is no silent fallback.
    """
    from repositories.organization_repository import OrganizationRepository

    org_repo = OrganizationRepository(db)
    existing = await org_repo.get_by_id(PLATFORM_ORG_ID)
    if existing is not None:
        logger.info(
            "platform_seed.noop_org_exists",
            extra={"org_id": str(PLATFORM_ORG_ID)},
        )
        return

    # Bypass RLS for the insert (migration-0044 pattern) — the platform
    # org must be visible to the superadmin session that manages it.
    await db.execute(text("SELECT set_config('app.bypass_rls', 'true', true)"))
    await db.execute(
        text("SELECT set_config('app.org_id', :org_id, true)"),
        {"org_id": str(PLATFORM_ORG_ID)},
    )

    settings = get_settings()
    password = settings.OZ_ROOT_PASSWORD

    org = Organization(
        id=PLATFORM_ORG_ID,
        name=_PLATFORM_ORG_NAME,
        plan="enterprise",
        status="approved",
        join_enabled=False,
        org_code=generate_org_code(),
        is_active=True,
    )
    db.add(org)
    await db.flush()

    # Root user — email 'root' is not an address, so email verification is
    # meaningless here; the credential is the password + must-change gate.
    root = User(
        organization_id=PLATFORM_ORG_ID,
        external_id="root",
        email="root",
        name="Platform Root",
        role="superadmin",
        password_hash=hash_password(password),
        is_email_verified=True,
        must_change_password=True,
        is_active=True,
        metadata_={},
    )
    db.add(root)
    await db.flush()
    await db.commit()

    # OpenBao namespace (idempotent) — propagate failures; the org must not
    # exist without its secrets backend.
    await bao_client.create_org_namespace(PLATFORM_ORG_ID)

    if password == "admin":  # noqa: S105  — comparing against the known default, not a credential literal
        logger.warning(
            "security.root_default_credentials",
            extra={
                "org_id": str(PLATFORM_ORG_ID),
                "hint": (
                    "The platform root user was seeded with the default "
                    "OZ_ROOT_PASSWORD.  Set OZ_ROOT_PASSWORD in OpenBao "
                    "system config and change the password immediately."
                ),
            },
        )
    else:
        logger.info(
            "platform_seed.root_created",
            extra={"org_id": str(PLATFORM_ORG_ID)},
        )
