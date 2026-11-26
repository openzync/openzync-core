"""Strip per-org blob storage config — globalized to OZ_S3_* Settings.

Per-org blob storage was removed (clean break): one global S3 backend
for all orgs, configured via ``OZ_S3_ENDPOINT_URL`` /
``OZ_S3_ACCESS_KEY_ID`` / ``OZ_S3_SECRET_ACCESS_KEY`` (required,
fail-fast) plus ``OZ_S3_REGION`` / ``OZ_S3_BUCKET_NAME`` /
``OZ_S3_BACKEND`` / ``OZ_BLOB_MAX_SIZE_MB`` /
``OZ_BLOB_IMAGE_EXTRACTION`` (defaulted).  Deletes the eight blob keys
from every stored JSONB config so no stale per-org value (including
``s3_secret_access_key`` material) lingers:

* ``users.config_override`` (authoritative store, all rows)
* ``organizations.config`` (legacy mapped table, all rows)

Revision ID: 0077
Revises: 0076

Rollback note: downgrade is a no-op — removed values are not restored.
Re-apply via the env file (``OZ_S3_*`` / ``OZ_BLOB_*``) if needed.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0077"
down_revision: str | None = "0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Delete the per-org blob storage keys from all stored configs."""
    # RLS is FORCE + fail-closed (0059/0060): DML must bypass it, same
    # one-liner as 0044/0050/0053/0065/0073/0074/0075/0076. Session-local;
    # dies with the connection.
    op.execute("SELECT set_config('app.bypass_rls', 'true', false)")
    op.execute(
        "UPDATE users SET config_override = config_override "
        "- 'blob_storage_backend' - 's3_endpoint_url' - 's3_region' "
        "- 's3_access_key_id' - 's3_secret_access_key' "
        "- 's3_bucket_name' - 'max_blob_size_mb' - 'image_extraction'"
    )
    op.execute(
        "UPDATE organizations SET config = config "
        "- 'blob_storage_backend' - 's3_endpoint_url' - 's3_region' "
        "- 's3_access_key_id' - 's3_secret_access_key' "
        "- 's3_bucket_name' - 'max_blob_size_mb' - 'image_extraction'"
    )


def downgrade() -> None:
    """No-op — removed blob values are not restored."""
