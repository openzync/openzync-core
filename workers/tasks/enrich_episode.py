"""Single episode enrichment task — replaces 4 separate LLM calls.

This worker replaces ``classify_dialog``, ``extract_entities``, ``extract_facts``,
and ``extract_structured`` with a single LLM call that produces all outputs in
one pass.  Each enrichment section is processed independently with savepoint
isolation, so partial failures don't lose completed work.

Bitmask:
    Sets ``episodes.enrichment_status`` bits 0, 2, 4, 5
    (``ENRICHMENT_ENTITIES | ENRICHMENT_FACTS | ENRICHMENT_CLASSIFICATION |
     ENRICHMENT_STRUCTURED_EXTRACTION``) on success.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import text

from core.exceptions import EpisodeNotFoundError, GraphBackendUnavailableError
from services.worker.prompt_renderer import build_enrichment_prompt, render_prompt
from workers.tasks.base import (
    ENRICHMENT_CLASSIFICATION,
    ENRICHMENT_ENTITIES,
    ENRICHMENT_FACTS,
    ENRICHMENT_STRUCTURED_EXTRACTION,
    with_retry,
)

logger = structlog.get_logger()


class PartialEnrichmentError(Exception):
    """Raised when one or more enrichment sections fail.

    The ``successful_bits`` attribute tracks which sections completed,
    so retries can skip already-done work.
    """

    def __init__(self, message: str, successful_bits: int = 0) -> None:
        self.successful_bits = successful_bits
        super().__init__(message)


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
    """Single enrichment pass: classify + extract entities/facts/structured in one LLM call.

    Pipeline:
        1. Open session, set RLS context.
        2. Check idempotency — skip if all 4 LLM bits already set.
        3. Resolve ``user_id`` from the episode record.
        4. Render the ``enrich_episode_v1.jinja2`` prompt with auto-injected
           context (entities, facts, schemas, history, …).
        5. Resolve LLM backend from org config.
        6. Single LLM call with ``CombinedLLMOutput`` as ``response_model``.
        7. Process each enrichment section in an independent savepoint:
           - Classification  (bit 4)
           - Entities        (bit 0)
           - Facts           (bit 2)
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
    from core.llm import resolve_backend
    from core.org_config import get_org_config
    from repositories.entity_repository import EntityRepository
    from repositories.episode_repository import EpisodeRepository
    from repositories.fact_repository import FactRepository
    from schemas.llm_outputs import (
        CombinedLLMOutput,
        EntityExtractionOutput,
        FactExtractionOutput,
    )
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
            episode = await episode_repo.get_by_id(uuid.UUID(episode_id))
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

            # ── 4. Fetch per-organization config ────────────────────────
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
                )
            except Exception:
                log.exception("enrich_episode.llm_call_failed")
                raise

            parsed = response.validated_data
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
            )

            # ── 6b. Resolve graph backend (shared across sections) ──────
            graph_backend = None
            try:
                graph_backend = await resolve_graph_backend(
                    ctx if isinstance(ctx, dict) else {},
                    uuid.UUID(org_id),
                    db,
                    fallback_to_postgres=True,
                )
            except GraphBackendUnavailableError:
                log.warning("enrich_episode.graph_backend_unavailable")
            except Exception:
                log.warning(
                    "enrich_episode.graph_backend_resolve_failed",
                    exc_info=True,
                )

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
                            log.warning(
                                "enrich_episode.no_graph_backend_entities"
                            )
                    set_bits |= ENRICHMENT_ENTITIES
                    log.info("enrich_episode.entities_done")
                except Exception:
                    log.exception("enrich_episode.entities_failed")
                    errors.append("entities")
            else:
                set_bits |= ENRICHMENT_ENTITIES

            # ── SECTION 3: Facts (bit 2) ─────────────────────────────────
            if not (episode.enrichment_status & ENRICHMENT_FACTS):
                try:
                    async with db.begin_nested():
                        await process_facts_output(
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
                        )
                    set_bits |= ENRICHMENT_FACTS
                    log.info("enrich_episode.facts_done")
                except Exception:
                    log.exception("enrich_episode.facts_failed")
                    errors.append("facts")
            else:
                set_bits |= ENRICHMENT_FACTS

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
