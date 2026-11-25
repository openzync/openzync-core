"""Prompt template resolution with auto-injected context.

Prompt texts are disk-only: ``services/worker/prompts/manifest.yaml`` picks
the ``is_default_for_type`` entry per type and the ``.jinja2`` files are
read once at startup into :data:`core.prompt_manifest.DEFAULT_PROMPT_TEXTS`.
:func:`resolve_prompt_template_by_type` is a dict lookup — no DB access.
Context is assembled and injected by the caller via
:func:`build_enrichment_prompt`.

Usage (workers):
    from services.worker.prompt_renderer import render_prompt

    system_prompt, ctx = await render_prompt(
        "enrich_episode",
        user_id=user_id,
        episode_id=episode_id,
        session_id=session_id,
        db_session_factory=session_factory,
        return_context=True,
    )
    prompt = build_enrichment_prompt(system_prompt, ctx)
"""

from __future__ import annotations

from datetime import UTC
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID

import orjson
import structlog

from core import rls
from core.prompt_manifest import DEFAULT_PROMPT_TEXTS

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.graph_backend.interface import GraphBackend

    # The worker hands out either a plain ``async_sessionmaker`` or the
    # RLS-scoped wrapper (``services.worker.worker.rls_scoped_session_factory``),
    # whose call returns an async context manager instead of a bare session.
    SessionFactory = (
        async_sessionmaker[AsyncSession]
        | Callable[[], AbstractAsyncContextManager[AsyncSession]]
    )

logger = structlog.get_logger(__name__)

