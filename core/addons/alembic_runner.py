"""Per-addon alembic runner — isolated chains with their own version table.

Addon bundles ship an alembic-style ``migrations/versions/*.py`` chain.
Each chain runs against the platform database but stays isolated:

* version tracking lives in ``addon_alembic_<slug>``, never the core
  ``alembic_version`` table;
* only ``addon_<slug>_<table>`` tables drawn from the manifest
  allowlist may be created, and every created table must carry an
  ``organization_id`` column (checked statically before anything runs);
* any failure raises loud — callers fail the row/record closed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from core.addons.manifest import ID_RE, AddonManifest
from core.exceptions import ExternalServiceError, ValidationError

logger = logging.getLogger(__name__)

_CREATE_TABLE_RE = re.compile(r"""create_table\(\s*['"]([^'"]+)['"]""")


def version_table_name(addon_id: str) -> str:
    """Return the isolated version-table name for an addon."""
    return f"addon_alembic_{addon_id}"


def table_prefix(addon_id: str) -> str:
    """Return the required table-name prefix for an addon."""
    return f"addon_{addon_id}_"


def enforce_table_policy(
    addon_id: str, manifest: AddonManifest, mig_dir: Path
) -> None:
    """Statically enforce the table allowlist before applying anything.

    Args:
        addon_id: Manifest id (also the table-prefix slug).
        manifest: Validated manifest carrying the table allowlist.
        mig_dir: Bundle ``migrations/`` dir.

    Raises:
        ValidationError: On non-allowlisted tables, missing
            ``organization_id``, branching/merging, or multiple heads.
    """
    if not ID_RE.match(addon_id):
        raise ValidationError(f"Invalid addon id: {addon_id!r}.")
    allowed = {f"{table_prefix(addon_id)}{t}" for t in manifest.tables}
    versions = mig_dir / "versions"
    if not versions.is_dir():
        return
    for path in sorted(versions.glob("*.py")):
        try:
            source = path.read_text()
        except OSError as exc:
            raise ValidationError(f"Unreadable revision {path.name}: {exc}") from exc
        created = _CREATE_TABLE_RE.findall(source)
        for name in created:
            if name not in allowed:
                raise ValidationError(
                    f"Revision {path.name} creates {name!r}, "
                    "outside the manifest tables allowlist."
                )
        if created and "organization_id" not in source:
            raise ValidationError(
                f"Revision {path.name} creates tables "
                "without an organization_id column."
            )


async def migrate_up(
    addon_id: str,
    source_path: Path,
    manifest: AddonManifest,
    database_url: str,
) -> None:
    """Upgrade an addon chain to head (no-op without a ``versions/`` dir).

    Raises:
        ValidationError: On policy violations or a branched chain.
        ExternalServiceError: When the upgrade itself fails.
    """
    await asyncio.to_thread(
        _apply, addon_id, source_path, manifest, database_url, "up"
    )


async def migrate_down(
    addon_id: str,
    source_path: Path,
    manifest: AddonManifest,
    database_url: str,
) -> None:
    """Downgrade an addon chain to base (uninstall path)."""
    await asyncio.to_thread(
        _apply, addon_id, source_path, manifest, database_url, "down"
    )


async def drop_addon_tables(
    db: AsyncSession, addon_id: str, manifest: AddonManifest
) -> None:
    """Drop exactly the allowlisted ``addon_<slug>_<table>`` tables.

    Args:
        db: The service-layer session (caller owns commit/rollback).
        addon_id: Manifest id.
        manifest: Validated manifest carrying the table allowlist.
    """
    for short in manifest.tables:
        # Interpolated, not bound — table names can't be bind params. Safe:
        # every name passed manifest TABLE_RE validation at parse time.
        name = f"{table_prefix(addon_id)}{short}"
        await db.execute(text(f'DROP TABLE IF EXISTS "{name}" CASCADE'))
        logger.info("addon.table_dropped", extra={"addon": addon_id, "t": name})


def _sync_url(database_url: str) -> str:
    """Map the async DATABASE_URL to a sync psycopg2 URL for alembic."""
    if "+asyncpg://" in database_url:
        return database_url.replace("+asyncpg://", "+psycopg2://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    raise ExternalServiceError("Unsupported DATABASE_URL scheme for migrations.")


def _linear_chain(script: ScriptDirectory, addon_id: str) -> list:
    """Return the chain base-first; single linear head only (no merges)."""
    heads = script.get_heads()
    if len(heads) != 1:
        raise ValidationError(
            f"Addon {addon_id!r} chain must have exactly one head, "
            f"found {len(heads)}."
        )
    ordered = []
    seen: set[str] = set()
    rev = script.get_revision(heads[0])
    while rev is not None:
        if rev.revision in seen:
            raise ValidationError(f"Addon {addon_id!r} chain has a cycle.")
        seen.add(rev.revision)
        ordered.append(rev)
        down = rev.down_revision
        if isinstance(down, (tuple, list)):
            if len(down) > 1:
                raise ValidationError(
                    f"Addon {addon_id!r} merges are not supported."
                )
            down = down[0] if down else None
        if down is None:
            break
        nxt = script.get_revision(down)
        if nxt is None:
            raise ValidationError(f"Unknown down_revision {down!r}.")
        rev = nxt
    ordered.reverse()
    return ordered


def _ensure_version_table(conn, table: str) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        text(f'CREATE TABLE IF NOT EXISTS "{table}" '
             "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
    )


def _stamp(conn, table: str, revision: str) -> None:  # type: ignore[no-untyped-def]
    conn.execute(text(f'DELETE FROM "{table}"'))
    conn.execute(
        text(f'INSERT INTO "{table}" (version_num) VALUES (:rev)'),
        {"rev": revision},
    )


def _apply(
    addon_id: str,
    source_path: Path,
    manifest: AddonManifest,
    database_url: str,
    direction: str,
) -> None:
    """Blocking alembic-chain driver (runs in a worker thread)."""
    mig_dir = source_path / "migrations"
    if not (mig_dir / "versions").is_dir():
        logger.info("addon.migrations_absent", extra={"addon": addon_id})
        return
    enforce_table_policy(addon_id, manifest, mig_dir)
    table = version_table_name(addon_id)
    engine = create_engine(_sync_url(database_url), poolclass=NullPool)
    try:
        with engine.begin() as conn:
            script = ScriptDirectory(str(mig_dir))
            ordered = _linear_chain(script, addon_id)
            ctx = MigrationContext.configure(conn, opts={"version_table": table})
            _ensure_version_table(conn, table)
            current = set(ctx.get_current_heads())
            # ``op.*`` in revision modules resolves via this context stack.
            with Operations.context(ctx):
                if direction == "up":
                    for rev in ordered:
                        if rev.revision in current:
                            continue
                        rev.module.upgrade()
                        _stamp(conn, table, rev.revision)
                else:
                    for rev in reversed(ordered):
                        if rev.revision not in current:
                            continue
                        rev.module.downgrade()
                    conn.execute(text(f'DELETE FROM "{table}"'))
    except (ValidationError, ExternalServiceError):
        raise
    except Exception as exc:
        raise ExternalServiceError(
            f"Addon {addon_id!r} migration {direction} failed: {exc}"
        ) from exc
    finally:
        engine.dispose()
    logger.info("addon.migrated", extra={"addon": addon_id, "dir": direction})


__all__ = [
    "drop_addon_tables",
    "enforce_table_policy",
    "migrate_down",
    "migrate_up",
    "table_prefix",
    "version_table_name",
]
