"""Prompt template resolution with auto-injected context.

All prompt templates live in the ``prompt_templates`` database table and are
resolved at runtime via :func:`resolve_prompt_template_by_type`.  The resolved
``template_text`` is returned as-is (plain text, no Jinja2) — context is
assembled and injected by the caller via :func:`build_enrichment_prompt`.

The ``.jinja2`` files under ``prompts/`` are the canonical source of truth
for system-default prompts (Option A).  They are read at signup time by
:meth:`PromptTemplateRepository.seed_default_prompts` and at import time by
:meth:`~.PromptTemplateRepository.import_system_template`.  The runtime
resolution path (``get_active_by_type``) only queries org-scoped DB rows.

Usage (workers):
    from services.worker.prompt_renderer import render_prompt

    system_prompt, ctx = await render_prompt(
        "fact_extraction",
        org_id=org_id,
        episode_id=episode_id,
        session_id=session_id,
        db_session_factory=session_factory,
        return_context=True,
    )
    prompt = build_enrichment_prompt(system_prompt, ctx)
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ── Module layout ──────────────────────────────────────────────────────────────
# 1. DataSource enum — every DB-backed variable type the system knows about
# 2. TYPE_DATA_SOURCES — static registry: prompt_type → set of DataSource
# 3. Provider functions — one per DataSource, each returns dict[str, Any]
# 4. Provider dispatch
# 5. render_prompt() — async, auto-injects context from DB
# 6. resolve_prompt_template_by_type() — kept for compat
# ────────────────────────────────────────────────────────────────────────────────


# ── 1. DataSource enum ─────────────────────────────────────────────────────────


class DataSource(Enum):
    """Known DB-backed sources for prompt context variables.

    Each value is a dot-separated descriptor.  The actual DB logic lives
    in the corresponding provider function in ``_PROVIDER_DISPATCH``.
    """

    EPISODE_CONTENT = "episode.content"
    """The ``content`` column of the current episode being enriched."""

    SESSION_ENTITIES = "session.entities"
    """Known entities for a session (from ``FactRepository.get_entities_for_session``)."""

    SESSION_FACTS = "session.facts"
    """Existing facts for a session (from ``FactRepository.list_by_session``)."""

    SESSION_RECENT_HISTORY = "session.recent_history"
    """Recent conversation turns for a session (last 10, chronological)."""

    ORG_ENTITY_TYPES = "org.entity_types"
    """Entity type ontology from ``extraction_schemas WHERE type='entity_type'``."""

    ORG_CLASSIFICATION_LABELS = "org.classification_labels"
    """Classification labels from ``extraction_schemas WHERE type='classification'``.
    Returns a dict with keys ``intent_labels``, ``emotion_labels``,
    ``valence_options``, ``arousal_options``.
    """

    ORG_STRUCTURED_SCHEMAS = "org.structured_schemas"
    """Active structured extraction schemas (``extraction_schemas WHERE
    type='structured'``).  Returns a list of dicts with ``name``,
    ``json_schema``, ``prompt_template``.
    """

    USER_EPISODES = "user.episodes"
    """Last 100 episodes for a user (chronological, for user summary)."""

    USER_FACTS = "user.facts"
    """Last 100 extracted facts for a user."""

    USER_ENTITIES = "user.entities"
    """Distinct graph entities linked to a user's sessions (up to 50)."""

    USER_CLASSIFICATIONS = "user.classifications"
    """Aggregate classification labels (top intents / emotions) for a user."""

    CUSTOM_INSTRUCTIONS = "org.custom_instructions"
    """Custom instructions for the prompt's scope (currently used by user_summary)."""

    EPISODE_METADATA = "episode.metadata"
    """The ``metadata`` JSONB of the current episode being enriched."""

    SIMILAR_EPISODES = "similar.episodes"
    """Semantically similar episodes via BM25 full-text search (scoped to user)."""

    SIMILAR_FACTS = "similar.facts"
    """Semantically similar facts via BM25 full-text search (scoped to user)."""


# ── 2. Static registry: prompt_type → set of DataSource ──────────────────────

