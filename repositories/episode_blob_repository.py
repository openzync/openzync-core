"""Repository for episode_blobs table — CRUD for blob attachments.

Follows the same pattern as ``EpisodeRepository``: raw SQL for batch writes,
ORM-style reads.  Returns ``EpisodeBlob`` models.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.episode_blob import EpisodeBlob


class EpisodeBlobRepository:
    """Data access for binary blob attachments linked to episodes.

    Args:
        db: An async SQLAlchemy session.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Batch Create ─────────────────────────────────────────────────────────

    async def batch_create(
        self,
        organization_id: UUID,
        project_id: UUID,
        session_id: UUID,
        episode_id: UUID,
        created_by: UUID,
        blobs: list[dict],
    ) -> list[EpisodeBlob]:
        """Batch-insert blob records for an episode.

        Uses raw ``INSERT ... RETURNING`` SQL for a single round-trip,
        matching the batch pattern in ``EpisodeRepository.batch_create``.

        Args:
            organization_id: Tenant isolation UUID.
            project_id: Project UUID.
            session_id: Session UUID.
            episode_id: Episode UUID these blobs belong to.
            created_by: User UUID who uploaded the blobs.
            blobs: List of blob dicts with keys:
                ``storage_backend``, ``storage_key``, ``file_name``,
                ``mime_type``, ``file_size``, ``content_hash``,
                ``blob_index``, ``width``, ``height``, and optionally
                ``id`` (otherwise a UUID is generated).

        Returns:
            List of ``EpisodeBlob`` ORM instances with generated fields
            populated (id, timestamps, etc.).
        """
        if not blobs:
            return []

        values: list[str] = []
        params: dict[str, str | int | None] = {}
        org_id_str = str(organization_id)
        proj_id_str = str(project_id)
        sess_id_str = str(session_id)
        ep_id_str = str(episode_id)
        user_id_str = str(created_by)

        for i, blob in enumerate(blobs):
            pfx = f"b{i}"
            params.update({
                f"{pfx}_id": blob.get("id", str(uuid4())),
                f"{pfx}_org": org_id_str,
                f"{pfx}_proj": proj_id_str,
                f"{pfx}_sess": sess_id_str,
                f"{pfx}_ep": ep_id_str,
                f"{pfx}_user": user_id_str,
                f"{pfx}_backend": blob.get("storage_backend", "s3"),
                f"{pfx}_key": blob.get("storage_key", ""),
                f"{pfx}_fname": blob.get("file_name", ""),
                f"{pfx}_mime": blob.get("mime_type", ""),
                f"{pfx}_size": blob.get("file_size", 0),
                f"{pfx}_hash": blob.get("content_hash", ""),
                f"{pfx}_w": blob.get("width"),
                f"{pfx}_h": blob.get("height"),
                f"{pfx}_idx": blob.get("blob_index", 0),
            })
            values.append(
                f"(:{pfx}_id, :{pfx}_org, :{pfx}_proj, :{pfx}_sess, "
                f":{pfx}_ep, :{pfx}_user, :{pfx}_backend, :{pfx}_key, "
                f":{pfx}_fname, :{pfx}_mime, :{pfx}_size, :{pfx}_hash, "
                f":{pfx}_w, :{pfx}_h, :{pfx}_idx)"
            )

        stmt = text(f"""
            INSERT INTO episode_blobs (
                id, organization_id, project_id, session_id, episode_id,
                created_by, storage_backend, storage_key, file_name, mime_type,
                file_size, content_hash, width, height, blob_index
            )
            VALUES {', '.join(values)}
            RETURNING
                id, organization_id, project_id, session_id, episode_id,
                created_by, storage_backend, storage_key, file_name, mime_type,
                file_size, content_hash, width, height, extracted_text,
                blob_index, created_at, updated_at
        """)

        result = await self._db.execute(stmt, params)
        rows = result.fetchall()

        # Convert raw rows to ORM models
        episode_blobs: list[EpisodeBlob] = []
        for row in rows:
            mapping = row._mapping  # type: ignore[attr-defined]
            episode_blobs.append(
                EpisodeBlob(
                    id=mapping["id"],
                    organization_id=mapping["organization_id"],
                    project_id=mapping["project_id"],
                    session_id=mapping["session_id"],
                    episode_id=mapping["episode_id"],
                    created_by=mapping["created_by"],
                    storage_backend=mapping["storage_backend"],
                    storage_key=mapping["storage_key"],
                    file_name=mapping["file_name"],
                    mime_type=mapping["mime_type"],
                    file_size=mapping["file_size"],
                    content_hash=mapping["content_hash"],
                    width=mapping["width"],
                    height=mapping["height"],
                    extracted_text=mapping["extracted_text"],
                    blob_index=mapping["blob_index"],
                    created_at=mapping["created_at"],
                    updated_at=mapping["updated_at"],
                )
            )
        return episode_blobs

    # ── Read ─────────────────────────────────────────────────────────────────

    async def get_by_episode(self, episode_id: UUID) -> list[EpisodeBlob]:
        """Get all blobs attached to an episode, ordered by ``blob_index``.

        Args:
            episode_id: The episode UUID.

        Returns:
            List of ``EpisodeBlob`` instances, empty if none.
        """
        result = await self._db.execute(
            select(EpisodeBlob)
            .where(EpisodeBlob.episode_id == episode_id)
            .order_by(EpisodeBlob.blob_index)
        )
        return list(result.scalars().all())

    async def get_by_session(self, session_id: UUID) -> list[EpisodeBlob]:
        """Get all blobs for a session.

        Args:
            session_id: The session UUID.

        Returns:
            List of ``EpisodeBlob`` instances.
        """
        result = await self._db.execute(
            select(EpisodeBlob)
            .where(EpisodeBlob.session_id == session_id)
            .order_by(EpisodeBlob.created_at)
        )
        return list(result.scalars().all())

    async def get_by_id(self, blob_id: UUID) -> EpisodeBlob | None:
        """Get a single blob by its UUID.

        Args:
            blob_id: The blob UUID.

        Returns:
            ``EpisodeBlob`` instance or ``None``.
        """
        result = await self._db.execute(
            select(EpisodeBlob).where(EpisodeBlob.id == blob_id)
        )
        return result.scalar_one_or_none()

    async def get_by_content_hash(
        self, organization_id: UUID, content_hash: str
    ) -> list[EpisodeBlob]:
        """Find blobs with a matching content hash (for dedup).

        Args:
            organization_id: Tenant isolation UUID.
            content_hash: SHA-256 hex digest.

        Returns:
            List of matching ``EpisodeBlob`` instances.
        """
        result = await self._db.execute(
            select(EpisodeBlob)
            .where(
                EpisodeBlob.organization_id == organization_id,
                EpisodeBlob.content_hash == content_hash,
            )
        )
        return list(result.scalars().all())

    # ── Update ───────────────────────────────────────────────────────────────

    async def update_extracted_text(
        self, blob_id: UUID, extracted_text: str
    ) -> EpisodeBlob | None:
        """Update the extracted_text field after worker processing.

        Uses a targeted UPDATE with ``synchronize_session="fetch"`` to
        avoid the overhead of a separate SELECT round-trip.

        Args:
            blob_id: The blob UUID.
            extracted_text: Text extracted from the file.

        Returns:
            Updated EpisodeBlob instance or None if not found.
        """
        from sqlalchemy import update as sa_update

        stmt = (
            sa_update(EpisodeBlob)
            .where(EpisodeBlob.id == blob_id)
            .values(extracted_text=extracted_text)
            .returning(EpisodeBlob)
            .execution_options(synchronize_session="fetch")
        )
        result = await self._db.execute(stmt)
        await self._db.flush()
        return result.scalar_one_or_none()

    # ── Count / Delete ───────────────────────────────────────────────────────

    async def count_by_episode(self, episode_id: UUID) -> int:
        """Count blobs attached to an episode.

        Uses SQL ``COUNT`` to avoid loading rows into memory.

        Args:
            episode_id: The episode UUID.

        Returns:
            Number of blobs.
        """
        from sqlalchemy import func

        result = await self._db.execute(
            select(func.count(EpisodeBlob.id))
            .where(EpisodeBlob.episode_id == episode_id)
        )
        return result.scalar_one()

    async def get_orphaned_blobs(
        self,
        organization_id: UUID,
        limit: int = 100,
    ) -> list[EpisodeBlob]:
        """Find blobs whose episodes are soft-deleted.

        Used by the cleanup worker to identify orphaned S3 objects.

        Args:
            organization_id: Tenant isolation UUID.
            limit: Maximum blobs to return (batch size).

        Returns:
            List of EpisodeBlob instances with soft-deleted episodes.
        """
        from sqlalchemy import join

        from models.episode import Episode

        result = await self._db.execute(
            select(EpisodeBlob)
            .select_from(
                join(EpisodeBlob, Episode,
                     EpisodeBlob.episode_id == Episode.id)
            )
            .where(
                EpisodeBlob.organization_id == organization_id,
                Episode.is_deleted == True,
            )
            .limit(limit)
        )
        return list(result.scalars().all())

    async def delete_by_episode(self, episode_id: UUID) -> list[EpisodeBlob]:
        """Delete all blob records for an episode and return the deleted records.

        Returns the list so callers can clean up S3 keys.

        Args:
            episode_id: The episode UUID.

        Returns:
            List of deleted EpisodeBlob instances (with storage_key for S3 cleanup).
        """
        result = await self._db.execute(
            select(EpisodeBlob).where(EpisodeBlob.episode_id == episode_id)
        )
        blobs = list(result.scalars().all())
        for blob in blobs:
            await self._db.delete(blob)
        await self._db.flush()
        return blobs

    async def delete_by_ids(
        self, blob_ids: list[UUID]
    ) -> list[EpisodeBlob]:
        """Delete blob records by ID and return them.

        Args:
            blob_ids: List of blob UUIDs to delete.

        Returns:
            List of deleted EpisodeBlob instances.
        """
        result = await self._db.execute(
            select(EpisodeBlob).where(EpisodeBlob.id.in_(blob_ids))
        )
        blobs = list(result.scalars().all())
        for blob in blobs:
            await self._db.delete(blob)
        await self._db.flush()
        return blobs
