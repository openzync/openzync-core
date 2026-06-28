"""Observation repository — all database access for graph_observations.

Observations are surfaced by a second-pass graph-topology analysis
(see services/observation_service.py for pattern detection).  This
repository provides CRUD operations for persisting and querying
observation records created by the compute_observations worker.

Key patterns:
- Raw SQL with bound parameters (graph_observations has no ORM-based
  write path — all mutations go through this repository).
- ON CONFLICT DO UPDATE for idempotent upsert using the functional
  unique index idx_observations_dedup.
- No business logic — pure query construction.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class ObservationRepository:
    """All database access for graph_observations.

    Args:
        db: An async SQLAlchemy session.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Upsert ────────────────────────────────────────────────────────────────

    async def upsert(
        self,
        organization_id: UUID,
        project_id: UUID,
        subject_entity_id: UUID,
        observation_type: str,
        content: str,
        confidence: float,
        related_entity_id: UUID | None = None,
        supporting_fact_ids: list[UUID] | None = None,
        supporting_relationship_ids: list[UUID] | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        observation_metadata: dict | None = None,
    ) -> None:
        """Insert or update an observation using the functional unique index.

        Uses ``INSERT ... ON CONFLICT DO UPDATE`` with the
        ``idx_observations_dedup`` functional unique index on
        ``(project_id, subject_entity_id, observation_type,
          COALESCE(related_entity_id, sentinel))``.

        Entity-level observations (``related_entity_id=NULL``) and
        pair-level observations (``related_entity_id=<UUID>``) are
        both correctly deduplicated because the COALESCE expression
        converts NULL to the sentinel all-zeros UUID in the index.

        Args:
            organization_id: Owning organization.
            project_id: Owning project.
            subject_entity_id: The entity this observation is about.
            observation_type: One of ``ObservationType`` enum values.
            content: Natural-language description.
            confidence: Confidence score (0.0–1.0).
            related_entity_id: For pair-level observations, the other
                entity in the pair.  ``None`` for entity-level observations.
            supporting_fact_ids: UUIDs of facts supporting this observation.
            supporting_relationship_ids: UUIDs of graph_relationships
                supporting this observation.
            valid_from: Start of temporal validity.
            valid_to: End of temporal validity (``None`` = open-ended).
            observation_metadata: Arbitrary JSONB metadata.
        """
        await self._db.execute(
            text("""
                INSERT INTO graph_observations
                    (organization_id, project_id, subject_entity_id,
                     related_entity_id, observation_type, content, confidence,
                     supporting_fact_ids, supporting_relationship_ids,
                     valid_from, valid_to, observation_metadata, updated_at)
                VALUES
                    (:org_id, :project_id, :subject_entity_id,
                     :related_entity_id, :obs_type, :content, :confidence,
                     :fact_ids, :rel_ids, :valid_from, :valid_to,
                      CAST(:obs_metadata AS jsonb), NOW())
                ON CONFLICT (project_id, subject_entity_id, observation_type,
                             COALESCE(related_entity_id,
                              '00000000-0000-0000-0000-000000000000'::uuid))
                DO UPDATE SET
                    content = EXCLUDED.content,
                    confidence = EXCLUDED.confidence,
                    supporting_fact_ids = EXCLUDED.supporting_fact_ids,
                    supporting_relationship_ids = EXCLUDED.supporting_relationship_ids,
                    valid_from = EXCLUDED.valid_from,
                    valid_to = EXCLUDED.valid_to,
                    observation_metadata = EXCLUDED.observation_metadata,
                    updated_at = NOW()
            """),
            {
                "org_id": organization_id,
                "project_id": project_id,
                "subject_entity_id": subject_entity_id,
                "related_entity_id": related_entity_id,
                "obs_type": observation_type,
                "content": content,
                "confidence": confidence,
                "fact_ids": supporting_fact_ids,
                "rel_ids": supporting_relationship_ids,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "obs_metadata": _serialize_metadata(observation_metadata),
            },
        )

    # ── Query ─────────────────────────────────────────────────────────────────

    async def get_by_project(
        self,
        project_id: UUID,
    ) -> list[dict]:
        """Retrieve all observations for a project.

        Args:
            project_id: The project to scope the query to.

        Returns:
            A list of observation rows as dicts, ordered by
            ``confidence DESC, created_at DESC``.
        """
        result = await self._db.execute(
            text("""
                SELECT *
                FROM graph_observations
                WHERE project_id = :project_id
                ORDER BY confidence DESC, created_at DESC
            """),
            {"project_id": project_id},
        )
        return result.mappings().all()

    async def get_by_subject(
        self,
        project_id: UUID,
        subject_entity_id: UUID,
    ) -> list[dict]:
        """Get all observations about a specific entity in a project.

        Args:
            project_id: Project scope.
            subject_entity_id: The entity to query observations for.

        Returns:
            Observations about this entity, ordered by confidence desc.
        """
        result = await self._db.execute(
            text("""
                SELECT *
                FROM graph_observations
                WHERE project_id = :project_id
                  AND subject_entity_id = :subject_entity_id
                ORDER BY confidence DESC, created_at DESC
            """),
            {"project_id": project_id,
             "subject_entity_id": subject_entity_id},
        )
        return result.mappings().all()

    async def get_by_type(
        self,
        project_id: UUID,
        observation_type: str,
    ) -> list[dict]:
        """Get all observations of a specific type in a project.

        Args:
            project_id: Project scope.
            observation_type: One of ``ObservationType`` enum values.

        Returns:
            Observations matching the type, ordered by confidence desc.
        """
        result = await self._db.execute(
            text("""
                SELECT *
                FROM graph_observations
                WHERE project_id = :project_id
                  AND observation_type = :obs_type
                ORDER BY confidence DESC, created_at DESC
            """),
            {"project_id": project_id, "obs_type": observation_type},
        )
        return result.mappings().all()

    async def get_pair_observations(
        self,
        project_id: UUID,
        entity_id_a: UUID,
        entity_id_b: UUID,
    ) -> list[dict]:
        """Get all observations for a specific entity pair (either direction).

        Args:
            project_id: Project scope.
            entity_id_a: First entity in the pair.
            entity_id_b: Second entity in the pair.

        Returns:
            Observations involving this pair, in either direction.
        """
        result = await self._db.execute(
            text("""
                SELECT *
                FROM graph_observations
                WHERE project_id = :project_id
                  AND ((subject_entity_id = :a AND related_entity_id = :b)
                       OR (subject_entity_id = :b AND related_entity_id = :a))
                ORDER BY confidence DESC
            """),
            {"project_id": project_id, "a": entity_id_a, "b": entity_id_b},
        )
        return result.mappings().all()

    # ── Delete ────────────────────────────────────────────────────────────────

    async def delete_by_project(
        self,
        project_id: UUID,
    ) -> int:
        """Delete all observations for a project.

        Used when re-running a full project scan to replace stale
        observations with fresh data.

        Args:
            project_id: The project to clear observations for.

        Returns:
            Number of deleted rows.
        """
        result = await self._db.execute(
            text("""
                DELETE FROM graph_observations
                WHERE project_id = :project_id
            """),
            {"project_id": project_id},
        )
        return result.rowcount


def _serialize_metadata(metadata: dict | None) -> str | None:
    """Serialize metadata dict to JSON string for PostgreSQL JSONB cast.

    The SQL uses ``:metadata::jsonb``, which requires a JSON string
    parameter.  ``None`` is passed as-is (SQL sets it to NULL).

    Args:
        metadata: A dict or None.

    Returns:
        JSON string or None.
    """
    if metadata is None:
        return None
    import orjson

    return orjson.dumps(metadata).decode()
