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

import uuid

import structlog
from sqlalchemy import text

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
    from core.llm import resolve_backend

    try:
        prompt_text = await render_prompt(
            "user_summary",
            org_id=org_id,
            user_id=user_id,
            project_id=project_id,
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
        async with session_factory() as db:
            org_cfg = await get_org_config(
                uuid.UUID(org_id), db, redis=None
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
