"""Unit tests for services/platform_seed — idempotent platform bootstrap.

The seed runs at startup; these tests pin the three observable contracts:

1. Fresh boot: org + root user created, OpenBao namespace bootstrapped.
2. Re-boot (org exists): namespace reconciled, missing root re-created.
3. Concurrent multi-worker boot: the loser's primary-key race is treated
   as already-seeded (no crash); any OTHER constraint error still aborts.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from core.config import PLATFORM_ORG_ID
from services.platform_seed import ensure_platform_root

pytestmark = pytest.mark.unit

ROOT_EXTERNAL_ID = "root"


def _make_org() -> MagicMock:
    """Build a MagicMock mimicking the platform Organization row."""
    org = MagicMock()
    org.id = PLATFORM_ORG_ID
    org.name = "SYSTEM"
    org.status = "approved"
    return org


def _make_integrity_error(constraint: str | None) -> IntegrityError:
    """Build an IntegrityError with a fake PG constraint name."""
    err = IntegrityError("stmt", {}, Exception("duplicate"))
    orig = MagicMock()
    orig.constraint_name = constraint
    err.orig = orig
    return err


class TestEnsurePlatformRoot:
    """ensure_platform_root — fresh seed, reconcile, and race handling."""

    @pytest.mark.asyncio
    async def test_fresh_boot_creates_org_root_and_namespace(self) -> None:
        """No org yet → org + root user inserted, namespace bootstrapped."""
        db = AsyncMock()
        bao = AsyncMock()
        org_repo = AsyncMock()
        org_repo.get_by_id.return_value = None
        user_repo = AsyncMock()
        user_repo.get_by_external_id.return_value = None

        with (
            patch(
                "repositories.organization_repository.OrganizationRepository",
                return_value=org_repo,
            ),
            patch(
                "repositories.user_repository.UserRepository",
                return_value=user_repo,
            ),
        ):
            await ensure_platform_root(db, bao)

        # RLS bypass + org insert + root user + commit.
        assert db.execute.await_count >= 2  # set_config calls
        db.add.assert_called()
        db.commit.assert_awaited_once()
        bao.create_org_namespace.assert_awaited_once_with(PLATFORM_ORG_ID)

        # The root user is a superadmin with the must-change gate.
        root = db.add.call_args.args[0]
        assert root.role == "superadmin"
        assert root.must_change_password is True
        assert root.external_id == ROOT_EXTERNAL_ID

    @pytest.mark.asyncio
    async def test_existing_org_reconciles_namespace_and_root(self) -> None:
        """Org present but root missing → namespace reconciled, root created."""
        db = AsyncMock()
        bao = AsyncMock()
        org_repo = AsyncMock()
        org_repo.get_by_id.return_value = _make_org()
        user_repo = AsyncMock()
        user_repo.get_by_external_id.return_value = None

        with (
            patch(
                "repositories.organization_repository.OrganizationRepository",
                return_value=org_repo,
            ),
            patch(
                "repositories.user_repository.UserRepository",
                return_value=user_repo,
            ),
        ):
            await ensure_platform_root(db, bao)

        bao.create_org_namespace.assert_awaited_once_with(PLATFORM_ORG_ID)
        user_repo.get_by_external_id.assert_awaited_once_with(
            PLATFORM_ORG_ID, ROOT_EXTERNAL_ID
        )
        db.commit.assert_awaited_once()
        # No org insert on the reconcile path.
        assert not any(
            isinstance(c.args[0], MagicMock) and c.args[0].name == "SYSTEM"
            for c in db.add.call_args_list
        )

    @pytest.mark.asyncio
    async def test_existing_org_with_root_is_noop(self) -> None:
        """Fully seeded platform → no inserts, no commit, namespace reconciled."""
        db = AsyncMock()
        bao = AsyncMock()
        org_repo = AsyncMock()
        org_repo.get_by_id.return_value = _make_org()
        user_repo = AsyncMock()
        user_repo.get_by_external_id.return_value = MagicMock()

        with (
            patch(
                "repositories.organization_repository.OrganizationRepository",
                return_value=org_repo,
            ),
            patch(
                "repositories.user_repository.UserRepository",
                return_value=user_repo,
            ),
        ):
            await ensure_platform_root(db, bao)

        bao.create_org_namespace.assert_awaited_once_with(PLATFORM_ORG_ID)
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_concurrent_seed_pkey_race_is_not_a_failure(self) -> None:
        """Multi-worker loser: PK conflict → rollback, no crash, no namespace."""
        db = AsyncMock()
        bao = AsyncMock()
        org_repo = AsyncMock()
        org_repo.get_by_id.return_value = None

        with (
            patch(
                "repositories.organization_repository.OrganizationRepository",
                return_value=org_repo,
            ),
            patch(
                "repositories.user_repository.UserRepository",
                return_value=AsyncMock(),
            ),
            patch(
                "services.platform_seed.hash_password",
                return_value="hashed",
            ),
        ):
            db.flush.side_effect = _make_integrity_error("organizations_pkey")
            await ensure_platform_root(db, bao)

        db.rollback.assert_awaited_once()
        bao.create_org_namespace.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_pkey_constraint_error_still_aborts(self) -> None:
        """An org-code collision is NOT the seed race → propagates loudly."""
        db = AsyncMock()
        bao = AsyncMock()
        org_repo = AsyncMock()
        org_repo.get_by_id.return_value = None

        with (
            patch(
                "repositories.organization_repository.OrganizationRepository",
                return_value=org_repo,
            ),
            patch(
                "repositories.user_repository.UserRepository",
                return_value=AsyncMock(),
            ),
            patch(
                "services.platform_seed.hash_password",
                return_value="hashed",
            ),
        ):
            db.flush.side_effect = _make_integrity_error("organizations_org_code_key")
            with pytest.raises(IntegrityError):
                await ensure_platform_root(db, bao)

        db.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_constraint_name_still_aborts(self) -> None:
        """Driver returns constraint_name=None → NOT the seed race → propagates.

        Fail-closed: only the exact ``organizations_pkey`` constraint is the
        benign multi-worker race.  A missing constraint name means we cannot
        prove the failure was the PK race, so it must abort startup loudly —
        never be masked as "already seeded".
        """
        db = AsyncMock()
        bao = AsyncMock()
        org_repo = AsyncMock()
        org_repo.get_by_id.return_value = None

        with (
            patch(
                "repositories.organization_repository.OrganizationRepository",
                return_value=org_repo,
            ),
            patch(
                "repositories.user_repository.UserRepository",
                return_value=AsyncMock(),
            ),
            patch(
                "services.platform_seed.hash_password",
                return_value="hashed",
            ),
        ):
            db.flush.side_effect = _make_integrity_error(None)
            with pytest.raises(IntegrityError):
                await ensure_platform_root(db, bao)

        db.rollback.assert_not_awaited()