# ── Module layout ──────────────────────────────────────────────────────────────
# 1. DataSource enum — every DB-backed variable type the system knows about
# 2. TYPE_DATA_SOURCES — static registry: prompt_type → set of DataSource
# 3. Provider functions — one per DataSource, each returns dict[str, Any]
# 4. Provider dispatch
# 5. render_prompt() — async, auto-injects context from DB
# 6. resolve_prompt_template_by_type() — sync disk-cache lookup
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
    """Known entities for a session (from ``FactRepository``)."""

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
    "enrich_episode": {
        DataSource.EPISODE_CONTENT,
        DataSource.EPISODE_METADATA,
        DataSource.SESSION_ENTITIES,
        DataSource.SESSION_FACTS,
        DataSource.SESSION_RECENT_HISTORY,
        DataSource.SIMILAR_EPISODES,
        DataSource.SIMILAR_FACTS,
        DataSource.ORG_ENTITY_TYPES,
        DataSource.ORG_CLASSIFICATION_LABELS,
        DataSource.ORG_STRUCTURED_SCHEMAS,
        DataSource.CUSTOM_INSTRUCTIONS,
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

_USER_ENTITIES_LIMIT: int = 50
"""Max entity entries returned for user context (mirrors the old LIMIT 50)."""

_USER_ENTITIES_EPISODE_CAP: int = 500
"""Max most-recent user episodes resolved into backend episode-id filters."""


async def _fetch_episode_content(
    db: AsyncSession,
    user_id: UUID,
    episode_id: UUID | None,
    **_: Any,
) -> dict[str, Any]:
    """Fetch the content of the current episode being enriched.

    Returns ``{"conversation": "<content>"}`` or ``{"conversation": ""}``
    if no ``episode_id`` is provided or the episode is not found.
    """
    if episode_id is None:
        return {"conversation": ""}

    from repositories.prompt_context_repository import (  # noqa: PLC0415 — lazy import
        PromptContextRepository,
    )

    repo = PromptContextRepository(db)
    row = await repo.get_episode_content(episode_id=episode_id, user_id=user_id)
    return {"conversation": row if row is not None else ""}


async def _fetch_session_entities(
    db: AsyncSession,
    user_id: UUID,
    session_id: UUID | None,
    graph_backend: GraphBackend | None,
    project_id: UUID | None,
    **extra: Any,
) -> dict[str, Any]:
    """Fetch known entities for a session via the graph backend.

    Returns ``{"known_entities": [...]}`` or an empty list if no session_id
    or missing parameters.
    """
    if session_id is None or project_id is None or graph_backend is None:
        return {"known_entities": []}

    # External backends (SurrealDB/FalkorDB) manage their own connections —
    # safe to use the backend directly.
    entities = await graph_backend.get_entities_for_session(
        user_id=user_id,
        project_id=project_id,
        session_id=session_id,
    )
    return {"known_entities": entities}


async def _fetch_session_facts(
    db: AsyncSession,
    user_id: UUID,
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
        user_id=user_id,
        session_id=session_id,
        limit=200,
    )
    return {"existing_facts": facts}


async def _fetch_session_recent_history(
    db: AsyncSession,
    user_id: UUID,
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

    from repositories.prompt_context_repository import (  # noqa: PLC0415 — lazy import
        PromptContextRepository,
    )

    repo = PromptContextRepository(db)
    # Column-only rows — detached scalars, no ORM expiry risk past session close.
    rows = await repo.list_session_recent_history(
        session_id=session_id,
        exclude_episode_id=episode_id,
        limit=_RECENT_HISTORY_WINDOW,
    )
    rows = list(rows)
    rows.reverse()  # chronological order
    recent_history = [{"role": r[0], "content": r[1]} for r in rows]
    return {"recent_history": recent_history}


async def _fetch_episode_metadata(
    db: AsyncSession,
    user_id: UUID,
    episode_id: UUID | None,
    **_: Any,
) -> dict[str, Any]:
    """Fetch the metadata JSONB of the current episode being enriched.

    Returns ``{"message_metadata": {...}}`` or ``{"message_metadata": {}}``
    if no ``episode_id`` is provided or the episode is not found.
    """
    if episode_id is None:
        return {"message_metadata": {}}

    from repositories.prompt_context_repository import (  # noqa: PLC0415 — lazy import
        PromptContextRepository,
    )

    repo = PromptContextRepository(db)
    row = await repo.get_episode_metadata(episode_id=episode_id, user_id=user_id)
    return {"message_metadata": row if row is not None else {}}


async def _fetch_similar_episodes(
    db: AsyncSession,
    user_id: UUID,
    episode_id: UUID | None,
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

    from repositories.prompt_context_repository import (  # noqa: PLC0415 — lazy import
        PromptContextRepository,
    )

    # Fetch current episode content + project_id to use as search query
    found = await PromptContextRepository(db).get_episode_content_and_project(
        episode_id=episode_id, user_id=user_id
    )
    if found is None:
        return {"similar_episodes": []}
    query, project_id = found

    from repositories.episode_repository import EpisodeRepository  # noqa: PLC0415

    repo = EpisodeRepository(db)
    results = await repo.search_by_bm25(
        query=query,
        project_id=project_id,
        user_id=user_id,
        limit=5,
    )
    # Filter out current episode from results
    results = [r for r in results if r["id"] != str(episode_id)]
    return {"similar_episodes": results}


async def _fetch_similar_facts(
    db: AsyncSession,
    user_id: UUID,
    episode_id: UUID | None,
    **_: Any,
) -> dict[str, Any]:
    """Fetch semantically similar facts via BM25 full-text search.

    Uses the current episode's content as the search query and returns
    the top 5 matching facts scoped to the same user.

    Returns ``{"related_facts": [...]}`` or empty list.
    """
    if episode_id is None or user_id is None:
        return {"related_facts": []}

    from repositories.prompt_context_repository import (  # noqa: PLC0415 — lazy import
        PromptContextRepository,
    )

    # Fetch current episode content + project_id to use as search query
    found = await PromptContextRepository(db).get_episode_content_and_project(
        episode_id=episode_id, user_id=user_id
    )
    if found is None:
        return {"related_facts": []}
    query, project_id = found

    from repositories.fact_repository import FactRepository  # noqa: PLC0415

    repo = FactRepository(db)
    results = await repo.search_by_bm25(
        query=query,
        project_id=project_id,
        user_id=user_id,
        limit=5,
    )
    return {"related_facts": results}


async def _fetch_org_entity_types(
    db: AsyncSession,
    user_id: UUID,
    **_: Any,
) -> dict[str, Any]:
    """Fetch entity type ontology from the org's extraction schemas.

    Falls back to the default set if no schemas are configured.
    Returns ``{"entity_types": [...]}``.
    """
    from repositories.prompt_context_repository import (  # noqa: PLC0415 — lazy import
        PromptContextRepository,
    )

    repo = PromptContextRepository(db)
    schemas = await repo.list_schema_json_by_type(
        user_id=user_id, schema_type="entity_type"
    )

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
    for schema in schemas:
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
    user_id: UUID,
    **_: Any,
) -> dict[str, Any]:
    """Fetch classification labels from the org's extraction schemas.

    Merges label sets if multiple schemas exist.
    Falls back to default label sets.

    Returns a dict with keys ``intent_labels``, ``emotion_labels``,
    ``valence_options``, ``arousal_options`` (comma-separated strings).
    """
    from repositories.prompt_context_repository import (  # noqa: PLC0415 — lazy import
        PromptContextRepository,
    )

    repo = PromptContextRepository(db)
    schemas = await repo.list_schema_json_by_type(
        user_id=user_id, schema_type="classification"
    )

    if not schemas:
        return {
            "intent_labels": (
                "greeting, question, command, complaint, chit-chat, "
                "farewell, request, confirmation"
            ),
            "emotion_labels": (
                "joy, frustration, sadness, anger, neutral, surprise, fear, disgust"
            ),
            "valence_options": "positive, negative, neutral",
            "arousal_options": "low, medium, high",
        }

    all_intents: set[str] = set()
    all_emotions: set[str] = set()
    valences: set[str] = set()
    arousals: set[str] = set()

    for schema in schemas:
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
        else (
            "greeting, question, command, complaint, chit-chat, "
            "farewell, request, confirmation"
        ),
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
    user_id: UUID,
    **_: Any,
) -> dict[str, Any]:
    """Fetch active structured extraction schemas.

    Returns ``{"schemas": [...]}`` — list of dicts with ``id``, ``name``,
    ``json_schema``, ``prompt_template``.
    """
    from repositories.prompt_context_repository import (  # noqa: PLC0415 — lazy import
        PromptContextRepository,
    )

    repo = PromptContextRepository(db)
    schemas = await repo.list_structured_schemas(user_id=user_id)
    return {"schemas": schemas}


async def _fetch_user_episodes(
    db: AsyncSession,
    user_id: UUID,
    **_: Any,
) -> dict[str, Any]:
    """Fetch last 100 episodes for a user (chronological).

    Returns ``{"episodes": [...]}`` or empty list if no user_id.
    """
    if user_id is None:
        return {"episodes": []}

    from repositories.prompt_context_repository import (  # noqa: PLC0415 — lazy import
        PromptContextRepository,
    )

    repo = PromptContextRepository(db)
    episodes_raw = await repo.list_user_episodes(
        user_id=user_id, limit=100
    )
    episodes_raw = list(episodes_raw)
    episodes_raw.reverse()  # chronological
    episodes = [{"role": r[0], "content": r[1]} for r in episodes_raw]
    return {"episodes": episodes}


async def _fetch_user_facts(
    db: AsyncSession,
    user_id: UUID,
    **_: Any,
) -> dict[str, Any]:
    """Fetch last 100 extracted facts for a user.

    Returns ``{"facts": [...]}`` or empty list if no user_id.
    """
    if user_id is None:
        return {"facts": []}

    from datetime import datetime  # noqa: PLC0415 — lazy import

    from repositories.prompt_context_repository import (  # noqa: PLC0415 — lazy import
        PromptContextRepository,
    )

    repo = PromptContextRepository(db)
    rows = await repo.list_user_facts(
        user_id=user_id,
        effective_at=datetime.now(UTC),
        limit=100,
    )
    facts = [
        {"subject": r[0], "predicate": r[1], "object": r[2]} for r in rows
    ]
    return {"facts": facts}


async def _fetch_user_entities(
    db: AsyncSession,
    user_id: UUID,
    graph_backend: GraphBackend | None,
    project_id: UUID | None,
    **extra: Any,
) -> dict[str, Any]:
    """Fetch distinct graph entities linked to a user's episodes.

    Resolves the user's episode IDs from PostgreSQL (core ``episodes`` /
    ``sessions`` tables — not graph stub tables), groups them by project,
    and delegates entity lookup to the graph backend's
    ``get_entities_for_user``.

    Returns ``{"entities": [{"name", "entity_type"}]}`` — identical shape
    to the previous PG-stub query — or an empty list if no user_id or no
    episodes.

    Raises:
        GraphBackendUnavailableError: If ``graph_backend`` is ``None``.
            Graph-disabled orgs skip upstream (the callers that resolve
            ``None`` never reach this provider with entity needs) — a
            ``None`` backend here is a wiring bug, never a steady state,
            so it fails loud instead of silently returning no entities.
    """
    if user_id is None:
        return {"entities": []}
    if graph_backend is None:
        from core.exceptions import (  # noqa: PLC0415 — lazy import
            GraphBackendUnavailableError,
        )

        logger.error(
            "prompt_renderer.user_entities_no_backend",
            user_id=str(user_id),
        )
        raise GraphBackendUnavailableError(
            f"No graph backend resolved for user entities (org {user_id}) — "
            "refusing to return an empty entity list that would silently "
            "degrade extraction context."
        )

    from repositories.prompt_context_repository import (  # noqa: PLC0415 — lazy import
        PromptContextRepository,
    )

    repo = PromptContextRepository(db)
    rows = await repo.list_user_episode_ids(
        user_id=user_id,
        project_id=project_id,
        cap=_USER_ENTITIES_EPISODE_CAP,
    )
    by_project: dict[str, list[UUID]] = {}
    for row in rows:
        by_project.setdefault(str(row[1]), []).append(UUID(str(row[0])))
    if not by_project:
        return {"entities": []}

    # External backends manage their own connections — safe to use
    # the backend directly.
    return {
        "entities": await _collect_user_entities(
            graph_backend, user_id, by_project
        )
    }


async def _collect_user_entities(
    backend: GraphBackend,
    user_id: UUID,
    by_project: dict[str, list[UUID]],
) -> list[dict[str, str]]:
    """Fan out ``get_entities_for_user`` per project and merge results.

    Dedupes by entity id across projects and caps at 50 entries,
    mirroring the previous ``LIMIT 50`` PG-stub query.
    """
    seen: set[str] = set()
    merged: list[dict[str, str]] = []
    for project_key, episode_ids in by_project.items():
        entities = await backend.get_entities_for_user(
            user_id=user_id,
            project_id=UUID(project_key),
            episode_ids=episode_ids,
        )
        for entity in entities:
            entity_id = str(entity.get("id", ""))
            if entity_id and entity_id not in seen:
                seen.add(entity_id)
                merged.append(
                    {
                        "name": str(entity.get("name", "")),
                        "entity_type": str(entity.get("entity_type", "")),
                    }
                )
                if len(merged) >= _USER_ENTITIES_LIMIT:
                    return merged
    return merged


async def _fetch_user_classifications(
    db: AsyncSession,
    user_id: UUID,
    **_: Any,
) -> dict[str, Any]:
    """Fetch aggregate classification labels for a user.

    Archived projects are excluded: their episodes stopped being classified
    (the workers early-return on archived), so including them would let stale
    conversations dominate the generated summary.  Neither intent nor emotion
    is a displayed number or model-neutral input — the bias would be silent.

    Returns ``{"classifications": {"top_intents": [...], "top_emotions": [...]}}``
    or empty dict if no user_id.
    """
    if user_id is None:
        return {"classifications": {"top_intents": [], "top_emotions": []}}

    from repositories.prompt_context_repository import (  # noqa: PLC0415 — lazy import
        PromptContextRepository,
    )

    repo = PromptContextRepository(db)
    rows = await repo.list_user_classifications(
        user_id=user_id, limit=5
    )
    top_intents: list[str] = []
    top_emotions: list[str] = []
    for r in rows:
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
    user_id: UUID,
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
        user_id=user_id,
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
    user_id: UUID | str | None = None,
    episode_id: UUID | str | None = None,
    session_id: UUID | str | None = None,
    project_id: UUID | str | None = None,
    graph_backend: GraphBackend | None = None,
    db_session_factory: SessionFactory | None = None,
    return_context: bool = False,
    skip_sources: set[DataSource] | None = None,
    **extra_context: Any,
) -> str | tuple[str, dict[str, Any]]:
    """Fetch and return a system prompt with auto-injected context.

    When ``user_id`` and ``db_session_factory`` are provided, context
    variables are auto-fetched from the DB according to the registered
    ``TYPE_DATA_SOURCES`` for the given ``prompt_type``.  Explicit
    ``extra_context`` kwargs take precedence over auto-injected values.

    When ``user_id`` is ``None`` (or ``db_session_factory`` is ``None``),
    no auto-injection occurs — only ``extra_context`` and the optional
    ``template_text`` are used.  This is the direct-invocation mode
    suitable for eval tests and one-off prompts.

    The returned ``template_text`` is raw Jinja2 (unrendered).  Call
    ``build_enrichment_prompt()`` to render it with the context.

    Args:
        prompt_type: The template type (e.g. ``"enrich_episode"``).
            Used to look up the registry of data sources to auto-inject.
        template_text: Raw template string.  If ``None``, resolved from
            the startup disk cache via ``resolve_prompt_template_by_type``.
        user_id: Organisation UUID.  Required for auto-injection.
        episode_id: Episode UUID.  Used to fetch conversation content
            and recent history.
        session_id: Session UUID.  Used to fetch known entities and
            existing facts for delta extraction.
        user_id: User UUID.  Used to fetch user-specific data
            (episodes, facts, entities, classifications).
        project_id: Project UUID.  Scopes session/user entity lookups;
            passed through to the graph backend.
        graph_backend: Resolved graph backend for entity providers.
            ``None`` is fail-loud on entity paths (raises
            ``GraphBackendUnavailableError``) — callers needing entity
            context must resolve the backend before rendering.
        db_session_factory: Session factory for DB access.  Required
            for auto-injection of context providers.
        return_context: If ``True``, returns ``(prompt, context_dict)``
            instead of just the prompt string.  Useful for callers that
            need the injected context for post-processing (e.g. entity
            resolution after fact extraction).
        skip_sources: Optional set of ``DataSource`` members to skip during
            auto-injection.  Skipped providers run no DB queries and leave
            their context keys absent (the template's ``{% if %}`` guards
            hide those blocks).  ``None`` (default) keeps current
            behavior — every registered source runs.
        **extra_context: Explicit template variables.  These override
            any auto-injected values with the same key.

    Returns:
        The raw template text (Jinja2, unrendered).  When
        ``return_context=True``, returns ``(template_text, context_dict)``
        — pass both to ``build_enrichment_prompt()`` to render.

    Raises:
        ValueError: If no cached default exists for ``prompt_type``.
        KeyError: If ``prompt_type`` is not in ``TYPE_DATA_SOURCES``
            (only when auto-injection is active).

    Example:
        .. code-block:: python

            prompt = await render_prompt(
                "enrich_episode",
                user_id="...",
                episode_id="...",
                session_id="...",
                db_session_factory=session_factory,
            )
    """
    # ── Resolve template text ──────────────────────────────────────────
    if template_text is None:
        # Disk cache, loaded at startup — evals pass template_text directly.
        template_text = resolve_prompt_template_by_type(prompt_type)

    # ── Auto-inject context from DB ────────────────────────────────────
    context: dict[str, Any] = dict(extra_context)

    if user_id is not None and db_session_factory is not None:
        sources = TYPE_DATA_SOURCES.get(prompt_type)
        if sources is None:
            raise KeyError(
                f"Unknown prompt type '{prompt_type}'. "
                f"Known types: {', '.join(sorted(TYPE_DATA_SOURCES))}"
            )

        resolved_org_id = UUID(user_id) if isinstance(user_id, str) else user_id
        resolved_episode_id = (
            UUID(episode_id) if isinstance(episode_id, str) else episode_id
        )
        resolved_session_id = (
            UUID(session_id) if isinstance(session_id, str) else session_id
        )
        resolved_user_id = UUID(user_id) if isinstance(user_id, str) else user_id
        resolved_project_id = (
            UUID(project_id) if isinstance(project_id, str) else project_id
        )

        async with db_session_factory() as db:
            try:
                # Every provider below reads tenant-scoped rows — bind the
                # session to the org before the first query.
                await rls.set_tenant_context(db, resolved_org_id)
                for source in sources:
                    if skip_sources is not None and source in skip_sources:
                        continue
                    provider = _PROVIDER_DISPATCH.get(source)
                    if provider is None:
                        continue

                    result = await provider(
                        db=db,
                        user_id=resolved_org_id,
                        episode_id=resolved_episode_id,
                        session_id=resolved_session_id,
                        project_id=resolved_project_id,
                        graph_backend=graph_backend,
                        db_session_factory=db_session_factory,
                    )

                    # Provider returns dict[str, Any] — merge into context,
                    # but do NOT overwrite caller-provided extra_context.
                    for k, v in result.items():
                        if k not in extra_context:
                            context[k] = v
            finally:
                await rls.release_tenant_context(db)

        # ── Computed variables (derived from injected data) ────────────
        if prompt_type == "user_summary" and "episodes" in context:
            context["episode_count"] = len(context["episodes"])

    # ── Render ─────────────────────────────────────────────────────────
    # The template text stays raw Jinja2 here — rendering happens in
    # build_enrichment_prompt(), which also applies the include_* flags.
    # Context is returned as-is for the caller to pass through.
    if return_context:
        return template_text, context
    return template_text


