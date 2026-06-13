"""Entity merge dedup worker — ARQ task for weekly scheduled entity deduplication.

Detects and merges duplicate entities within an organization's knowledge graph
using exact name matching and fuzzy string similarity (``pg_trgm``).

Pipeline:
    1. Query all non-merged entities for an org.
    2. Exact match: group by ``LOWER(name)`` — detect entities with identical
       names (case-insensitive).
    3. Fuzzy match: ``pg_trgm similarity > 0.85`` for remaining entities.
    4. For each duplicate cluster:
       a. Select canonical entity (most relationships, then most recently
          updated).
       b. Rewire all ``graph_relationships`` to canonical entity.
       c. Flag non-canonical entities with ``is_merged = True``.
       d. Write an ``audit_log`` entry with before/after snapshot.
    5. 7-day recovery window: soft-delete via ``is_merged`` flag, not hard
       delete.

Bitmask:
    Does NOT set an enrichment bit — this is a data maintenance task, not
    an episode enrichment step.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import text

from workers.tasks.base import with_retry

logger = structlog.get_logger()

# ── Constants ──────────────────────────────────────────────────────────────────

FUZZY_SIMILARITY_THRESHOLD: float = 0.85
"""Minimum ``pg_trgm`` similarity for fuzzy duplicate detection."""

MERGE_BATCH_SIZE: int = 100
"""Number of entity clusters to process in a single DB transaction."""


# ── Public ARQ task (decorated with retry) ─────────────────────────────────────


@with_retry(max_retries=2, base_delay_s=5.0)
async def merge_duplicate_entities(
    ctx: object,
    org_id: str | None = None,
) -> dict:
    """Merge duplicate entities for an organization.

    Can be called:
    - Without ``org_id`` (scheduled weekly run): processes all eligible orgs.
    - With ``org_id`` (manual trigger): processes a single org.

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        org_id: Optional org UUID to process (processes all if ``None``).

    Returns:
        Dict with ``status``, ``orgs_processed``, ``clusters_merged``,
        ``entities_merged``, ``relationships_rewired``.
    """
    from core.config import settings
    from core.db import get_async_session, init_db_engine

    _engine = init_db_engine(
        str(settings.DATABASE_URL),
        pool_size=5,
        max_overflow=2,
    )
    _session_factory = get_async_session(_engine)

    try:
        async with _session_factory() as db:
            # Determine which orgs to process
            if org_id:
                org_ids = [uuid.UUID(org_id)]
            else:
                org_ids = await _find_eligible_orgs(db)

            if not org_ids:
                logger.info("merge_duplicates.no_eligible_orgs")
                return {
                    "status": "skipped",
                    "reason": "No eligible orgs found",
                }

            total_clusters = 0
            total_merged = 0
            total_rewired = 0

            for current_org_id in org_ids:
                try:
                    clusters = await _process_org(
                        db, current_org_id,
                    )
                    total_clusters += clusters["clusters"]
                    total_merged += clusters["entities_merged"]
                    total_rewired += clusters["relationships_rewired"]
                except Exception as exc:
                    logger.error(
                        "merge_duplicates.org_failed",
                        org_id=str(current_org_id),
                        error=str(exc),
                    )

            logger.info(
                "merge_duplicates.completed",
                orgs_processed=len(org_ids),
                clusters_merged=total_clusters,
                entities_merged=total_merged,
                relationships_rewired=total_rewired,
            )

            return {
                "status": "completed",
                "orgs_processed": len(org_ids),
                "clusters_merged": total_clusters,
                "entities_merged": total_merged,
                "relationships_rewired": total_rewired,
            }

    finally:
        await _engine.dispose()


# ── Private helpers ────────────────────────────────────────────────────────────


async def _find_eligible_orgs(db: Any) -> list[uuid.UUID]:
    """Find organizations with at least two non-merged entities.

    Args:
        db: Database session.

    Returns:
        List of organization UUIDs eligible for dedup.
    """
    result = await db.execute(
        text("""
            SELECT organization_id, COUNT(*) as cnt
            FROM graph_entities
            WHERE is_merged = false
            GROUP BY organization_id
            HAVING COUNT(*) >= 2
        """),
    )
    return [r[0] for r in result.all()]


async def _process_org(db: Any, org_id: uuid.UUID) -> dict[str, int]:
    """Run dedup logic for a single organization.

    Args:
        db: Database session.
        org_id: Organization UUID.

    Returns:
        Dict with ``clusters``, ``entities_merged``,
        ``relationships_rewired`` counts.
    """
    clusters = await _find_duplicate_clusters(db, org_id)

    if not clusters:
        return {"clusters": 0, "entities_merged": 0, "relationships_rewired": 0}

    total_merged = 0
    total_rewired = 0

    for cluster in clusters:
        try:
            result = await _merge_cluster(db, org_id, cluster)
            total_merged += result["entities_merged"]
            total_rewired += result["relationships_rewired"]
        except Exception as exc:
            logger.error(
                "merge_duplicates.cluster_failed",
                org_id=str(org_id),
                entity_ids=[str(e["id"]) for e in cluster],
                error=str(exc),
            )

    await db.commit()

    logger.info(
        "merge_duplicates.org_completed",
        org_id=str(org_id),
        clusters=len(clusters),
        entities_merged=total_merged,
        relationships_rewired=total_rewired,
    )

    return {
        "clusters": len(clusters),
        "entities_merged": total_merged,
        "relationships_rewired": total_rewired,
    }


async def _find_duplicate_clusters(
    db: Any, org_id: uuid.UUID,
) -> list[list[dict[str, Any]]]:
    """Find duplicate entity clusters using exact and fuzzy matching.

    Two-phase approach:
    1. Exact match: ``LOWER(name)`` GROUP BY — entities with identical
       normalized names.
    2. Fuzzy match: ``pg_trgm similarity > threshold`` for remaining entities
       (excluding those already matched exactly).

    Args:
        db: Database session.
        org_id: Organization UUID.

    Returns:
        List of clusters, where each cluster is a list of entity dicts
        (``id``, ``name``, ``entity_type``, ``updated_at``).
    """
    clusters: list[list[dict[str, Any]]] = []
    seen_ids: set[str] = set()

    # ── Phase 1: Exact name matches (case-insensitive) ──────────────────────
    result = await db.execute(
        text("""
            SELECT LOWER(name) as normalized, array_agg(id ORDER BY name) as ids
            FROM graph_entities
            WHERE organization_id = :org_id AND is_merged = false
            GROUP BY LOWER(name)
            HAVING COUNT(*) > 1
        """),
        {"org_id": org_id},
    )
    for row in result.all():
        ids = list(row[1])
        entity_ids_str = [str(eid) for eid in ids]
        cluster = await _fetch_entity_details(db, org_id, entity_ids_str)
        clusters.append(cluster)
        seen_ids.update(entity_ids_str)

    # ── Phase 2: Fuzzy name matches via pg_trgm ─────────────────────────────
    # Fetch all remaining non-merged entities not in an exact cluster
    remaining_result = await db.execute(
        text("""
            SELECT id, name FROM graph_entities
            WHERE organization_id = :org_id
              AND is_merged = false
              AND id != ALL(CAST(:seen_ids AS uuid[]))
        """),
        {
            "org_id": org_id,
            "seen_ids": [uuid.UUID(eid) for eid in seen_ids] if seen_ids else [uuid.UUID(int=0)],
        },
    )
    remaining = {str(r[0]): r[1] for r in remaining_result.all()}

    # Build fuzzy clusters
    processed_fuzzy: set[str] = set()
    for eid_a, name_a in remaining.items():
        if eid_a in processed_fuzzy:
            continue

        fuzzy_matches = await db.execute(
            text("""
                SELECT id, name, similarity(LOWER(name), LOWER(:query)) as sim
                FROM graph_entities
                WHERE organization_id = :org_id
                  AND is_merged = false
                  AND id != :entity_id
                  AND similarity(LOWER(name), LOWER(:query)) > :threshold
                ORDER BY sim DESC
            """),
            {
                "query": name_a,
                "org_id": org_id,
                "entity_id": uuid.UUID(eid_a),
                "threshold": FUZZY_SIMILARITY_THRESHOLD,
            },
        )
        fuzzy_ids = [str(r[0]) for r in fuzzy_matches.all()]

        if fuzzy_ids:
            cluster_ids = [eid_a] + fuzzy_ids
            cluster_ids_str = [str(eid) for eid in cluster_ids]
            new_ids = set(cluster_ids_str) - processed_fuzzy
            if len(new_ids) >= 2:
                cluster = await _fetch_entity_details(
                    db, org_id, list(new_ids),
                )
                clusters.append(cluster)
            processed_fuzzy.update(cluster_ids_str)
        else:
            processed_fuzzy.add(eid_a)

    return clusters


async def _fetch_entity_details(
    db: Any, org_id: uuid.UUID, entity_ids: list[str],
) -> list[dict[str, Any]]:
    """Fetch entity details for a list of entity IDs.

    Args:
        db: Database session.
        org_id: Organization UUID.
        entity_ids: List of entity UUID strings.

    Returns:
        List of entity dicts with ``id``, ``name``, ``entity_type``,
        ``updated_at``.
    """
    if not entity_ids:
        return []

    result = await db.execute(
        text("""
            SELECT id, name, entity_type, updated_at
            FROM graph_entities
            WHERE organization_id = :org_id
              AND id = ANY(CAST(:entity_ids AS uuid[]))
        """),
        {
            "org_id": org_id,
            "entity_ids": [uuid.UUID(eid) for eid in entity_ids],
        },
    )
    return [
        {
            "id": str(r[0]),
            "name": r[1],
            "entity_type": r[2],
            "updated_at": r[3],
        }
        for r in result.all()
    ]


async def _merge_cluster(
    db: Any, org_id: uuid.UUID, cluster: list[dict[str, Any]],
) -> dict[str, int]:
    """Merge a single duplicate cluster.

    Steps:
    1. Select canonical entity (most relationships → most recently updated).
    2. Rewire all ``graph_relationships`` to canonical entity.
    3. Set ``is_merged = True`` on non-canonical entities.
    4. Write ``audit_log`` entry.

    Args:
        db: Database session.
        org_id: Organization UUID.
        cluster: List of entity dicts in this cluster (at least 2).

    Returns:
        Dict with ``entities_merged``, ``relationships_rewired`` counts.
    """
    if len(cluster) < 2:
        return {"entities_merged": 0, "relationships_rewired": 0}

    # ── 1. Select canonical entity ──────────────────────────────────────────
    canonical = await _select_canonical(db, org_id, cluster)

    duplicate_entities = [e for e in cluster if e["id"] != canonical["id"]]

    total_rewired = 0

    # ── 2. Rewire relationships ─────────────────────────────────────────────
    for dup in duplicate_entities:
        dup_id = uuid.UUID(dup["id"])
        canonical_id = uuid.UUID(canonical["id"])

        # Rewire source_id
        src_result = await db.execute(
            text("""
                UPDATE graph_relationships
                SET source_id = :canonical_id
                WHERE organization_id = :org_id
                  AND source_id = :dup_id
                  AND invalid_at IS NULL
            """),
            {
                "canonical_id": canonical_id,
                "org_id": org_id,
                "dup_id": dup_id,
            },
        )
        total_rewired += src_result.rowcount

        # Rewire target_id
        tgt_result = await db.execute(
            text("""
                UPDATE graph_relationships
                SET target_id = :canonical_id
                WHERE organization_id = :org_id
                  AND target_id = :dup_id
                  AND invalid_at IS NULL
            """),
            {
                "canonical_id": canonical_id,
                "org_id": org_id,
                "dup_id": dup_id,
            },
        )
        total_rewired += tgt_result.rowcount

        # Remove duplicate active relationships created by rewiring
        # Keep the first one (lowest ID) for each (source, target, type)
        await db.execute(
            text("""
                DELETE FROM graph_relationships g
                WHERE organization_id = :org_id
                  AND invalid_at IS NULL
                  AND g.id NOT IN (
                      SELECT MIN(id::text)::uuid
                      FROM graph_relationships
                      WHERE organization_id = :org_id
                        AND invalid_at IS NULL
                      GROUP BY source_id, target_id, relationship_type
                  )
            """),
            {"org_id": org_id},
        )

    # ── 3. Mark duplicates as merged ────────────────────────────────────────
    for dup in duplicate_entities:
        await db.execute(
            text("""
                UPDATE graph_entities
                SET is_merged = true, updated_at = now()
                WHERE id = :dup_id AND organization_id = :org_id
            """),
            {
                "dup_id": uuid.UUID(dup["id"]),
                "org_id": org_id,
            },
        )

    # ── 4. Write audit log ──────────────────────────────────────────────────
    before_snapshot = [
        {"id": e["id"], "name": e["name"], "entity_type": e["entity_type"]}
        for e in cluster
    ]
    after_snapshot = {
        "canonical_id": canonical["id"],
        "canonical_name": canonical["name"],
        "merged_ids": [e["id"] for e in duplicate_entities],
        "merged_names": [e["name"] for e in duplicate_entities],
    }

    from services.audit_log_service import AuditLogService
    audit_service = AuditLogService(db)
    await audit_service.log_action(
        organization_id=org_id,
        actor_id="system",
        actor_type="system",
        action="entity.merge",
        resource_type="graph_entities",
        resource_id=canonical["id"],
        details={
            "action": "merge_duplicate_entities",
            "before": before_snapshot,
            "after": after_snapshot,
            "cluster_size": len(cluster),
            "relationships_rewired": total_rewired,
        },
        ip_address=None,
    )

    logger.info(
        "merge_duplicates.cluster_merged",
        org_id=str(org_id),
        canonical_id=canonical["id"],
        canonical_name=canonical["name"],
        merged_count=len(duplicate_entities),
        relationships_rewired=total_rewired,
    )

    return {
        "entities_merged": len(duplicate_entities),
        "relationships_rewired": total_rewired,
    }


async def _select_canonical(
    db: Any, org_id: uuid.UUID, cluster: list[dict[str, Any]],
) -> dict[str, Any]:
    """Select the canonical entity from a duplicate cluster.

    Heuristics (applied in order):
    1. Entity with the most active relationships (source + target) wins.
    2. Tie-break: most recently updated.

    Args:
        db: Database session.
        org_id: Organization UUID.
        cluster: List of entity dicts in the cluster.

    Returns:
        The canonical entity dict.
    """
    if len(cluster) == 1:
        return cluster[0]

    # Fetch relationship counts for all entities in the cluster
    entity_ids = [uuid.UUID(e["id"]) for e in cluster]
    result = await db.execute(
        text("""
            SELECT entity_id, COUNT(*) as rel_count FROM (
                SELECT source_id as entity_id
                FROM graph_relationships
                WHERE organization_id = :org_id
                  AND source_id = ANY(CAST(:entity_ids AS uuid[]))
                  AND invalid_at IS NULL
                UNION ALL
                SELECT target_id as entity_id
                FROM graph_relationships
                WHERE organization_id = :org_id
                  AND target_id = ANY(CAST(:entity_ids AS uuid[]))
                  AND invalid_at IS NULL
            ) AS rels
            GROUP BY entity_id
        """),
        {
            "org_id": org_id,
            "entity_ids": entity_ids,
        },
    )
    rel_counts: dict[str, int] = {
        str(r[0]): r[1] for r in result.all()
    }

    # Sort: most relationships first, then most recently updated
    def _sort_key(e: dict[str, Any]) -> tuple[int, datetime]:
        count = rel_counts.get(e["id"], 0)
        updated = e["updated_at"] or datetime.min.replace(tzinfo=timezone.utc)
        return (count, updated)

    sorted_cluster = sorted(cluster, key=_sort_key, reverse=True)
    return sorted_cluster[0]
