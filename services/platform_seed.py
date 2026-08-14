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

Idempotent on every boot:
- Org already present → no-op, BUT the OpenBao namespace is reconciled
  (idempotent bootstrap heals a first boot that committed the DB rows and
  then crashed) and a missing root user is re-created.  An org without a
  root superadmin is an unbootstrapped platform.
- Multi-worker startup races the seed insert → the loser's primary-key
  conflict is treated as already-seeded (no crash loop); the winner's
  bootstrap wins.

RLS: the insert runs with ``app.bypass_rls='true'`` + ``app.org_id`` set
to the platform UUID (same pattern as the 0044 data-migration backfill),
so the seed works regardless of the DB role's table ownership.

Fail-fast: any error other than the concurrent-seed primary-key race
propagates to the lifespan and aborts startup — a silent seed failure
would leave the platform with no way to bootstrap a superadmin.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

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

_ROOT_EXTERNAL_ID = "root"


async def ensure_platform_root(
    db: AsyncSession,  # noqa: F821
    bao_client: OpenBaoClient,  # noqa: F821
) -> None:
    """Create the platform org + root user if they do not exist yet.

    Idempotent: re-runs on every boot.  When the org already exists the
    OpenBao namespace is reconciled (idempotent) and a missing root user
    is created.  A concurrent first boot (multi-worker) that loses the
    insert race is treated as already-seeded rather than a crash.

    Args:
        db: A fresh async DB session (not org-scoped).
        bao_client: Authenticated OpenBao client (namespace bootstrap).

    Raises:
        Exception: Any failure propagates — the caller (app lifespan)
            must fail loudly; there is no silent fallback.  The single
            exception is the concurrent-seed primary-key race, which is
            the normal multi-worker startup outcome.
    """
    from repositories.organization_repository import OrganizationRepository
    from repositories.user_repository import UserRepository

    settings = get_settings()
    password = settings.OZ_ROOT_PASSWORD

    org_repo = OrganizationRepository(db)
    existing = await org_repo.get_by_id(PLATFORM_ORG_ID)
    if existing is not None:
        # No-op path — reconcile what a crashed first boot may have missed.
        # (b) The namespace bootstrap is idempotent (skips an existing
        # namespace), so this heals a boot that committed the DB rows but
        # died before bootstrap — the secrets backend can never stay
        # permanently missing.
        await bao_client.create_org_namespace(PLATFORM_ORG_ID)
        # (c) Org present but root user missing = silently unbootstrapped
        # platform; create the root on the same idempotent path.
        root = await UserRepository(db).get_by_external_id(
            PLATFORM_ORG_ID, _ROOT_EXTERNAL_ID
        )
        if root is None:
            await _create_root_user(db, password)
            await db.commit()
            _log_root_credential_state(password)
            logger.info(
                "platform_seed.root_recreated",
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

    org = Organization(
        id=PLATFORM_ORG_ID,
        name=_PLATFORM_ORG_NAME,
        plan="enterprise",
        status="approved",
        join_enabled=False,
        org_code=generate_org_code(),
        is_active=True,
    )
    try:
        db.add(org)
        await db.flush()

        # Root user — email 'root' is not an address, so email verification
        # is meaningless here; the credential is the password + must-change
        # gate.
        await _create_root_user(db, password)
        await db.commit()
    except IntegrityError as err:
        # (a) Multi-worker startup: two workers race the seed insert and one
        # hits the PLATFORM_ORG_ID primary-key conflict.  That is not a
        # failure — the winner bootstrapped the platform — so roll back the
        # aborted transaction and continue startup instead of crash-looping.
        # Fail-closed: ONLY the exact `organizations_pkey` constraint is the
        # benign race.  A driver that reports no constraint name (None) or
        # any other constraint (e.g. an org-code collision) is a REAL startup
        # failure and re-raises loudly — never masked as "already seeded".
        constraint = getattr(getattr(err, "orig", None), "constraint_name", None)
        if constraint != "organizations_pkey":
            raise
        await db.rollback()
        logger.warning(
            "platform_seed.already_seeded",
            extra={"org_id": str(PLATFORM_ORG_ID), "constraint": constraint},
        )
        return

    # OpenBao namespace (idempotent) — propagate failures; the org must not
    # exist without its secrets backend.
    await bao_client.create_org_namespace(PLATFORM_ORG_ID)

    _log_root_credential_state(password)


async def _create_root_user(db: AsyncSession, password: str) -> User:  # noqa: F821
    """Insert the platform root superadmin (RLS-bypass), uncommitted.

    Shared by the fresh-seed and the reconcile-missing-root paths so the
    root row is created identically in both.

    Args:
        db: The seed session.
        password: The OZ_ROOT_PASSWORD credential to hash.

    Returns:
        The newly created (flushed, uncommitted) root User.
    """
    await db.execute(text("SELECT set_config('app.bypass_rls', 'true', true)"))
    await db.execute(
        text("SELECT set_config('app.org_id', :org_id, true)"),
        {"org_id": str(PLATFORM_ORG_ID)},
    )
    root = User(
        organization_id=PLATFORM_ORG_ID,
        external_id=_ROOT_EXTERNAL_ID,
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
    return root


def _log_root_credential_state(password: str) -> None:
    """Log the default-credential warning or a plain creation info.

    Args:
        password: The seeded OZ_ROOT_PASSWORD value.
    """
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