TYPE_DATA_SOURCES: dict[str, set[DataSource]] = {
    "fact_extraction": {
        DataSource.EPISODE_CONTENT,
        DataSource.EPISODE_METADATA,
        DataSource.SESSION_ENTITIES,
        DataSource.SESSION_FACTS,
        DataSource.SESSION_RECENT_HISTORY,
        DataSource.SIMILAR_EPISODES,
        DataSource.SIMILAR_FACTS,
    },
    "entity_extraction": {
        DataSource.EPISODE_CONTENT,
        DataSource.EPISODE_METADATA,
        DataSource.SESSION_ENTITIES,
        DataSource.ORG_ENTITY_TYPES,
        DataSource.SIMILAR_EPISODES,
        DataSource.SIMILAR_FACTS,
    },
    "classification": {
        DataSource.EPISODE_CONTENT,
        DataSource.EPISODE_METADATA,
        DataSource.SESSION_RECENT_HISTORY,
        DataSource.SIMILAR_EPISODES,
        DataSource.SIMILAR_FACTS,
        DataSource.ORG_CLASSIFICATION_LABELS,
    },
    "structured_extraction": {
        DataSource.EPISODE_CONTENT,
        DataSource.EPISODE_METADATA,
        DataSource.SESSION_ENTITIES,
        DataSource.SESSION_FACTS,
        DataSource.SESSION_RECENT_HISTORY,
        DataSource.SIMILAR_EPISODES,
        DataSource.SIMILAR_FACTS,
        DataSource.ORG_STRUCTURED_SCHEMAS,
    },
    "user_summary": {
        DataSource.USER_EPISODES,
        DataSource.USER_FACTS,
        DataSource.USER_ENTITIES,
        DataSource.USER_CLASSIFICATIONS,
        DataSource.CUSTOM_INSTRUCTIONS,
    },
}


# ── 3. Provider functions — one per DataSource ──────────────────────────────

_RECENT_HISTORY_WINDOW: int = 10


async def _fetch_episode_content(
    db: AsyncSession,
    org_id: UUID,
    episode_id: UUID | None,
    **_: Any,
) -> dict[str, Any]:
    """Fetch the content of the current episode being enriched.

    Returns ``{"conversation": "<content>"}`` or ``{"conversation": ""}``
    if no ``episode_id`` is provided or the episode is not found.
    """
    if episode_id is None:
        return {"conversation": ""}

    from sqlalchemy import select  # noqa: PLC0415 — lazy import

    from models.episode import Episode  # noqa: PLC0415 — lazy import

    result = await db.execute(
        select(Episode.content).where(
            Episode.id == episode_id,
            Episode.organization_id == org_id,
        )
    )
    row = result.scalar_one_or_none()
    return {"conversation": row if row is not None else ""}


async def _fetch_session_entities(
    db: AsyncSession,
    org_id: UUID,
    session_id: UUID | None,
    **_: Any,
) -> dict[str, Any]:
    """Fetch known entities for a session.

    Returns ``{"known_entities": [...]}`` or an empty list if no session_id.
    """
    if session_id is None:
        return {"known_entities": []}

    from repositories.fact_repository import (
        FactRepository,  # noqa: PLC0415 — lazy import
    )

    repo = FactRepository(db)
    entities = await repo.get_entities_for_session(
        session_id=session_id,
        organization_id=org_id,
    )
    return {"known_entities": entities}


async def _fetch_session_facts(
    db: AsyncSession,
    org_id: UUID,
    session_id: UUID | None,
    **_: Any,
) -> dict[str, Any]:
    """Fetch existing facts for a session.

    Returns ``{"existing_facts": [...]}`` or an empty list if no session_id.
    """
    if session_id is None:
        return {"existing_facts": []}

    from repositories.fact_repository import (
        FactRepository,  # noqa: PLC0415 — lazy import
    )

    repo = FactRepository(db)
    facts, _ = await repo.list_by_session(
        organization_id=org_id,
        session_id=session_id,
        limit=200,
    )
    return {"existing_facts": facts}


