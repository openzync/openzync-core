"""Single episode enrichment task — replaces 4 separate LLM calls.

This worker replaces ``classify_dialog``, ``extract_entities``, ``extract_facts``,
and ``extract_structured`` with a single LLM call that produces all outputs in
one pass.  Each enrichment section is processed independently with savepoint
isolation, so partial failures don't lose completed work.

Bitmask:
    Sets ``episodes.enrichment_status`` bits 0, 2, 4, 5, 8
    (``ENRICHMENT_ENTITIES | ENRICHMENT_FACTS | ENRICHMENT_CLASSIFICATION |
     ENRICHMENT_STRUCTURED_EXTRACTION | LLM_INVALIDATION_BIT``) on success.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, cast

import structlog
from sqlalchemy import select, text

from core.exceptions import EpisodeNotFoundError, GraphBackendUnavailableError
from services.worker.prompt_renderer import build_enrichment_prompt, render_prompt
from workers.tasks.base import (
    ENRICHMENT_CLASSIFICATION,
    ENRICHMENT_ENTITIES,
    ENRICHMENT_FACTS,
    ENRICHMENT_STRUCTURED_EXTRACTION,
    LLM_INVALIDATION_BIT,
    with_retry,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from models.fact import Fact
    from schemas.organization_config import OrgConfigBase

logger = structlog.get_logger()


class PartialEnrichmentError(Exception):
    """Raised when one or more enrichment sections fail.

    The ``successful_bits`` attribute tracks which sections completed,
    so retries can skip already-done work.
    """

    def __init__(self, message: str, successful_bits: int = 0) -> None:
        self.successful_bits = successful_bits
        super().__init__(message)


async def _resolve_existing_fact_refs(
    db: AsyncSession, existing_facts: list[dict[str, object]]
) -> dict[str, Fact]:
    """Map prompt E-references to the Fact rows the LLM was shown.

    The render context's ``existing_facts`` dicts (from
    :meth:`FactRepository.list_by_session`) carry the row ids; a single
    query avoids N+1 and returns real ORM rows for the invalidation
    service (which reads ``valid_to``/entity columns off the ORM objects).

    Args:
        db: Active session, RLS-scoped to the org.
        existing_facts: The exact list rendered into the prompt's
            EXISTING FACTS table, in table order.

    Returns:
        ``{"E<1-based index>": Fact}`` for every rendered table row.

    Raises:
        RuntimeError: If a rendered row is missing from the DB — a
            data-integrity anomaly that must not be silently skipped.
    """
    from models.fact import Fact  # noqa: PLC0415 — lazy import

    ids = {uuid.UUID(str(f["id"])) for f in existing_facts if f.get("id")}
    if not ids:
        return {}

    result = await db.execute(select(Fact).where(Fact.id.in_(ids)))
    by_id = {fact.id: fact for fact in result.scalars().all()}

    ref_to_fact: dict[str, Fact] = {}
    for index, fact_dict in enumerate(existing_facts, start=1):
        fact_id = uuid.UUID(str(fact_dict["id"]))
        fact_row = by_id.get(fact_id)
        if fact_row is None:
            raise RuntimeError(
                f"existing fact {fact_id} rendered as E{index} "
                "disappeared before invalidation"
            )
        ref_to_fact[f"E{index}"] = fact_row
    return ref_to_fact


@with_retry(max_retries=3, base_delay_s=2.0)
async def enrich_episode(
    ctx: object,
    episode_id: str,
    org_id: str,
    project_id: str,
    content: str,
    session_id: str | None = None,
    trace_id: str = "",
    metadata: dict | None = None,
    role: str = "user",
) -> None:
    """Single enrichment pass: classify + extract entities/facts/structured.

    Pipeline:
        1. Open session, set RLS context.
        2. Check idempotency — skip if all 5 LLM bits already set.
        3. Resolve ``user_id`` from the episode record.
        4. Render the ``enrich_episode_v1.jinja2`` prompt with auto-injected
           context (entities, facts, schemas, history, …).
        5. Resolve LLM backend from org config.
        6. Single LLM call with ``CombinedLLMOutput`` as ``response_model``.
        7. Process each enrichment section in an independent savepoint:
           - Classification  (bit 4)
           - Entities        (bit 0)
           - Facts           (bit 2) + LLM-driven invalidations (bit 8)
           - Structured      (bit 5)
        8. Commit all successful savepoints.
        9. Raise ``PartialEnrichmentError`` if any section failed.

    Each section is independently rolled back on failure, so completed
    sections are never lost.  On ``PartialEnrichmentError`` the ARQ retry
    mechanism re-runs; the idempotency check at the top skips already-set
    bits.

    Args:
        ctx: ARQ worker context (``db_session_factory``, ``redis``).
        episode_id: UUID of the source episode (string, from ARQ).
        org_id: UUID of the owning organization.
        project_id: UUID of the project for project scoping.
        content: The message text to enrich.
        session_id: UUID of the session (for FK and context assembly).
        trace_id: Request trace ID for end-to-end correlation.
        metadata: Optional metadata dict from the episode.
        role: Message role (default ``"user"``; passed for compatibility
            with the memory service enqueue but not used internally).

    Raises:
        EpisodeNotFoundError: If the episode does not exist.
        PartialEnrichmentError: If one or more sections failed after
            retry exhaustion (carries ``successful_bits`` for partial retry).
        Exception: Re-raises the last unexpected error after retry exhaustion.
    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    # Lazy imports — ARQ workers run in a separate process.
    from core.config import settings
    from core.db import get_async_session
    from core.llm import build_cache_config, resolve_backend
    from core.org_config import get_org_config
    from repositories.entity_repository import EntityRepository
    from repositories.episode_blob_repository import EpisodeBlobRepository
    from repositories.episode_repository import EpisodeRepository
    from repositories.fact_repository import FactRepository
    from schemas.llm_outputs import (
        CombinedLLMOutput,
        EntityExtractionOutput,
        FactExtractionOutput,
    )
    from services.usage_service import record_llm_usage
    from workers.backend import resolve_graph_backend
    from workers.tasks.classify_dialog import process_classification_output
    from workers.tasks.extract_entities import process_entities_output
    from workers.tasks.extract_facts import process_facts_output
    from workers.tasks.extract_structured import process_structured_output

    metadata = metadata or {}

    log = logger.bind(
        episode_id=episode_id,
        org_id=org_id,
        project_id=project_id,
        trace_id=trace_id,
    )

    # ── Resolve DB engine ─────────────────────────────────────────────────
    engine = ctx.get("db_engine") if isinstance(ctx, dict) else None
    if engine is None:
        from core.db import init_db_engine

        engine = init_db_engine(str(settings.DATABASE_URL), pool_size=2, max_overflow=1)
        _own_engine = True
    else:
        _own_engine = False
    session_factory = ctx.get("db_session_factory") if isinstance(ctx, dict) else None
    if session_factory is None:
        session_factory = get_async_session(engine)

    arq_redis = ctx.get("redis") if isinstance(ctx, dict) else None
    bao_client = ctx.get("openbao_client") if isinstance(ctx, dict) else None

    try:
        async with session_factory() as db:
            # ── 1-2. Set RLS context + idempotency check ─────────────────
            await db.execute(
                text("SELECT set_config('app.org_id', :oid, true)"),
                {"oid": org_id},
            )

            episode_repo = EpisodeRepository(db)
            episode = await episode_repo.get_by_id_for_update(uuid.UUID(episode_id))
            if episode is None:
                raise EpisodeNotFoundError(
                    message=f"Episode {episode_id} not found for enrichment.",
                    detail={"episode_id": episode_id},
                )

            llm_bits = (
                ENRICHMENT_ENTITIES
                | ENRICHMENT_FACTS
                | ENRICHMENT_CLASSIFICATION
                | ENRICHMENT_STRUCTURED_EXTRACTION
                | LLM_INVALIDATION_BIT
            )
            if episode.enrichment_status & llm_bits == llm_bits:
                log.info("enrich_episode.already_done")
                return

            user_id: str = str(episode.user_id)

            # ── 3. Render prompt with auto-injected context ──────────────
            try:
                system_prompt, prompt_ctx = await render_prompt(
                    "enrich_episode",
                    org_id=org_id,
                    episode_id=episode_id,
                    session_id=session_id,
                    user_id=user_id,
                    project_id=project_id,
                    db_session_factory=session_factory,
                    return_context=True,
                    metadata=metadata,
                )
                prompt = build_enrichment_prompt(system_prompt, prompt_ctx)
            except Exception:
                log.exception("enrich_episode.prompt_render_failed")
                raise

            blob_count: int = 0
            # ── 3b. Append blob extracted text to prompt (best-effort) ──
            try:
                blob_repo = EpisodeBlobRepository(db)
                blobs = await blob_repo.get_by_episode(uuid.UUID(episode_id))
                blob_texts = [b for b in blobs if b.extracted_text]
                if blob_texts:
                    blob_parts: list[str] = [
                        "\n\n## ATTACHED FILE CONTENTS\n"
                    ]
                    for b in blob_texts:
                        blob_parts.append(
                            f"### {b.file_name} ({b.mime_type})\n"
                            f"{b.extracted_text}\n"
                        )
                    prompt += "".join(blob_parts)
                    blob_count = len(blob_texts)
                    log.info(
                        "enrich_episode.blob_text_appended",
                        extra={
                            "blob_count": blob_count,
                            "total_blobs": len(blobs),
                        },
                    )
            except Exception:
                # Non-critical: blob text is optional context.
                # If loading blob records or extracted_text hasn't been
                # populated yet, enrichment proceeds with just the
                # conversation text.
                log.warning(
                    "enrich_episode.blob_text_fetch_failed",
                    exc_info=True,
                )

            # ── 4. Fetch per-organization config ────────────────────────
            org_cfg: OrgConfigBase | None = None
            llm_config_dict: dict | None = None
            try:
                if bao_client is not None:
                    org_cfg = await get_org_config(
                        uuid.UUID(org_id), redis=None, bao_client=bao_client
                    )
                else:
                    from core.config import BootstrapSettings
                    from core.openbao import OpenBaoClient

                    bootstrap = BootstrapSettings()
                    async with OpenBaoClient(
                        bootstrap.OPENBAO_ADDR,
                        bootstrap.OPENBAO_ROLE_ID,
                        bootstrap.OPENBAO_SECRET_ID,
                        timeout=10.0,
                    ) as _tmp_bao:
                        org_cfg = await get_org_config(
                            uuid.UUID(org_id), redis=None, bao_client=_tmp_bao
                        )
                llm_config_dict = org_cfg.to_llm_config_dict()
            except Exception:
                log.warning(
                    "enrich_episode.org_config_fetch_failed",
                    exc_info=True,
                )

            # ── 5. Single LLM call ──────────────────────────────────────
            log.info("enrich_episode.llm_call_start")
            try:
                llm = await resolve_backend(org_config=llm_config_dict)
                start = time.monotonic()
                response = await llm.chat(
                    [
                        {
                            "role": "system",
                            "content": "You are an episode enrichment system.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    response_model=CombinedLLMOutput,
                    temperature=0.0,
                    max_tokens=8192,
                    cache_config=build_cache_config(org_config=llm_config_dict),
                )
            except Exception:
                log.exception("enrich_episode.llm_call_failed")
                raise

            # Record usage in the same transaction as the enrichment work —
            # commits atomically with the savepoints below.
            await record_llm_usage(
                session=db,
                organization_id=uuid.UUID(org_id),
                model=response.model,
                task_type="enrich_episode",
                usage=response.usage,
                duration_ms=round((time.monotonic() - start) * 1000),
            )

            # chat() was called with response_model=CombinedLLMOutput, so a
            # successful call guarantees validated_data is a CombinedLLMOutput.
            parsed = cast("CombinedLLMOutput", response.validated_data)
            log.info(
                "enrich_episode.llm_call_done",
                has_classification=(
                    parsed.classification.intent is not None
                    or parsed.classification.emotion is not None
                ),
                entity_count=len(parsed.entities),
                relationship_count=len(parsed.relationships),
                fact_count=len(parsed.facts),
                structured_count=len(parsed.structured_extractions),
                blob_count=blob_count,
            )

            # ── LLM-driven fact invalidation gate ───────────────────────
            # Default ON; the org opts out with ``llm_fact_invalidation_enabled:
            # false``.  The single LLM call above succeeding is the availability
            # gate — resolve_backend raises without an org-level llm_backend,
            # so a completed call means an LLM is actually configured.
            invalidation_enabled = (
                org_cfg is not None
                and org_cfg.llm_fact_invalidation_enabled is not False
            )

            # ── 6b. Resolve graph backend (shared across sections) ──────
            # No Postgres fallback here: a disabled org ("none" / no config)
            # resolves to None → entities section skips persistence but still
            # sets the bit; a configured-but-unavailable backend raises and
            # fails the task via ARQ retry so the bit is never set without
            # entities actually being persisted.
            graph_backend = None
            try:
                graph_backend = await resolve_graph_backend(
                    ctx if isinstance(ctx, dict) else {},
                    uuid.UUID(org_id),
                    db,
                )
            except GraphBackendUnavailableError:
                # A CONFIGURED backend that can't be resolved is a broken
                # backend, not a disabled one — abort the task so no
                # enrichment bit gets set and reconcile/retry re-runs it.
                # Swallowing here would mark entities done without persisting
                # them (permanent silent data loss).
                log.error(
                    "enrich_episode.graph_backend_unavailable",
                    org_id=org_id,
                    episode_id=episode_id,
                )
                raise
            except Exception:
                log.error(
                    "enrich_episode.graph_backend_resolve_failed",
                    org_id=org_id,
                    episode_id=episode_id,
                    exc_info=True,
                )
                raise

            # Build shared repos
            entity_repo = (
                EntityRepository(db=db, graph_backend=graph_backend)
                if graph_backend
                else None
            )
            fact_repo = FactRepository(db)

            # Pre-fetch context from prompt data sources
            entity_types: list[str] = prompt_ctx.get("entity_types", [])
            known_entities: list[dict] = prompt_ctx.get("known_entities", [])
            existing_facts: list[dict] = prompt_ctx.get("existing_facts", [])
            schemas: list[dict] = prompt_ctx.get("schemas", [])

            # Track errors per section + accumulated bits for partial-retry
            errors: list[str] = []
            set_bits = 0

            # ── SECTION 1: Classification (bit 4) ────────────────────────
            if not (episode.enrichment_status & ENRICHMENT_CLASSIFICATION):
                try:
                    async with db.begin_nested():
                        await process_classification_output(
                            db=db,
                            org_id=org_id,
                            episode_id=episode_id,
                            project_id=project_id,
                            parsed=parsed.classification,
                            validation_sets=None,
                            episode_repo=episode_repo,
                        )
                    set_bits |= ENRICHMENT_CLASSIFICATION
                    log.info("enrich_episode.classification_done")
                except Exception:
                    log.exception("enrich_episode.classification_failed")
                    errors.append("classification")
            else:
                set_bits |= ENRICHMENT_CLASSIFICATION

            # ── SECTION 2: Entities (bit 0) ──────────────────────────────
            if not (episode.enrichment_status & ENRICHMENT_ENTITIES):
                try:
                    async with db.begin_nested():
                        if entity_repo is not None and graph_backend is not None:
                            entity_name_map = await process_entities_output(
                                db=db,
                                graph_backend=graph_backend,
                                entity_repo=entity_repo,
                                episode_repo=episode_repo,
                                org_id=org_id,
                                episode_id=episode_id,
                                project_id=project_id,
                                parsed=EntityExtractionOutput(
                                    entities=parsed.entities,
                                    relationships=parsed.relationships,
                                ),
                                entity_types=entity_types,
                            )
                            # Merge newly created entities into known_entities
                            # so the facts section can resolve against them.
                            known_names: set[str] = {
                                e.get("name", "").lower()
                                for e in known_entities
                                if e.get("name")
                            }
                            for ename, eid in entity_name_map.items():
                                if ename.lower() not in known_names:
                                    known_entities.append(
                                        {"name": ename, "id": eid}
                                    )
                        else:
                            # Graph disabled for this org — nothing was
                            # persisted to a graph.  The bit is still set so
                            # the episode is not re-enriched; entities can be
                            # back-filled later once a backend exists.
                            log.info(
                                "enrich_episode.graph_disabled_entities_skipped",
                                entity_count=len(parsed.entities),
                            )
                    set_bits |= ENRICHMENT_ENTITIES
                    log.info("enrich_episode.entities_done")
                except Exception:
                    log.exception("enrich_episode.entities_failed")
                    errors.append("entities")
            else:
                set_bits |= ENRICHMENT_ENTITIES

            # ── SECTION 3: Facts (bit 2) + LLM invalidations (bit 8) ────
            if not (episode.enrichment_status & ENRICHMENT_FACTS):
                try:
                    async with db.begin_nested():
                        facts_result = await process_facts_output(
                            db=db,
                            graph_backend=graph_backend,
                            entity_repo=entity_repo,
                            fact_repo=fact_repo,
                            episode_repo=episode_repo,
                            org_id=org_id,
                            episode_id=episode_id,
                            project_id=project_id,
                            session_id=session_id or "",
                            user_id=user_id,
                            trace_id=trace_id,
                            parsed=FactExtractionOutput(
                                facts=parsed.facts,
                            ),
                            known_entities=known_entities,
                            existing_facts=existing_facts,
                            arq_redis=arq_redis,
                            return_slot_map=True,
                        )
                        slot_map: dict[int, Fact | None]
                        if isinstance(facts_result, tuple):
                            _, slot_map = facts_result
                        else:
                            # Legacy test doubles return the plain id list.
                            slot_map = {}

                        # ── LLM-driven fact invalidations — inside the same
                        # savepoint as the facts they depend on, so a failure
                        # rolls back the persisted facts AND the invalidations
                        # together.
                        invalidations = (
                            parsed.invalidations
                            if isinstance(parsed.invalidations, list)
                            else []
                        )
                        if invalidation_enabled and invalidations:
                            from services.cache_service import CacheService
                            from services.fact_invalidation_service import (
                                PURGE_ONLY_CACHE_TTL,
                                FactInvalidationService,
                            )
                            from services.graph_edge_sync_service import (
                                GraphEdgeSyncService,
                            )

                            ref_to_fact = await _resolve_existing_fact_refs(
                                db, existing_facts
                            )
                            # Every output slot gets an entry — None when the
                            # slot's fact wasn't actually inserted (filtered,
                            # deduplicated, or skipped by the conflict scan),
                            # so a pure retraction never KeyErrors on lookup.
                            successor_by_ref = {
                                f"N{i}": slot_map.get(i)
                                for i in range(1, len(parsed.facts) + 1)
                            }
                            invalidation_svc = FactInvalidationService(
                                db=db,
                                fact_repo=fact_repo,
                                cache_service=(
                                    CacheService(
                                        arq_redis, default_ttl=PURGE_ONLY_CACHE_TTL
                                    )
                                    if arq_redis is not None
                                    else None
                                ),
                                graph_sync=(
                                    GraphEdgeSyncService(backends=[graph_backend])
                                    if graph_backend is not None
                                    else None
                                ),
                            )
                            invalidation_result = (
                                await invalidation_svc.apply_llm_invalidations(
                                    org_id=uuid.UUID(org_id),
                                    project_id=uuid.UUID(project_id),
                                    invalidations=[
                                        inv.model_dump() for inv in invalidations
                                    ],
                                    ref_to_fact=ref_to_fact,
                                    successor_by_ref=successor_by_ref,
                                )
                            )
                            log.info(
                                "enrich_episode.invalidations_applied",
                                closed_count=invalidation_result.closed_count,
                                skipped_count=invalidation_result.skipped_count,
                            )

                        # Invalidation bit — stamped unconditionally: an
                        # episode whose facts are persisted and invalidation
                        # assessed (or skipped because disabled / no
                        # invalidations) must not re-enrich.
                        await episode_repo.apply_enrichment_bits(
                            uuid.UUID(episode_id), LLM_INVALIDATION_BIT
                        )
                    set_bits |= ENRICHMENT_FACTS | LLM_INVALIDATION_BIT
                    log.info("enrich_episode.facts_done")
                except Exception:
                    log.exception("enrich_episode.facts_failed")
                    errors.append("facts")
            else:
                set_bits |= ENRICHMENT_FACTS
                # Pre-invalidation episodes carry bit 2 but not bit 8 —
                # stamp bit 8 so the top-level check stops re-running the
                # LLM.  There is no stored LLM output to invalidate against
                # retroactively, so the bit is marked assessed.
                if not (episode.enrichment_status & LLM_INVALIDATION_BIT):
                    await episode_repo.apply_enrichment_bits(
                        uuid.UUID(episode_id), LLM_INVALIDATION_BIT
                    )
                    set_bits |= LLM_INVALIDATION_BIT

            # ── SECTION 4: Structured Extraction (bit 5) ─────────────────
            if not (episode.enrichment_status & ENRICHMENT_STRUCTURED_EXTRACTION):
                try:
                    async with db.begin_nested():
                        await process_structured_output(
                            db=db,
                            org_id=org_id,
                            episode_id=episode_id,
                            project_id=project_id,
                            session_id=session_id or "",
                            parsed=parsed.structured_extractions,
                            schemas=schemas,
                            episode_repo=episode_repo,
                        )
                    set_bits |= ENRICHMENT_STRUCTURED_EXTRACTION
                    log.info("enrich_episode.structured_done")
                except Exception:
                    log.exception("enrich_episode.structured_failed")
                    errors.append("structured")
            else:
                set_bits |= ENRICHMENT_STRUCTURED_EXTRACTION

            # ── 7. Commit all successful savepoints ─────────────────────
            try:
                await db.commit()
                log.info("enrich_episode.commit_done", set_bits=set_bits)
            except Exception:
                log.exception("enrich_episode.commit_failed")
                raise

        # ── Report partial failure for ARQ retry ──────────────────────────
        if errors:
            raise PartialEnrichmentError(
                f"Enrichment sections failed for episode {episode_id}: "
                f"{', '.join(errors)}. Successful bits: {set_bits}",
                successful_bits=set_bits,
            )

        log.info("enrich_episode.complete", successful_bits=set_bits)

    except Exception:
        log.error(
            "enrich_episode.failed",
            episode_id=episode_id,
            org_id=org_id,
        )
        raise
    finally:
        if _own_engine:
            await engine.dispose()
