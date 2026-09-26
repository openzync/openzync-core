"""User summary generation worker — LLM-driven profiling from conversation history.

Runs as an ARQ background task after enough new conversation data has
accumulated.  Fetches the user's episodic history, extracted facts, graph
entities, and dialog classifications, then renders a ``summarise_user_v1``
prompt and calls the LLM to produce or refresh the user profile summary.

Pipeline:
    1. Fetch the user's last 100 conversation episodes (chronological).
    2. Fetch extracted facts (subject-predicate-object triples).
    3. Fetch graph entities linked to the user's sessions.
    4. Fetch aggregate dialog classifications (top intents / emotions).
    5. Fetch custom instructions for the ``user_summary`` scope.
    6. Resolve prompt template from DB (filesystem fallback).
    7. Render the Jinja2 prompt with all gathered context.
    8. Call the LLM backend (temperature 0.3).
    9. Persist the generated summary on the User model.
"""

from __future__ import annotations

import time
import uuid

import structlog

from services.worker.prompt_renderer import render_prompt
from workers.tasks.base import with_retry

logger = structlog.get_logger()


@with_retry(max_retries=2, base_delay_s=5.0)
async def generate_user_summary(
    ctx: object,
    org_id: str,
    user_id: str,
    project_id: str | None = None,
    trace_id: str = "",
) -> None:
    """Generate or refresh a user profile summary from conversation history.

    Designed as an ARQ task — the ``ctx`` parameter provides a shared DB
    engine from the worker process (``ctx["db_engine"]``).  When ``ctx``
    is absent (direct invocation), a short-lived engine is created as a
    fallback.

    Pipeline:
        1. Fetch last 100 episodes, facts, entities, and classifications.
        2. Fetch custom instructions + resolve prompt template.
        3. Render ``summarise_user_v1`` Jinja2 prompt.
        4. Call LLM (temperature 0.3).
        5. Persist the summary on the User model via ``UserRepository.update_summary``.

    Args:
        ctx: ARQ worker context (unused — required by ARQ contract).
        org_id: UUID of the owning organization (string, from ARQ).
        user_id: UUID of the user to summarise.
        project_id: Optional project UUID to scope data fetching.
        trace_id: Request trace ID for end-to-end correlation across ARQ tasks.

    Raises:
        Exception: Re-raises the last LLM or DB error after retry exhaustion
            (``on_exhaustion="raise"`` default behaviour).
    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    from core.config import settings
    from core.db import get_async_session
    from core.org_config import get_org_config

    logger.info(
        "user_summary.started",
        org_id=org_id,
        user_id=user_id,
        trace_id=trace_id,
    )

    # ── Resolve DB engine from ARQ worker context (or create fallback) ────
    engine = ctx.get("db_engine") if isinstance(ctx, dict) else None
    if engine is None:
        from core.db import init_db_engine

        engine = init_db_engine(
            str(settings.DATABASE_URL), pool_size=2, max_overflow=1
        )
        _own_engine = True
    else:
        _own_engine = False

    session_factory = ctx.get("db_session_factory") if isinstance(ctx, dict) else None
    if session_factory is None:
        session_factory = get_async_session(engine)

    # ── 1-4. Render prompt with auto-injected context ─────────────────────
    # Resolve the graph backend BEFORE rendering — the user-entities
    # provider fails loud on a None backend (a missing backend here is a
    # wiring bug, never a steady state).  Graph-disabled orgs resolve to
    # None and skip entity context via the GraphBackendUnavailableError
    # path below, mirroring enrich_episode's section-2 skip.
    from core.exceptions import GraphBackendUnavailableError
    from core.llm import build_cache_config, resolve_backend
    from services.usage_service import record_llm_usage
    from workers.backend import resolve_graph_backend

    graph_backend = None
    try:
        async with session_factory() as _backend_db:
            graph_backend = await resolve_graph_backend(
                ctx if isinstance(ctx, dict) else {},
                uuid.UUID(org_id),
                _backend_db,
            )
    except GraphBackendUnavailableError:
        logger.error(
            "user_summary.graph_backend_unavailable",
            org_id=org_id,
            user_id=user_id,
        )
        raise
    if graph_backend is None:
        logger.warning(
            "user_summary.graph_disabled_entities_skipped",
            org_id=org_id,
            user_id=user_id,
        )
        raise GraphBackendUnavailableError(
            f"Graph disabled for org {org_id} — user summary requires "
            "entity context; refusing to render without it."
        )

    try:
        prompt_text = await render_prompt(
            "user_summary",
            org_id=org_id,
            user_id=user_id,
            project_id=project_id,
            graph_backend=graph_backend,
            db_session_factory=session_factory,
        )
    except Exception:
        logger.error(
            "user_summary.prompt_failed",
            org_id=org_id,
            user_id=user_id,
            exc_info=True,
        )
        raise

    # ── 5b. Fetch per-organization config for LLM resolution ─────────────
    llm_config_dict: dict | None = None
    try:
        bao_client = ctx.get("openbao_client") if isinstance(ctx, dict) else None
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
        logger.warning(
            "user_summary.org_config_fetch_failed",
            org_id=org_id,
            user_id=user_id,
            exc_info=True,
        )

    try:
        llm = await resolve_backend(org_config=llm_config_dict)
        start = time.monotonic()
        response = await llm.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a user profiling system. Output ONLY the summary text."
                    ),
                },
                {"role": "user", "content": prompt_text},
            ],
            temperature=0.3,
            cache_config=build_cache_config(org_config=llm_config_dict),
        )
    except Exception as exc:
        logger.error(
            "user_summary.llm_failed",
            org_id=org_id,
            user_id=user_id,
            error=str(exc),
        )
        raise  # Let @with_retry handle transient LLM failures

    # ── 9. Persist summary ────────────────────────────────────────────────
    from repositories.user_repository import UserRepository

    try:
        async with session_factory() as db:
            await UserRepository(db).update_summary(
                user_id=uuid.UUID(user_id),
                summary=response.content,
            )
            # Usage row shares the summary-persist transaction — the chat
            # itself is not transactional, so this is the earliest commit
            # point that keeps the record atomic with the surrounding work.
            await record_llm_usage(
                session=db,
                organization_id=uuid.UUID(org_id),
                model=response.model,
                task_type="user_summary",
                usage=response.usage,
                duration_ms=round((time.monotonic() - start) * 1000),
            )
            await db.commit()
    except Exception as exc:
        logger.error(
            "user_summary.persist_failed",
            org_id=org_id,
            user_id=user_id,
            error=str(exc),
        )
        raise  # Let @with_retry handle transient DB failures
    finally:
        if _own_engine:
            await engine.dispose()

    logger.info(
        "user_summary.completed",
        org_id=org_id,
        user_id=user_id,
        summary_length=len(response.content),
    )