async def _fetch_session_recent_history(
    db: AsyncSession,
    org_id: UUID,
    session_id: UUID | None,
    episode_id: UUID | None,
    **_: Any,
) -> dict[str, Any]:
    """Fetch recent conversation turns for a session (before current episode).

    Returns ``{"recent_history": [...]}`` — chronological list of
    ``{"role": str, "content": str}`` dicts, or empty list if no session_id.
    """
    if session_id is None:
        return {"recent_history": []}

    from sqlalchemy import select  # noqa: PLC0415 — lazy import

    from models.episode import Episode  # noqa: PLC0415 — lazy import

    query = (
        select(Episode)
        .where(
            Episode.session_id == session_id,
            Episode.is_deleted == False,  # noqa: E712 — SQLAlchemy boolean
        )
        .order_by(Episode.created_at.desc())
        .limit(_RECENT_HISTORY_WINDOW)
    )
    if episode_id is not None:
        query = query.where(Episode.id != episode_id)

    result = await db.execute(query)
    recent_eps = list(result.scalars().all())
    recent_eps.reverse()  # chronological order
    recent_history = [{"role": ep.role, "content": ep.content} for ep in recent_eps]
    return {"recent_history": recent_history}


async def _fetch_episode_metadata(
    db: AsyncSession,
    org_id: UUID,
    episode_id: UUID | None,
    **_: Any,
) -> dict[str, Any]:
    """Fetch the metadata JSONB of the current episode being enriched.

    Returns ``{"message_metadata": {...}}`` or ``{"message_metadata": {}}``
    if no ``episode_id`` is provided or the episode is not found.
    """
    if episode_id is None:
        return {"message_metadata": {}}

    from sqlalchemy import select  # noqa: PLC0415 — lazy import

    from models.episode import Episode  # noqa: PLC0415 — lazy import

    result = await db.execute(
        select(Episode.metadata_).where(
            Episode.id == episode_id,
            Episode.organization_id == org_id,
        )
    )
    row = result.scalar_one_or_none()
    return {"message_metadata": row if row is not None else {}}


async def _fetch_similar_episodes(
    db: AsyncSession,
    org_id: UUID,
    episode_id: UUID | None,
    user_id: UUID | None,
    **_: Any,
) -> dict[str, Any]:
    """Fetch semantically similar episodes via BM25 full-text search.

    Uses the current episode's content as the search query and returns
    the top 5 matches scoped to the same user.  BM25 is used instead of
    vector search because the episode may not have an embedding yet when
    enrichment runs.

    Returns ``{"similar_episodes": [...]}`` or empty list.
    """
    if episode_id is None or user_id is None:
        return {"similar_episodes": []}

    from sqlalchemy import select  # noqa: PLC0415 — lazy import

    from models.episode import Episode  # noqa: PLC0415 — lazy import

    # Fetch current episode content + project_id to use as search query
    ep_result = await db.execute(
        select(Episode.content, Episode.project_id).where(
            Episode.id == episode_id,
            Episode.organization_id == org_id,
        )
    )
    row = ep_result.one_or_none()
    if not row:
        return {"similar_episodes": []}
    query, project_id = row

    from repositories.episode_repository import EpisodeRepository  # noqa: PLC0415

    repo = EpisodeRepository(db)
    results = await repo.search_by_bm25(
        query=query,
        project_id=project_id,
        org_id=org_id,
        limit=5,
    )
    # Filter out current episode from results
    results = [r for r in results if r["id"] != str(episode_id)]
    return {"similar_episodes": results}


