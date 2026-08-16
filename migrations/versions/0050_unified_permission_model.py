"""Unified permission-based RBAC — users.permissions + api_keys.permissions.

Users and API keys now carry explicit permission string arrays using one
shared vocabulary (``core/rbac.py``: ``ALL_PERMISSIONS``).  Admin/superadmin
roles are wildcards — an empty array means "everything the role allows".
Non-admin users get the member defaults (``project:read``,
``project:write``) plus any optional grants.

Changes:
- ``users.permissions`` ARRAY(String) NOT NULL, server default ``'{}'``.
- ``api_keys.scopes`` renamed to ``api_keys.permissions`` (same ARRAY type).
- Data backfill:
  * members get ``{project:read,project:write}``; admin/superadmin stay
    ``'{}'`` (wildcard).
  * legacy API-key scopes are mapped to the new vocabulary
    (``read`` → ``project:read``, ``write`` → ``project:write``,
    ``admin`` → all permissions, ``admin:write`` →
    ``{configuration:write,project:manage,members:write}``).  Unknown
    legacy scopes are dropped (fail-safe).  Identity mappings for the new
    permission strings make the backfill a no-op if re-run.

RLS context: ``users`` and ``api_keys`` carry org-isolation policies keyed
on ``app.bypass_rls``/``app.org_id`` (migrations/0001).  The migration sets
``app.bypass_rls = 'true'`` session-locally so the DML is not silently
filtered when the migration role is not the table owner.

Revision ID: 0050
Revises: 0049
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | None = None
depends_on: str | None = None

# Legacy API-key scope → new permission mapping.  Identity rows for the new
# permission strings keep the backfill idempotent: re-running maps already
# migrated values to themselves instead of dropping them.
_SCOPE_MAP: tuple[tuple[str, str], ...] = (
    ("read", "project:read"),
    ("write", "project:write"),
    ("admin", "project:read"),
    ("admin", "project:write"),
    ("admin", "project:manage"),
    ("admin", "configuration:read"),
    ("admin", "configuration:write"),
    ("admin", "members:read"),
    ("admin", "members:write"),
    ("admin:write", "configuration:write"),
    ("admin:write", "project:manage"),
    ("admin:write", "members:write"),
    # Identity rows — idempotency guard.
    ("project:read", "project:read"),
    ("project:write", "project:write"),
    ("project:manage", "project:manage"),
    ("configuration:read", "configuration:read"),
    ("configuration:write", "configuration:write"),
    ("members:read", "members:read"),
    ("members:write", "members:write"),
)

_MEMBER_DEFAULTS = "{project:read,project:write}"


def upgrade() -> None:
    """Add users.permissions, rename api_keys.scopes, backfill both."""
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")

    # 1. users.permissions — empty array = wildcard via role.
    op.add_column(
        "users",
        sa.Column(
            "permissions",
            sa.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )

    # 2. Rename api_keys.scopes → api_keys.permissions (same ARRAY type).
    #    Also replace the legacy '{read,write}' server default so raw
    #    inserts without explicit permissions get valid vocabulary values.
    op.alter_column(
        "api_keys",
        "scopes",
        new_column_name="permissions",
        existing_type=sa.ARRAY(sa.String()),
        server_default=sa.text("'{project:read,project:write}'"),
    )

    # 3. Backfill users: members get the member defaults; admin/superadmin
    #    keep '{}' (wildcard).  The `permissions = '{}'` guard makes the
    #    UPDATE a no-op on re-run so custom grants are never clobbered.
    op.execute(
        sa.text(
            "UPDATE users SET permissions = :defaults "
            "WHERE role = 'member' AND permissions = '{}'"
        ).bindparams(defaults=_MEMBER_DEFAULTS)
    )

    # 4. Backfill api_keys: map each legacy scope element through the table
    #    above, dedupe + sort.  Unknown scopes match nothing → dropped.
    op.execute(
        sa.text(
            """
            UPDATE api_keys k
            SET permissions = sub.permissions
            FROM (
                SELECT
                    k2.id,
                    array_agg(DISTINCT m.permission ORDER BY m.permission) AS permissions
                FROM api_keys k2
                CROSS JOIN LATERAL unnest(k2.permissions) AS s(scope)
                JOIN (VALUES
                    (:m1s, :m1p), (:m2s, :m2p), (:m3s, :m3p), (:m4s, :m4p),
                    (:m5s, :m5p), (:m6s, :m6p), (:m7s, :m7p), (:m8s, :m8p),
                    (:m9s, :m9p), (:m10s, :m10p), (:m11s, :m11p), (:m12s, :m12p),
                    (:m13s, :m13p), (:m14s, :m14p), (:m15s, :m15p), (:m16s, :m16p),
                    (:m17s, :m17p), (:m18s, :m18p), (:m19s, :m19p)
                ) AS m(scope, permission) ON m.scope = s.scope
                GROUP BY k2.id
            ) sub
            WHERE k.id = sub.id
            """
        ).bindparams(
            **{
                f"m{i}s": src
                for i, (src, _) in enumerate(_SCOPE_MAP, start=1)
            },
            **{
                f"m{i}p": perm
                for i, (_, perm) in enumerate(_SCOPE_MAP, start=1)
            },
        )
    )


def downgrade() -> None:
    """Rename api_keys.permissions back to scopes and drop users.permissions.

    Rollback note: the legacy API-key scope values were overwritten in place
    by the upgrade backfill and cannot be reconstructed losslessly (e.g. a
    key with only ``project:read`` could have come from ``read`` or
    ``admin``).  Run ``alembic downgrade -1`` with the same
    ``OZ_DATABASE_URL`` used for the upgrade.
    """
    op.alter_column(
        "api_keys",
        "permissions",
        new_column_name="scopes",
        existing_type=sa.ARRAY(sa.String()),
    )
    op.drop_column("users", "permissions")