# ── 6. resolve_prompt_template_by_type — disk-cache lookup ─────────────────


def resolve_prompt_template_by_type(type: str) -> str:
    """Resolve the default prompt template text for a given type.

    Dict lookup against the startup-cached defaults
    (:data:`core.prompt_manifest.DEFAULT_PROMPT_TEXTS`) — no DB access.

    Args:
        type: The template type (e.g. ``"enrich_episode"``).

    Returns:
        The active template body.

    Raises:
        ValueError: If no cached default exists for ``type``.
    """
    try:
        return DEFAULT_PROMPT_TEXTS[type]
    except KeyError:
        raise ValueError(
            f"No cached prompt template for type '{type}'. "
            f"Known types: {', '.join(sorted(DEFAULT_PROMPT_TEXTS))}"
        ) from None


# ── 7. Prompt assembly helper ─────────────────────────────────────────────────


def build_enrichment_prompt(
    system_prompt: str,
    ctx: dict[str, Any],
    *,
    include_classification: bool | None = None,
    include_entities: bool | None = None,
    include_facts: bool | None = None,
    include_structured: bool | None = None,
    include_invalidations: bool | None = None,
) -> str:
    """Render the enrichment prompt from the Jinja template and context.

    Renders ``system_prompt`` (the raw ``enrich_episode_v1.jinja2`` text)
    with Jinja2 using ``ctx`` plus per-section ``include_*`` flags, then
    appends MESSAGE METADATA as pretty-printed JSON.  Context sections
    (entities, facts, history, similar matches, schemas, conversation)
    render inside the template — the old manual appends are gone so the
    EXISTING FACTS table keeps the template's ``E{i}`` refs that
    invalidation resolution depends on.

    Args:
        system_prompt: Raw template text from ``render_prompt``.
        ctx: Context dict from ``render_prompt(return_context=True)``.
            May also carry ``include_*`` keys; explicit kwargs win.
        include_classification: Show the classification OUTPUT SCHEMA
            block.  Defaults to ``ctx["include_classification"]``, else
            ``True``.
        include_entities: Show the entities/relationships OUTPUT SCHEMA
            blocks.  Defaults to ``ctx["include_entities"]``, else ``True``.
        include_facts: Show the facts OUTPUT SCHEMA block.  Defaults to
            ``ctx["include_facts"]``, else ``True``.
        include_structured: Show the structured_extractions OUTPUT SCHEMA
            block.  Defaults to ``ctx["include_structured"]``, else ``True``.
        include_invalidations: Show the INVALIDATIONS block.  Defaults to
            ``ctx["include_invalidations"]``, else ``True``.

    Returns:
        A fully rendered prompt string ready for the LLM.
    """
    from jinja2 import Environment  # noqa: PLC0415 — render-only dependency

    flags = {
        "include_classification": include_classification
        if include_classification is not None
        else ctx.get("include_classification", True),
        "include_entities": include_entities
        if include_entities is not None
        else ctx.get("include_entities", True),
        "include_facts": include_facts
        if include_facts is not None
        else ctx.get("include_facts", True),
        "include_structured": include_structured
        if include_structured is not None
        else ctx.get("include_structured", True),
        "include_invalidations": include_invalidations
        if include_invalidations is not None
        else ctx.get("include_invalidations", True),
    }
    # S701: plain-text LLM prompt, not HTML — escaping would corrupt it.
    template = Environment(autoescape=False).from_string(  # noqa: S701
        system_prompt
    )
    parts: list[str] = [template.render({**ctx, **flags}).strip()]

    # ── Metadata ─────────────────────────────────────────────────────────
    metadata = ctx.get("message_metadata")
    if metadata:
        metadata_json = orjson.dumps(metadata, option=orjson.OPT_INDENT_2).decode()
        parts.append(f"\n\n## MESSAGE METADATA\n\n{metadata_json}")

    return "".join(parts)