async def _fetch_similar_facts(
    db: AsyncSession,
    org_id: UUID,
    episode_id: UUID | None,
    user_id: UUID | None,
    **_: Any,
) -> dict[str, Any]:
    """Fetch semantically similar facts via BM25 full-text search.

    Uses the current episode's content as the search query and returns
    the top 5 matching facts scoped to the same user.

    Returns ``{"related_facts": [...]}`` or empty list.
    """
    if episode_id is None or user_id is None:
        return {"related_facts": []}

    from sqlalchemy import select  # noqa: PLC0415 — lazy import

    from models.episode import Episode  # noqa: PLC0415 — lazy import

    # Fetch current episode content + project_id to use as search query
    ep_result = await db.execute(
        select(Episode.content, Episode.project_id).where(
            Episode.id == episode_id,
            Episode.organization_id == org_id,
        )
    )
    row = ep_result.one_or_none()
    if not row:
        return {"related_facts": []}
    query, project_id = row

    from repositories.fact_repository import FactRepository  # noqa: PLC0415

    repo = FactRepository(db)
    results = await repo.search_by_bm25(
        query=query,
        project_id=project_id,
        org_id=org_id,
        limit=5,
    )
    return {"related_facts": results}


async def _fetch_org_entity_types(
    db: AsyncSession,
    org_id: UUID,
    **_: Any,
) -> dict[str, Any]:
    """Fetch entity type ontology from the org's extraction schemas.

    Falls back to the default set if no schemas are configured.
    Returns ``{"entity_types": [...]}``.
    """
    from sqlalchemy import text  # noqa: PLC0415 — lazy import

    result = await db.execute(
        text("""
            SELECT json_schema FROM extraction_schemas
            WHERE organization_id = :org_id
              AND type = 'entity_type'
              AND is_active = true
        """),
        {"org_id": org_id},
    )
    schemas = result.all()

    if not schemas:
        return {
            "entity_types": [
                "Person",
                "Organization",
                "Product",
                "Location",
                "Date",
                "Custom",
            ],
        }

    types: list[str] = []
    for row in schemas:
        schema: dict = row[0]
        if (
            isinstance(schema, dict)
            and "types" in schema
            and isinstance(schema["types"], list)
        ):
            types.extend(schema["types"])

    return {
        "entity_types": types
        or [
            "Person",
            "Organization",
            "Product",
            "Location",
            "Date",
            "Custom",
        ],
    }


async def _fetch_classification_labels(
    db: AsyncSession,
    org_id: UUID,
    **_: Any,
) -> dict[str, Any]:
    """Fetch classification labels from the org's extraction schemas.

    Merges label sets if multiple schemas exist.
    Falls back to default label sets.

    Returns a dict with keys ``intent_labels``, ``emotion_labels``,
    ``valence_options``, ``arousal_options`` (comma-separated strings).
    """
    from sqlalchemy import text  # noqa: PLC0415 — lazy import

    result = await db.execute(
        text("""
            SELECT json_schema FROM extraction_schemas
            WHERE organization_id = :org_id
              AND type = 'classification'
              AND is_active = true
        """),
        {"org_id": org_id},
    )
    schemas = result.all()

    if not schemas:
        return {
            "intent_labels": "greeting, question, command, complaint, chit-chat, farewell, request, confirmation",
            "emotion_labels": "joy, frustration, sadness, anger, neutral, surprise, fear, disgust",
            "valence_options": "positive, negative, neutral",
            "arousal_options": "low, medium, high",
        }

    all_intents: set[str] = set()
    all_emotions: set[str] = set()
    valences: set[str] = set()
    arousals: set[str] = set()

    for row in schemas:
        schema: dict = row[0]
        if isinstance(schema, dict):
            if "intent" in schema and isinstance(schema["intent"], list):
                all_intents.update(schema["intent"])
            if "emotion" in schema and isinstance(schema["emotion"], list):
                all_emotions.update(schema["emotion"])
            if "valence" in schema and isinstance(schema["valence"], list):
                valences.update(schema["valence"])
            if "arousal" in schema and isinstance(schema["arousal"], list):
                arousals.update(schema["arousal"])

    return {
        "intent_labels": ", ".join(sorted(all_intents))
        if all_intents
        else "greeting, question, command, complaint, chit-chat, farewell, request, confirmation",
        "emotion_labels": ", ".join(sorted(all_emotions))
        if all_emotions
        else "joy, frustration, sadness, anger, neutral, surprise, fear, disgust",
        "valence_options": ", ".join(sorted(valences))
        if valences
        else "positive, negative, neutral",
        "arousal_options": ", ".join(sorted(arousals))
        if arousals
        else "low, medium, high",
    }


