"""Entity extraction helpers for the combined enrichment worker.

Exports ``process_entities_output``, which the combined ``enrich_episode``
worker calls to persist LLM-extracted entities and relationships.  The
standalone ``extract_entities`` ARQ task was retired in favour of
``enrich_episode``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.graph_backend.interface import GraphBackend
    from repositories.entity_repository import EntityRepository
    from repositories.episode_repository import EpisodeRepository
    from schemas.llm_outputs import EntityExtractionOutput

import structlog

from workers.tasks.base import ENRICHMENT_ENTITIES

logger = structlog.get_logger()


# ── Shared post-processing (exported for combined worker) ──────────────────


async def process_entities_output(
    db: AsyncSession,
    graph_backend: GraphBackend,
    entity_repo: EntityRepository,
    episode_repo: EpisodeRepository | None,
    org_id: str,
    episode_id: str,
    project_id: str,
    parsed: EntityExtractionOutput,
    entity_types: list[str],
) -> dict[str, str]:
    """Process and persist entity extraction output from LLM.

    Filters pronouns, validates entity types, upserts entities and
    relationships in the graph backend, and links entities to the episode.
    Does NOT manage transactions — caller is responsible for commit/rollback.

    Args:
        db: Active database session.
        graph_backend: Resolved graph backend for entity/relationship CRUD.
        entity_repo: Entity repository for upsert operations.
        episode_repo: Optional episode repository for setting enrichment bits.
            If None, bits are not set (caller manages this).
        org_id: Organization UUID string.
        episode_id: Episode UUID string.
        project_id: Project UUID string.
        parsed: The validated EntityExtractionOutput from the LLM.
        entity_types: List of allowed entity type strings from org config.

    Returns:
        A dict mapping entity names to their UUID strings, for use by
        downstream fact processing.

    Raises:
        Various DB/graph errors on persistence failure.
    """
    # ── 1. Convert parsed output to mutable dicts ──────────────────────────
    entities: list[dict] = [e.model_dump() for e in parsed.entities]
    relationships: list[dict] = [r.model_dump() for r in parsed.relationships]

    # ── Pronoun filter — skip entities that are pronouns or common    ───
    #    misspellings.  Pronouns like "I", "me", "my" should never be
    #    persisted as graph entities — they are resolved to the speaker
    #    during fact extraction (see _match_entity in extract_facts.py).
    _pronoun_skip_names: set[str] = {
        # First‑person
        "i",
        "me",
        "my",
        "mine",
        "myself",
        "we",
        "us",
        "our",
        "ours",
        "ourselves",
        # Second‑person
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
        # Third‑person
        "he",
        "him",
        "his",
        "himself",
        "she",
        "her",
        "hers",
        "herself",
        "it",
        "its",
        "itself",
        "they",
        "them",
        "their",
        "theirs",
        "themselves",
        # Ambiguous / filler
        "this",
        "that",
        "these",
        "those",
        "someone",
        "somebody",
        "everyone",
        "everybody",
        "nobody",
        "anyone",
        "anybody",
        # Common misspellings (observed: "shhe")
        "shhe",
        "hhe",
        "thei",
        "theyr",
        "thereselves",
        # Questions / catch‑all that leak through extraction
        "what",
        "who",
        "whom",
        "whose",
        "which",
    }

    filtered_entities: list[dict] = []
    for entity in entities:
        name = (entity.get("name") or "").strip()
        if not name:
            logger.warning(
                "entity_extraction.entity_without_name_skipped",
                episode_id=episode_id,
            )
            continue
        if name.lower() in _pronoun_skip_names:
            logger.info(
                "entity_extraction.pronoun_skipped",
                episode_id=episode_id,
                name=name,
            )
            continue
        filtered_entities.append(entity)
    entities = filtered_entities

    # Clean relationships that reference skipped pronouns
    clean_relationships: list[dict] = []
    for rel in relationships:
        subj = (rel.get("subject") or "").strip()
        obj = (rel.get("object") or "").strip()
        if not subj or not obj:
            logger.warning(
                "entity_extraction.relationship_without_subject_or_object_skipped",
                episode_id=episode_id,
                subject=subj,
                predicate=rel.get("predicate"),
                object=obj,
            )
            continue
        if subj.lower() in _pronoun_skip_names or obj.lower() in _pronoun_skip_names:
            logger.info(
                "entity_extraction.relationship_pronoun_skipped",
                episode_id=episode_id,
                subject=subj,
                predicate=rel.get("predicate"),
                object=obj,
            )
            continue
        clean_relationships.append(rel)
    relationships = clean_relationships

    if not entities and not relationships:
        logger.info("entity_extraction.empty", episode_id=episode_id)

    logger.info(
        "entity_extraction.parsed",
        episode_id=episode_id,
        entities=len(entities),
        relationships=len(relationships),
    )

    # ── 2. Validate entity types against allowed ontology ────────────────
    allowed_types: set[str] = set(entity_types) | {"Custom"}
    for entity in entities:
        raw_type = entity.get("type")
        if not raw_type or raw_type not in allowed_types:
            logger.warning(
                "entity_extraction.invalid_type",
                episode_id=episode_id,
                name=entity.get("name"),
                original_type=raw_type,
                reassigned_to="Custom",
                allowed=sorted(allowed_types),
            )
            entity["type"] = "Custom"

    # ── 3. Upsert entities to graph ────────────────────────────────────
    name_to_node: dict[str, dict] = {}

    # ── Failure counters for enrichment-bit gating ─────────────────────
    # Note: entity failures now raise GraphBackendUnavailableError and trigger
    # retry via @with_retry. Only relationship failures (entity-not-found in
    # graph) are counted here since they are non-fatal edge cases.
    relationship_failure_count: int = 0
    relationship_skip_count: int = 0

    for entity in entities:
        entity_name = entity.get("name", "")
        entity_type = entity.get("type", "Custom")
        mentions: list[str] = entity.get("mentions", [])

        # ── Normalize entity name casing ────────────────────────────
        normalized_name = entity_name
        if mentions:
            first_mention = mentions[0].strip()
            if first_mention and len(first_mention) > 1:
                normalized_name = first_mention
        elif entity_name and entity_name.islower():
            normalized_name = entity_name.capitalize()

        summary = (
            f"{normalized_name} ({entity_type}) — "
            f"mentioned as: {', '.join(set(mentions))}"
            if mentions
            else f"{normalized_name} ({entity_type})"
        )

        node = await entity_repo.upsert_entity(
            org_id=uuid.UUID(org_id),
            project_id=uuid.UUID(project_id),
            name=normalized_name,
            entity_type=entity_type,
            summary=summary,
        )
        name_to_node[normalized_name] = node
        # Also key by original name as fallback for callers
        # that might use the raw LLM output
        if normalized_name != entity_name:
            name_to_node[entity_name] = node

    # ── 4. Upsert relationships to graph ─────────────────────────────
    for rel in relationships:
        subject = rel.get("subject", "")
        predicate = rel.get("predicate", "")
        obj = rel.get("object", "")

        if not subject or not predicate or not obj:
            continue

        # ── On-the-fly entity recovery pass ────────────────────────
        # If the LLM included a name in a relationship but didn't
        # declare it in the entities array, auto-create it as a
        # "Custom" type entity so the graph edge is not lost.
        for name in (subject, obj):
            if name not in name_to_node:
                fallback_node = await entity_repo.upsert_entity(
                    org_id=uuid.UUID(org_id),
                    project_id=uuid.UUID(project_id),
                    name=name,
                    entity_type="Custom",
                    summary=(
                        f"Auto-created from relationship: "
                        f"{subject} {predicate} {obj}"
                    ),
                )
                name_to_node[name] = fallback_node
                logger.info(
                    "entity_extraction.relationship_entity_recovered",
                    episode_id=episode_id,
                    entity_name=name,
                    relationship=f"{subject} {predicate} {obj}",
                )

        if subject in name_to_node and obj in name_to_node:
            result = await entity_repo.upsert_relationship(
                subject=subject,
                predicate=predicate,
                obj=obj,
                org_id=uuid.UUID(org_id),
                project_id=uuid.UUID(project_id),
            )
            if result is None:
                relationship_failure_count += 1
        else:
            relationship_skip_count += 1
            logger.warning(
                "entity_extraction.relationship_skipped_missing_entity",
                episode_id=episode_id,
                subject=subject,
                predicate=predicate,
                object=obj,
                subject_in_graph=subject in name_to_node,
                object_in_graph=obj in name_to_node,
            )

    # ── 5. Link entities to this episode via graph backend ──────────
    episode_uuid = uuid.UUID(episode_id)
    for entity_node in name_to_node.values():
        await graph_backend.link_entity_to_episode(
            org_id=uuid.UUID(org_id),
            project_id=uuid.UUID(project_id),
            episode_id=episode_uuid,
            entity_id=uuid.UUID(entity_node["id"]),
        )

    # ── 6. Set enrichment_status bit 0 (if repo provided) ──────────
    if episode_repo is not None:
        await episode_repo.apply_enrichment_bits(
            uuid.UUID(episode_id), ENRICHMENT_ENTITIES
        )

    await db.flush()

    logger.info(
        "entity_extraction.persisted",
        episode_id=episode_id,
        org_id=org_id,
        project_id=project_id,
        entity_count=len(name_to_node),
        relationship_failure_count=relationship_failure_count,
        relationship_skip_count=relationship_skip_count,
    )

    return {name: node["id"] for name, node in name_to_node.items()}


