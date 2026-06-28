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

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.graph_observation import GraphObservation

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
    ) -> list[GraphObservation]:
        """Retrieve all observations for a project.

        Args:
            project_id: The project to scope the query to.

        Returns:
            A list of :class:`GraphObservation` instances, ordered by
            ``confidence DESC, created_at DESC``.
        """
        result = await self._db.execute(
            select(GraphObservation)
            .where(GraphObservation.project_id == project_id)
            .order_by(GraphObservation.confidence.desc(),
                      GraphObservation.created_at.desc())
        )
        return result.scalars().all()

    async def get_by_subject(
        self,
        project_id: UUID,
        subject_entity_id: UUID,
    ) -> list[GraphObservation]:
        """Get all observations about a specific entity in a project.

        Args:
            project_id: Project scope.
            subject_entity_id: The entity to query observations for.

        Returns:
            Observations about this entity, ordered by confidence desc.
        """
        result = await self._db.execute(
            select(GraphObservation)
            .where(GraphObservation.project_id == project_id)
            .where(GraphObservation.subject_entity_id == subject_entity_id)
            .order_by(GraphObservation.confidence.desc(),
                      GraphObservation.created_at.desc())
        )
        return result.scalars().all()

    async def get_by_type(
        self,
        project_id: UUID,
        observation_type: str,
    ) -> list[GraphObservation]:
        """Get all observations of a specific type in a project.

        Args:
            project_id: Project scope.
            observation_type: One of ``ObservationType`` enum values.

        Returns:
            Observations matching the type, ordered by confidence desc.
        """
        result = await self._db.execute(
            select(GraphObservation)
            .where(GraphObservation.project_id == project_id)
            .where(GraphObservation.observation_type == observation_type)
            .order_by(GraphObservation.confidence.desc(),
                      GraphObservation.created_at.desc())
        )
        return result.scalars().all()

    async def get_pair_observations(
        self,
        project_id: UUID,
        entity_id_a: UUID,
        entity_id_b: UUID,
    ) -> list[GraphObservation]:
        """Get all observations for a specific entity pair (either direction).

        Args:
            project_id: Project scope.
            entity_id_a: First entity in the pair.
            entity_id_b: Second entity in the pair.

        Returns:
            Observations involving this pair, in either direction.
        """
        result = await self._db.execute(
            select(GraphObservation)
            .where(GraphObservation.project_id == project_id)
            .where(
                ((GraphObservation.subject_entity_id == entity_id_a)
                 & (GraphObservation.related_entity_id == entity_id_b))
                | ((GraphObservation.subject_entity_id == entity_id_b)
                   & (GraphObservation.related_entity_id == entity_id_a))
            )
            .order_by(GraphObservation.confidence.desc())
        )
        return result.scalars().all()

    # ── Detection queries ───────────────────────────────────────────────────────
    # These are extracted from ObservationService to keep SQL out of the service
    # layer.  Each method maps to one graph-topology analysis query.

    async def get_episode_count(self, project_id: UUID) -> int:
        """Get total distinct episode count for a project.

        Args:
            project_id: Project scope.

        Returns:
            Number of distinct episodes.
        """
        result = await self._db.execute(
            text("""
                SELECT COUNT(DISTINCT episode_id) AS total
                FROM graph_episode_entities
                WHERE project_id = :project_id
            """),
            {"project_id": project_id},
        )
        row = result.mappings().one_or_none()
        return row["total"] if row else 0

    async def get_co_occurring_pairs(
        self,
        project_id: UUID,
        min_count: int,
    ) -> list[dict]:
        """Find entity pairs that co-occur above a frequency threshold.

        Args:
            project_id: Project scope.
            min_count: Minimum co-occurrence count.

        Returns:
            List of dicts with keys: entity_a_id, entity_a_name,
            entity_b_id, entity_b_name, co_count.
        """
        result = await self._db.execute(
            text("""
                SELECT
                    a.entity_id AS entity_a_id,
                    ge_a.name AS entity_a_name,
                    b.entity_id AS entity_b_id,
                    ge_b.name AS entity_b_name,
                    COUNT(DISTINCT a.episode_id) AS co_count
                FROM graph_episode_entities a
                JOIN graph_episode_entities b
                    ON a.episode_id = b.episode_id
                    AND a.entity_id < b.entity_id
                JOIN graph_entities ge_a
                    ON ge_a.id = a.entity_id
                JOIN graph_entities ge_b
                    ON ge_b.id = b.entity_id
                WHERE a.project_id = :project_id
                GROUP BY entity_a_id, entity_a_name, entity_b_id, entity_b_name
                HAVING COUNT(DISTINCT a.episode_id) >= :min_count
                ORDER BY co_count DESC
            """),
            {"project_id": project_id, "min_count": min_count},
        )
        return result.mappings().all()

    async def get_entity_timestamps(self, project_id: UUID) -> list[dict]:
        """Get ordered entity → episode timestamps for temporal analysis.

        Args:
            project_id: Project scope.

        Returns:
            List of dicts with keys: entity_id, entity_name,
            episode_created_at.
        """
        result = await self._db.execute(
            text("""
                SELECT
                    gee.entity_id,
                    ge.name AS entity_name,
                    e.created_at AS episode_created_at
                FROM graph_episode_entities gee
                JOIN episodes e
                    ON e.id = gee.episode_id
                    AND e.is_deleted = false
                JOIN graph_entities ge
                    ON ge.id = gee.entity_id
                WHERE gee.project_id = :project_id
                ORDER BY gee.entity_id, e.created_at
            """),
            {"project_id": project_id},
        )
        return result.mappings().all()

    async def get_fact_predicate_counts(self, project_id: UUID) -> list[dict]:
        """Get predicate frequency for each entity (as subject).

        Args:
            project_id: Project scope.

        Returns:
            List of dicts with keys: entity_id, entity_name, entity_type,
            predicate, predicate_count, total_facts.
        """
        result = await self._db.execute(
            text("""
                SELECT
                    f.subject_entity_id AS entity_id,
                    ge.name AS entity_name,
                    ge.entity_type,
                    f.predicate,
                    COUNT(*) AS predicate_count,
                    COUNT(*) OVER (PARTITION BY f.subject_entity_id) AS total_facts
                FROM facts f
                JOIN graph_entities ge
                    ON ge.id = f.subject_entity_id
                WHERE f.project_id = :project_id
                  AND f.subject_entity_id IS NOT NULL
                  AND f.invalid_at IS NULL
                GROUP BY f.subject_entity_id, ge.name, ge.entity_type, f.predicate
                HAVING COUNT(*) >= 2
                ORDER BY entity_id, predicate_count DESC
            """),
            {"project_id": project_id},
        )
        return result.mappings().all()

    async def get_relationship_ids_between(
        self,
        project_id: UUID,
        entity_a_id: UUID,
        entity_b_id: UUID,
    ) -> list[UUID]:
        """Fetch graph_relationship IDs connecting two entities (either direction).

        Args:
            project_id: Project scope.
            entity_a_id: First entity UUID.
            entity_b_id: Second entity UUID.

        Returns:
            List of relationship UUIDs (may be empty).
        """
        result = await self._db.execute(
            text("""
                SELECT id FROM graph_relationships
                WHERE project_id = :project_id
                  AND invalid_at IS NULL
                  AND ((source_id = :a AND target_id = :b)
                       OR (source_id = :b AND target_id = :a))
                ORDER BY created_at DESC
            """),
            {"project_id": project_id, "a": entity_a_id, "b": entity_b_id},
        )
        return [row[0] for row in result.all()]

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