async def _fetch_structured_schemas(
    db: AsyncSession,
    org_id: UUID,
    **_: Any,
) -> dict[str, Any]:
    """Fetch active structured extraction schemas.

    Returns ``{"schemas": [...]}`` — list of dicts with ``id``, ``name``,
    ``json_schema``, ``prompt_template``.
    """
    from sqlalchemy import text  # noqa: PLC0415 — lazy import

    result = await db.execute(
        text("""
            SELECT id, name, json_schema, prompt_template FROM extraction_schemas
            WHERE organization_id = :org_id
              AND type = 'structured'
              AND is_active = true
            ORDER BY name
        """),
        {"org_id": org_id},
    )
    rows = result.all()
    schemas = [
        {
            "id": str(row[0]),
            "name": row[1],
            "json_schema": row[2],
            "prompt_template": row[3],
        }
        for row in rows
    ]
    return {"schemas": schemas}


async def _fetch_user_episodes(
    db: AsyncSession,
    org_id: UUID,
    user_id: UUID | None,
    **_: Any,
) -> dict[str, Any]:
    """Fetch last 100 episodes for a user (chronological).

    Returns ``{"episodes": [...]}`` or empty list if no user_id.
    """
    if user_id is None:
        return {"episodes": []}

    from sqlalchemy import text  # noqa: PLC0415 — lazy import

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
        {"user_id": user_id, "org_id": org_id},
    )
    episodes_raw = list(result.fetchall())
    episodes_raw.reverse()  # chronological
    episodes = [{"role": r[0], "content": r[1]} for r in episodes_raw]
    return {"episodes": episodes}


async def _fetch_user_facts(
    db: AsyncSession,
    org_id: UUID,
    user_id: UUID | None,
    **_: Any,
) -> dict[str, Any]:
    """Fetch last 100 extracted facts for a user.

    Returns ``{"facts": [...]}`` or empty list if no user_id.
    """
    if user_id is None:
        return {"facts": []}

    from sqlalchemy import text  # noqa: PLC0415 — lazy import

    result = await db.execute(
        text("""
            SELECT f.subject, f.predicate, f.object FROM facts f
            JOIN episodes e ON f.source_episode_id = e.id
            JOIN sessions s ON e.session_id = s.id
            WHERE s.user_id = :user_id AND s.organization_id = :org_id
            ORDER BY f.created_at DESC
            LIMIT 100
        """),
        {"user_id": user_id, "org_id": org_id},
    )
    facts = [
        {"subject": r[0], "predicate": r[1], "object": r[2]} for r in result.fetchall()
    ]
    return {"facts": facts}


async def _fetch_user_entities(
    db: AsyncSession,
    org_id: UUID,
    user_id: UUID | None,
    **_: Any,
) -> dict[str, Any]:
    """Fetch distinct graph entities linked to a user's sessions.

    Returns ``{"entities": [...]}`` or empty list if no user_id.
    """
    if user_id is None:
        return {"entities": []}

    from sqlalchemy import text  # noqa: PLC0415 — lazy import

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
        {"user_id": user_id, "org_id": org_id},
    )
    entities = [{"name": r[0], "entity_type": r[1]} for r in result.fetchall()]
    return {"entities": entities}


async def _fetch_user_classifications(
    db: AsyncSession,
    org_id: UUID,
    user_id: UUID | None,
    **_: Any,
) -> dict[str, Any]:
    """Fetch aggregate classification labels for a user.

    Returns ``{"classifications": {"top_intents": [...], "top_emotions": [...]}}``
    or empty dict if no user_id.
    """
    if user_id is None:
        return {"classifications": {"top_intents": [], "top_emotions": []}}

    from sqlalchemy import text  # noqa: PLC0415 — lazy import

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
        {"user_id": user_id, "org_id": org_id},
    )
    top_intents: list[str] = []
    top_emotions: list[str] = []
    for r in result.fetchall():
        if r[0]:
            top_intents.append(r[0])
        if r[1]:
            top_emotions.append(r[1])

    return {
        "classifications": {
            "top_intents": top_intents,
            "top_emotions": top_emotions,
        },
    }


