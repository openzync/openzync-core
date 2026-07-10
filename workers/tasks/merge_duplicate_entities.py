"""Entity merge dedup worker — ARQ task for weekly scheduled entity deduplication.

Detects and merges duplicate entities within an organization's knowledge graph
using exact name matching and backend fuzzy string similarity.

Pipeline:
    1. Query all non-merged entities for a project via the graph backend.
    2. Exact match: group by ``LOWER(name)`` — detect entities with identical
       names (case-insensitive).
    3. Fuzzy match: ``backend.bulk_search_entities()`` — delegate fuzzy search
       to the graph backend (pg_trgm for PostgreSQL, etc.).
    4. For each duplicate cluster:
       a. Select canonical entity (most recently created).
       b. ``backend.merge_entities()`` — ONE atomic call per cluster that
          rewires relationships, deletes duplicates, and marks merged
          entities.
       c. Write an ``audit_log`` entry with before/after snapshot.
    5. 7-day recovery window: soft-delete via ``is_merged`` flag, not hard
       delete.

All raw SQL has been removed — every graph operation goes through the
``GraphBackend`` ABC.  See Wave 3c of the migration.

Bitmask:
    Does NOT set an enrichment bit — this is a data maintenance task, not
    an episode enrichment step.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.organization import Organization
from models.project import Project
from packages.graph_backend.interface import GraphBackend
from workers.backend import resolve_graph_backend
from workers.tasks.base import with_retry

logger = structlog.get_logger()

# ── Constants ──────────────────────────────────────────────────────────────────

FUZZY_SIMILARITY_THRESHOLD: float = 0.85
"""Minimum similarity score for fuzzy duplicate detection."""

MERGE_BATCH_SIZE: int = 100
"""Number of entity clusters to process in a single DB transaction."""

_BULK_SEARCH_LIMIT: int = 100
"""Maximum fuzzy-matching candidates to return per query."""


# ── Public ARQ task (decorated with retry) ─────────────────────────────────────


@with_retry(max_retries=2, base_delay_s=5.0)
async def merge_duplicate_entities(
    ctx: dict,
    org_id: str | None = None,
) -> dict:
    """Merge duplicate entities for an organization.

    Can be called:
    - Without ``org_id`` (scheduled weekly run): processes all eligible orgs.
    - With ``org_id`` (manual trigger): processes a single org.

    Args:
        ctx: ARQ worker context (passed to ``resolve_graph_backend``).
        org_id: Optional org UUID to process (processes all if ``None``).

    Returns:
        Dict with ``status``, ``orgs_processed``, ``orgs_failed``,
        ``clusters_merged``, ``entities_merged``,
        ``relationships_rewired``.
    """
    from core.config import settings
    from core.db import get_async_session

    _engine = ctx.get("db_engine") if isinstance(ctx, dict) else None
    if _engine is None:
        from core.db import init_db_engine

        _engine = init_db_engine(
            str(settings.DATABASE_URL),
            pool_size=5,
            max_overflow=2,
        )
        _own_engine = True
    else:
        _own_engine = False
    _session_factory = ctx.get("db_session_factory") if isinstance(ctx, dict) else None
    if _session_factory is None:
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

            org_errors: list[str] = []
            total_clusters = 0
            total_merged = 0
            total_rewired = 0

            for current_org_id in org_ids:
                try:
                    clusters = await _process_org(
                        ctx, db, current_org_id,
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
                    org_errors.append(str(current_org_id))

            if org_errors and len(org_errors) == len(org_ids):
                raise RuntimeError(
                    f"All {len(org_ids)} orgs failed to merge duplicates: {', '.join(org_errors)}"
                )

            logger.info(
                "merge_duplicates.completed",
                orgs_processed=len(org_ids),
                orgs_failed=len(org_errors),
                clusters_merged=total_clusters,
                entities_merged=total_merged,
                relationships_rewired=total_rewired,
            )

            return {
                "status": "completed" if not org_errors else "partial",
                "orgs_processed": len(org_ids),
                "orgs_failed": len(org_errors),
                "clusters_merged": total_clusters,
                "entities_merged": total_merged,
                "relationships_rewired": total_rewired,
            }

    finally:
        if _own_engine:
            await _engine.dispose()


# ── Private helpers ────────────────────────────────────────────────────────────


async def _find_eligible_orgs(db: AsyncSession) -> list[uuid.UUID]:
    """Return all organization IDs for duplicate-entity processing.

    Returns ALL orgs — per-org backend resolution and entity-count
    filtering happens in ``_process_org`` / ``_process_project`` via
    ``backend.get_all_entities()``.  Direct ``GraphEntity`` ORM queries
    are incorrect for non-Postgres backends and silently return zero
    results when entities live in SurrealDB or FalkorDB.

    Args:
        db: Database session.

    Returns:
        List of all organization UUIDs.
    """
    result = await db.execute(select(Organization.id))
    return [r[0] for r in result.all()]


async def _process_org(
    ctx: dict,
    db: AsyncSession,
    org_id: uuid.UUID,
) -> dict[str, int]:
    """Run dedup logic for a single organization.

    Resolves the per-org graph backend, discovers projects with graph
    data, and processes each project independently.

    Args:
        ctx: ARQ worker context (passed to ``resolve_graph_backend``).
        db: Database session.
        org_id: Organization UUID.

    Returns:
        Dict with ``clusters``, ``entities_merged``,
        ``relationships_rewired`` counts.
    """
    backend = await resolve_graph_backend(ctx, org_id, db)
    if backend is None:
        logger.warning(
            "merge_duplicates.graph_disabled",
            org_id=str(org_id),
        )
        return {"clusters": 0, "entities_merged": 0, "relationships_rewired": 0}

    # Discover projects for this org
    result = await db.execute(
        select(Project.id).where(
            Project.organization_id == org_id,
            Project.is_archived == False,
        )
    )
    project_ids = [r[0] for r in result.all()]

    if not project_ids:
        return {"clusters": 0, "entities_merged": 0, "relationships_rewired": 0}

    cluster_errors: list[str] = []
    total_clusters = 0
    total_merged = 0
    total_rewired = 0

    for project_id in project_ids:
        try:
            result_counts = await _process_project(
                db, backend, org_id, project_id,
            )
            total_clusters += result_counts["clusters"]
            total_merged += result_counts["entities_merged"]
            total_rewired += result_counts["relationships_rewired"]
        except Exception as exc:
            logger.error(
                "merge_duplicates.project_failed",
                org_id=str(org_id),
                project_id=str(project_id),
                error=str(exc),
            )
            cluster_errors.append(str(project_id))

    if cluster_errors and len(cluster_errors) == len(project_ids):
        raise RuntimeError(
            f"All {len(project_ids)} projects failed to merge duplicates "
            f"for org {org_id}: {', '.join(cluster_errors)}"
        )

    await db.commit()

    logger.info(
        "merge_duplicates.org_completed",
        org_id=str(org_id),
        clusters=total_clusters,
        projects_failed=len(cluster_errors),
        entities_merged=total_merged,
        relationships_rewired=total_rewired,
    )

    return {
        "clusters": total_clusters,
        "clusters_failed": len(cluster_errors),
        "entities_merged": total_merged,
        "relationships_rewired": total_rewired,
    }


async def _process_project(
    db: AsyncSession,
    backend: GraphBackend,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
) -> dict[str, int]:
    """Run dedup logic for a single project within an org.

    Args:
        db: Database session (for audit log).
        backend: Graph backend for this org.
        org_id: Organization UUID.
        project_id: Project UUID.

    Returns:
        Dict with ``clusters``, ``entities_merged``,
        ``relationships_rewired`` counts.
    """
    clusters = await _find_duplicate_clusters(backend, org_id, project_id)

    if not clusters:
        return {"clusters": 0, "entities_merged": 0, "relationships_rewired": 0}

    cluster_errors: list[str] = []
    total_merged = 0
    total_rewired = 0

    for cluster in clusters:
        try:
            result = await _merge_cluster(db, backend, org_id, project_id, cluster)
            total_merged += result["entities_merged"]
            total_rewired += result["relationships_rewired"]
        except Exception as exc:
            logger.error(
                "merge_duplicates.cluster_failed",
                org_id=str(org_id),
                project_id=str(project_id),
                entity_ids=[str(e["id"]) for e in cluster],
                error=str(exc),
            )
            cluster_errors.append(str(cluster[0].get("id", "unknown")))

    if cluster_errors and len(cluster_errors) == len(clusters):
        raise RuntimeError(
            f"All {len(clusters)} clusters failed to merge for project "
            f"{project_id} in org {org_id}: {', '.join(cluster_errors)}"
        )

    return {
        "clusters": len(clusters),
        "clusters_failed": len(cluster_errors),
        "entities_merged": total_merged,
        "relationships_rewired": total_rewired,
    }


async def _find_duplicate_clusters(
    backend: GraphBackend,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
) -> list[list[dict[str, Any]]]:
    """Find duplicate entity clusters using exact and fuzzy matching.

    Two-phase approach:
    1. **Exact match**: Group by ``LOWER(name)`` in Python — entities with
       identical normalized names.
    2. **Fuzzy match**: ``backend.bulk_search_entities()`` for each remaining
       entity, feeding its name as the query.

    All data comes from ``backend.get_all_entities()`` — no raw SQL.

    Args:
        backend: Graph backend for this org.
        org_id: Organization UUID.
        project_id: Project UUID.

    Returns:
        List of clusters, where each cluster is a list of entity dicts
        (``id``, ``name``, ``entity_type``, ``created_at``).
    """
    entities = await backend.get_all_entities(
        org_id, project_id, include_merged=False,
    )

    if len(entities) < 2:
        return []

    clusters: list[list[dict[str, Any]]] = []
    seen_ids: set[str] = set()

    # ── Phase 1: Exact name matches (case-insensitive) ──────────────────────
    name_groups: dict[str, list[dict[str, Any]]] = {}
    for entity in entities:
        name_lower = entity["name"].lower().strip()
        name_groups.setdefault(name_lower, []).append(entity)

    for name_lower, group in name_groups.items():
        if len(group) > 1:
            clusters.append(group)
            for e in group:
                seen_ids.add(e["id"])

    # ── Phase 2: Fuzzy name matches via backend ─────────────────────────────
    remaining = [e for e in entities if e["id"] not in seen_ids]
    processed_fuzzy: set[str] = set()

    for entity in remaining:
        eid = entity["id"]
        if eid in processed_fuzzy:
            continue

        fuzzy_results = await backend.bulk_search_entities(
            org_id=org_id,
            project_id=project_id,
            query=entity["name"],
            fuzzy_threshold=FUZZY_SIMILARITY_THRESHOLD,
            limit=_BULK_SEARCH_LIMIT,
        )

        # Exclude self and entities already locked into exact-match clusters
        fuzzy_ids = [
            r["id"]
            for r in fuzzy_results
            if r["id"] != eid and r["id"] not in seen_ids
        ]

        if fuzzy_ids:
            cluster_ids = [eid] + fuzzy_ids
            cluster = [entity] + [
                e for e in entities if e["id"] in fuzzy_ids
            ]
            clusters.append(cluster)
            processed_fuzzy.update(cluster_ids)
            seen_ids.update(cluster_ids)
        else:
            processed_fuzzy.add(eid)

    return clusters


async def _merge_cluster(
    db: AsyncSession,
    backend: GraphBackend,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    cluster: list[dict[str, Any]],
) -> dict[str, int]:
    """Merge a single duplicate cluster.

    Steps:
    1. Select canonical entity (most recent ``created_at``).
    2. ``backend.merge_entities()`` — atomic rewire + dedup + soft-delete.
    3. Write ``audit_log`` entry.

    Args:
        db: Database session (for audit log).
        backend: Graph backend for this org.
        org_id: Organization UUID.
        project_id: Project UUID.
        cluster: List of entity dicts in this cluster (at least 2).

    Returns:
        Dict with ``entities_merged``, ``relationships_rewired`` counts.
    """
    if len(cluster) < 2:
        return {"entities_merged": 0, "relationships_rewired": 0}

    # ── 1. Select canonical entity ──────────────────────────────────────────
    canonical = _select_canonical(cluster)
    duplicate_entities = [e for e in cluster if e["id"] != canonical["id"]]

    # ── 2. Atomic merge via backend ─────────────────────────────────────────
    merge_result = await backend.merge_entities(
        org_id=org_id,
        project_id=project_id,
        canonical_id=uuid.UUID(canonical["id"]),
        merged_ids=[uuid.UUID(e["id"]) for e in duplicate_entities],
    )

    rewired_count = merge_result["rewired_count"]
    deleted_count = merge_result["deleted_count"]
    merged_count = merge_result["merged_count"]

    # ── 3. Write audit log ──────────────────────────────────────────────────
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
            "relationships_rewired": rewired_count + deleted_count,
        },
        ip_address=None,
    )

    logger.info(
        "merge_duplicates.cluster_merged",
        org_id=str(org_id),
        project_id=str(project_id),
        canonical_id=canonical["id"],
        canonical_name=canonical["name"],
        merged_count=merged_count,
        relationships_rewired=rewired_count,
        duplicates_deleted=deleted_count,
    )

    return {
        "entities_merged": merged_count,
        "relationships_rewired": rewired_count + deleted_count,
    }


def _select_canonical(cluster: list[dict[str, Any]]) -> dict[str, Any]:
    """Select the canonical entity from a duplicate cluster.

    Heuristic: most recently ``created_at`` wins.  This is a client-side
    choice — the actual rewire/dedup logic is handled atomically by
    ``backend.merge_entities()``.

    .. note::
        The original heuristic used relationship counts + ``updated_at``,
        but those require extra DB calls that ``get_all_entities()`` does
        not provide.  ``created_at`` is a reasonable proxy: the most recent
        entity in a duplicate cluster was likely created with the most
        complete/up-to-date information.

    Args:
        cluster: List of entity dicts in the cluster.

    Returns:
        The canonical entity dict.
    """
    if len(cluster) == 1:
        return cluster[0]

    def _sort_key(entity: dict[str, Any]) -> str:
        return entity.get("created_at") or ""

    sorted_cluster = sorted(cluster, key=_sort_key, reverse=True)
    return sorted_cluster[0]
