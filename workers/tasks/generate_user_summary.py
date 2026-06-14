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

from services.worker.prompt_renderer import render_prompt, resolve_prompt_template_by_type
from services.custom_instruction_service import format_custom_instructions
from workers.tasks.base import with_retry

logger = structlog.get_logger()


@with_retry(max_retries=2, base_delay_s=5.0)
async def generate_user_summary(
    ctx: object,
    org_id: str,
    user_id: str,
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

    # Lazy imports to keep the module importable without the full async
    # stack at definition time — ARQ workers run in a separate process.
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
    )

    from core.config import settings
    from core.db import get_async_session

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

    session_factory: async_sessionmaker[AsyncSession] = (
        ctx.get("db_session_factory") if isinstance(ctx, dict) else None
    )
    if session_factory is None:
        session_factory = get_async_session(engine)

    # ── 1-4. Fetch conversation data in a single session ──────────────────
    episodes: list[dict[str, str]] = []
    facts: list[dict[str, str]] = []
    entities: list[dict[str, str]] = []
    classifications: dict[str, list[str]] = {"top_intents": [], "top_emotions": []}

    try:
        async with session_factory() as db:
            # 1. Episodes (last 100, chronological)
            result = await db.execute(
                text("""
                    SELECT role, content FROM episodes
                    WHERE session_id IN (
                        SELECT id FROM sessions
                        WHERE user_id = :user_id AND organization_id = :org_id
                    )
                    AND is_deleted = false
                    ORDER BY created_at DESC
                    LIMIT 100
                """),
                {"user_id": uuid.UUID(user_id), "org_id": uuid.UUID(org_id)},
            )
            episodes = [{"role": r[0], "content": r[1]} for r in result.fetchall()]
            episodes.reverse()  # chronological order

            # 2. Facts (last 100)
            result = await db.execute(
                text("""
                    SELECT f.subject, f.predicate, f.object FROM facts f
                    JOIN episodes e ON f.source_episode_id = e.id
                    JOIN sessions s ON e.session_id = s.id
                    WHERE s.user_id = :user_id AND s.organization_id = :org_id
                    ORDER BY f.created_at DESC
                    LIMIT 100
                """),
                {"user_id": uuid.UUID(user_id), "org_id": uuid.UUID(org_id)},
            )
            facts = [
                {"subject": r[0], "predicate": r[1], "object": r[2]}
                for r in result.fetchall()
            ]

            # 3. Entities (distinct, up to 50)
            result = await db.execute(
                text("""
                    SELECT DISTINCT ge.name, ge.entity_type
                    FROM graph_entities ge
                    JOIN graph_episode_entities gee ON ge.id = gee.entity_id
                    JOIN episodes e ON gee.episode_id = e.id
                    JOIN sessions s ON e.session_id = s.id
                    WHERE s.user_id = :user_id AND s.organization_id = :org_id
                    LIMIT 50
                """),
                {"user_id": uuid.UUID(user_id), "org_id": uuid.UUID(org_id)},
            )
            entities = [
                {"name": r[0], "entity_type": r[1]} for r in result.fetchall()
            ]

            # 4. Dialog classifications (aggregate top intents/emotions)
            result = await db.execute(
                text("""
                    SELECT intent, emotion, COUNT(*) as cnt
                    FROM dialog_classifications dc
                    JOIN episodes e ON dc.episode_id = e.id
                    JOIN sessions s ON e.session_id = s.id
                    WHERE s.user_id = :user_id AND s.organization_id = :org_id
                    GROUP BY intent, emotion
                    ORDER BY cnt DESC
                    LIMIT 5
                """),
                {"user_id": uuid.UUID(user_id), "org_id": uuid.UUID(org_id)},
            )
            for r in result.fetchall():
                if r[0]:
                    classifications["top_intents"].append(r[0])
                if r[1]:
                    classifications["top_emotions"].append(r[1])

        logger.debug(
            "user_summary.data_fetched",
            episode_count=len(episodes),
            fact_count=len(facts),
            entity_count=len(entities),
            top_intents=classifications["top_intents"],
            top_emotions=classifications["top_emotions"],
        )
    except Exception as exc:
        logger.warning(
            "user_summary.data_fetch_failed",
            org_id=org_id,
            user_id=user_id,
            error=str(exc),
            exc_info=True,
        )
        raise  # Let @with_retry handle transient DB failures

    # ── 5-6. Resolve custom instructions + prompt template ────────────────
    custom_instr = ""
    async with session_factory() as db:
        from repositories.custom_instruction_repository import (
            CustomInstructionRepository,
        )
        raw = await CustomInstructionRepository(db).get_by_scope(
            org_id=uuid.UUID(org_id), scope="user_summary", target_id=uuid.UUID(user_id),
        )
        if raw:
            custom_instr = format_custom_instructions(
                [{"name": i.name, "text": i.text} for i in raw],
            )

    template_text: str | None = None
    try:
        template_text = await resolve_prompt_template_by_type(
            "user_summary", org_id, session_factory,
        )
    except Exception:
        logger.warning(
            "user_summary.template_resolve_failed",
            exc_info=True,
        )

    # ── 7-8. Render prompt and call LLM ───────────────────────────────────
    from core.llm import resolve_backend

    try:
        prompt_text = render_prompt(
            "user_summary",
            template_text=template_text,
            custom_instructions=custom_instr,
            episodes=episodes,
            facts=facts,
            entities=entities,
            classifications=classifications,
            episode_count=len(episodes),
        )
    except FileNotFoundError:
        logger.error(
            "user_summary.prompt_missing",
            template="user_summary.jinja2",
        )
        return

    try:
        llm = await resolve_backend()
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