async def _fetch_custom_instructions(
    db: AsyncSession,
    org_id: UUID,
    user_id: UUID | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Fetch custom instructions for the org.

    For ``user_summary`` scope, also accepts a ``user_id`` for per-user
    custom instructions.  Returns ``{"custom_instructions": "<text>"}``
    or empty string if none found.
    """
    from repositories.custom_instruction_repository import (  # noqa: PLC0415 — lazy import
        CustomInstructionRepository,
    )

    repo = CustomInstructionRepository(db)
    raw = await repo.get_by_scope(
        org_id=org_id,
        # user_summary uses scope="user_summary" with user_id;
        # other scopes use "extraction" without user_id.
        scope="user_summary" if user_id is not None else "extraction",
        target_id=user_id,
    )
    if raw:
        from services.custom_instruction_service import (  # noqa: PLC0415 — lazy import
            format_custom_instructions,
        )

        text = format_custom_instructions(
            [{"name": i.name, "text": i.text} for i in raw],
        )
        return {"custom_instructions": text}

    return {"custom_instructions": ""}


# ── 4. Provider dispatch ──────────────────────────────────────────────────────

_PROVIDER_DISPATCH: dict[DataSource, Any] = {
    DataSource.EPISODE_CONTENT: _fetch_episode_content,
    DataSource.SESSION_ENTITIES: _fetch_session_entities,
    DataSource.SESSION_FACTS: _fetch_session_facts,
    DataSource.SESSION_RECENT_HISTORY: _fetch_session_recent_history,
    DataSource.ORG_ENTITY_TYPES: _fetch_org_entity_types,
    DataSource.ORG_CLASSIFICATION_LABELS: _fetch_classification_labels,
    DataSource.ORG_STRUCTURED_SCHEMAS: _fetch_structured_schemas,
    DataSource.USER_EPISODES: _fetch_user_episodes,
    DataSource.USER_FACTS: _fetch_user_facts,
    DataSource.USER_ENTITIES: _fetch_user_entities,
    DataSource.USER_CLASSIFICATIONS: _fetch_user_classifications,
    DataSource.CUSTOM_INSTRUCTIONS: _fetch_custom_instructions,
    DataSource.EPISODE_METADATA: _fetch_episode_metadata,
    DataSource.SIMILAR_EPISODES: _fetch_similar_episodes,
    DataSource.SIMILAR_FACTS: _fetch_similar_facts,
}


# ── 5. render_prompt — async with auto-injection ─────────────────────────────


async def render_prompt(
    prompt_type: str,
    *,
    template_text: str | None = None,
    org_id: UUID | str | None = None,
    episode_id: UUID | str | None = None,
    session_id: UUID | str | None = None,
    user_id: UUID | str | None = None,
    db_session_factory: async_sessionmaker[AsyncSession] | None = None,
    return_context: bool = False,
    **extra_context: Any,
) -> str | tuple[str, dict[str, Any]]:
    """Fetch and return a system prompt with auto-injected context.

    When ``org_id`` and ``db_session_factory`` are provided, context
    variables are auto-fetched from the DB according to the registered
    ``TYPE_DATA_SOURCES`` for the given ``prompt_type``.  Explicit
    ``extra_context`` kwargs take precedence over auto-injected values.

    When ``org_id`` is ``None`` (or ``db_session_factory`` is ``None``),
    no auto-injection occurs — only ``extra_context`` and the optional
    ``template_text`` are used.  This is the direct-invocation mode
    suitable for eval tests and one-off prompts.

    The returned ``template_text`` is plain text (no Jinja2).  Call
    ``build_enrichment_prompt()`` to assemble the full prompt.

    Args:
        prompt_type: The template type (e.g. ``"fact_extraction"``).
            Used to look up the registry of data sources to auto-inject.
        template_text: Raw template string.  If ``None`` and ``org_id``
            is provided, resolved from DB via ``resolve_prompt_template_by_type``.
        org_id: Organisation UUID.  Required for auto-injection.
        episode_id: Episode UUID.  Used to fetch conversation content
            and recent history.
        session_id: Session UUID.  Used to fetch known entities and
            existing facts for delta extraction.
        user_id: User UUID.  Used to fetch user-specific data
            (episodes, facts, entities, classifications).
        db_session_factory: Session factory for DB access.  Required
            for auto-injection (and template resolution from DB).
        return_context: If ``True``, returns ``(prompt, context_dict)``
            instead of just the prompt string.  Useful for callers that
            need the injected context for post-processing (e.g. entity
            resolution after fact extraction).
        **extra_context: Explicit template variables.  These override
            any auto-injected values with the same key.

    Returns:
        The system prompt text (plain text, no Jinja2).  When
        ``return_context=True``, returns ``(prompt, context_dict)``.

    Raises:
        ValueError: If ``template_text`` is ``None`` and cannot be
            resolved from the DB.
        KeyError: If ``prompt_type`` is not in ``TYPE_DATA_SOURCES``
            (only when auto-injection is active).

    Example:
        .. code-block:: python

            prompt = await render_prompt(
                "fact_extraction",
                org_id="...",
                episode_id="...",
                session_id="...",
                db_session_factory=session_factory,
            )
    """
    # ── Resolve template text ──────────────────────────────────────────
    if template_text is None:
        if org_id is not None and db_session_factory is not None:
            template_text = await resolve_prompt_template_by_type(
                prompt_type,
                org_id,
                db_session_factory,
            )

    if template_text is None:
        raise ValueError(
            f"No template_text available for '{prompt_type}'. "
            f"Provide it explicitly or pass org_id + db_session_factory "
            f"to resolve from the database."
        )

    # ── Auto-inject context from DB ────────────────────────────────────
    context: dict[str, Any] = dict(extra_context)

    if org_id is not None and db_session_factory is not None:
        sources = TYPE_DATA_SOURCES.get(prompt_type)
        if sources is None:
            raise KeyError(
                f"Unknown prompt type '{prompt_type}'. "
                f"Known types: {', '.join(sorted(TYPE_DATA_SOURCES))}"
            )

        resolved_org_id = UUID(org_id) if isinstance(org_id, str) else org_id
        resolved_episode_id = (
            UUID(episode_id) if isinstance(episode_id, str) else episode_id
        )
        resolved_session_id = (
            UUID(session_id) if isinstance(session_id, str) else session_id
        )
        resolved_user_id = UUID(user_id) if isinstance(user_id, str) else user_id

        async with db_session_factory() as db:
            for source in sources:
                provider = _PROVIDER_DISPATCH.get(source)
                if provider is None:
                    continue

                result = await provider(
                    db=db,
                    org_id=resolved_org_id,
                    episode_id=resolved_episode_id,
                    session_id=resolved_session_id,
                    user_id=resolved_user_id,
                )

                # Provider returns dict[str, Any] — merge into context,
                # but do NOT overwrite caller-provided extra_context.
                for k, v in result.items():
                    if k not in extra_context:
                        context[k] = v

        # ── Computed variables (derived from injected data) ────────────
        if prompt_type == "user_summary" and "episodes" in context:
            context["episode_count"] = len(context["episodes"])

    # ── Render ─────────────────────────────────────────────────────────
    # The template text is plain text (no Jinja2 variables). Context is
    # injected by the caller via build_enrichment_prompt().
    if return_context:
        return template_text, context
    return template_text


# ── 6. resolve_prompt_template_by_type — kept for direct use ────────────────


async def resolve_prompt_template_by_type(
    type: str,
    org_id: UUID | str,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> str | None:
    """Resolve the active default prompt template for a given type.

    Looks up the org-specific default for this type (``is_default_for_type
    = True``).  Returns ``None`` if no default exists (no system-level
    fallback — defaults come from disk manifest).

    Args:
        type: The template type (e.g. ``"fact_extraction"``).
        org_id: Organisation UUID (string or ``UUID`` instance).
        db_session_factory: An ``async_sessionmaker`` bound to the write
            database engine.

    Returns:
        The active template body, or ``None`` if no default exists.
    """
    if isinstance(org_id, str):
        org_id = UUID(org_id)

    from repositories.prompt_template_repository import (  # noqa: PLC0415 — lazy import
        PromptTemplateRepository,
    )

    async with db_session_factory() as session:
        repo = PromptTemplateRepository(session)
        template = await repo.get_active_by_type(org_id=org_id, type=type)

    return template.template_text if template is not None else None


# ── 7. Prompt assembly helper ─────────────────────────────────────────────────


def build_enrichment_prompt(system_prompt: str, ctx: dict[str, Any]) -> str:
    """Build a full enrichment prompt by appending context sections.

    Takes the raw system instructions from the DB and automatically injects
    all available context (metadata, entities, facts, history, semantic
    search results) as structured sections after the instructions.

    Args:
        system_prompt: Raw system instructions from the DB template.
        ctx: Context dict from ``render_prompt(return_context=True)``.

    Returns:
        A fully assembled prompt string ready for the LLM.
    """
    parts: list[str] = [system_prompt.strip()]

    # ── Metadata ─────────────────────────────────────────────────────────
    metadata = ctx.get("message_metadata")
    if metadata:
        parts.append(f"\n\n## MESSAGE METADATA\n\n{json.dumps(metadata, indent=2)}")

    # ── Known entities ──────────────────────────────────────────────────
    known_entities: list = ctx.get("known_entities", [])
    if known_entities:
        parts.append("\n\n## KNOWN ENTITIES\n\n")
        parts.append("| Name | Type |\n|------|------|\n")
        for ent in known_entities:
            parts.append(f"| {ent.get('name', '')} | {ent.get('entity_type', '')} |\n")

    # ── Existing facts ──────────────────────────────────────────────────
    existing_facts: list = ctx.get("existing_facts", [])
    if existing_facts:
        parts.append("\n\n## EXISTING FACTS\n\n")
        parts.append("| Subject | Predicate | Object |\n")
        parts.append("|---------|-----------|--------|\n")
        for f in existing_facts:
            parts.append(
                f"| {f.get('subject', '')} | {f.get('predicate', '')} | {f.get('object', '')} |\n"
            )

    # ── Recent history ──────────────────────────────────────────────────
    recent_history: list = ctx.get("recent_history", [])
    if recent_history:
        parts.append("\n\n## RECENT CONVERSATION\n\n")
        for turn in recent_history:
            parts.append(f"[{turn.get('role', '?')}]\n{turn.get('content', '')}\n")

    # ── Similar episodes (semantic search) ──────────────────────────────
    similar_episodes: list = ctx.get("similar_episodes", [])
    if similar_episodes:
        parts.append("\n\n## SIMILAR EPISODES FROM HISTORY\n\n")
        for ep in similar_episodes:
            role = ep.get("role", "?")
            content_preview = ep.get("content", "")[:300]
            score = ep.get("score", 0)
            parts.append(f"[{role}] (score: {score:.3f})\n{content_preview}\n\n")

    # ── Related facts (semantic search) ─────────────────────────────────
    related_facts: list = ctx.get("related_facts", [])
    if related_facts:
        parts.append("\n\n## RELATED FACTS FROM HISTORY\n\n")
        parts.append("| Subject | Predicate | Object | Score |\n")
        parts.append("|---------|-----------|--------|-------|\n")
        for f in related_facts:
            parts.append(
                f"| {f.get('subject', '')} | {f.get('predicate', '')} | {f.get('object', '')} | {f.get('score', 0):.3f} |\n"
            )

    # ── Conversation to extract from ───────────────────────────────────
    conversation = ctx.get("conversation", "")
    if conversation:
        parts.append(f"\n\n## NOW EXTRACT FROM THIS CONVERSATION\n\n{conversation}")

    return "".join(parts)
