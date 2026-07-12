"""Streamlit chat UI for OpenZync — powered by the OpenZync SDK + OpenRouter."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI
from langchain_core.messages import AIMessage, HumanMessage
from openzync import AsyncOpenZync
from openzync.integrations.langchain import OZMemory

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("streamlit_chat")

# ── Page Config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="OpenZync Chat",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Environment ───────────────────────────────────────────────────────────────

load_dotenv()

OPENZYNC_API_KEY: str = os.environ.get("OPENZYNC_API_KEY", "")
OPENZYNC_BASE_URL: str = os.environ.get("OPENZYNC_BASE_URL", "http://localhost:8000")
OPENROUTER_API_KEY: str = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL: str = os.environ.get(
    "OPENROUTER_MODEL", "openai/gpt-oss-20b:free"
)
OPENROUTER_BASE_URL: str = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
)
DEFAULT_USER_EXTERNAL_ID: str = os.environ.get(
    "DEFAULT_USER_EXTERNAL_ID", "streamlit-chat-user"
)
PROJECT_NAME: str = os.environ.get("PROJECT_NAME", "Streamlit Chat")
PROJECT_ID: str | None = os.environ.get("PROJECT_ID", None)
"""Optional pre-configured project UUID.  When set, skips project discovery."""

# Validate critical env vars.
missing: list[str] = []
if not OPENZYNC_API_KEY:
    missing.append("OPENZYNC_API_KEY")
if not OPENROUTER_API_KEY:
    missing.append("OPENROUTER_API_KEY")
if missing:
    st.error(
        f"Missing required environment variables: {', '.join(missing)}. "
        "Copy `.env.example` to `.env` and fill in your API keys."
    )
    st.stop()

# ── Cached Clients ────────────────────────────────────────────────────────────


@st.cache_resource
def _get_openzync_client() -> tuple[asyncio.AbstractEventLoop, AsyncOpenZync]:
    """Create and cache the OpenZync client with a persistent event loop.

    The sync ``OpenZync`` wrapper uses ``asyncio.run()`` per call, which
    closes the loop after each invocation. Python 3.14+ is stricter about
    closed-loop callbacks from httpx, so we manage our own persistent loop.
    """
    logger.info("Initializing OpenZync client: base_url=%s", OPENZYNC_BASE_URL)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = AsyncOpenZync(api_key=OPENZYNC_API_KEY, base_url=OPENZYNC_BASE_URL)
    return loop, client


@st.cache_resource
def _get_llm_client() -> OpenAI:
    """Create and cache the OpenAI-compatible client for OpenRouter."""
    logger.info(
        "Initializing OpenRouter client: base_url=%s model=%s",
        OPENROUTER_BASE_URL,
        OPENROUTER_MODEL,
    )
    return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)


_openzync_loop, _async_oz = _get_openzync_client()


def _await(coro: Any) -> Any:
    """Run an async SDK call on the persistent event loop."""
    return _openzync_loop.run_until_complete(coro)


llm: OpenAI = _get_llm_client()

# ── User Management ───────────────────────────────────────────────────────────


def _find_user_by_external_id(client: AsyncOpenZync, external_id: str) -> str | None:
    """Look up a user by external_id. Returns the UUID or ``None``."""
    try:
        result = _await(client.users.list(limit=20))
        users = result.get("data", []) if isinstance(result, dict) else result.data
        for u in users:
            if u.get("external_id") == external_id or (
                hasattr(u, "external_id") and u.external_id == external_id
            ):
                uid = u.get("id") if isinstance(u, dict) else u.id
                logger.info("Found existing user: %s (id=%s)", external_id, uid)
                return uid
    except Exception as exc:
        logger.warning("Failed to list users: %s", exc)
    return None


def _create_user(client: AsyncOpenZync, external_id: str) -> str:
    """Create a new user. Returns the UUID."""
    user = _await(client.users.create(external_id=external_id))
    uid: str = user.id if hasattr(user, "id") else user["id"]
    logger.info("Created user: %s (id=%s)", external_id, uid)
    return uid


def _ensure_user(client: AsyncOpenZync, external_id: str) -> str:
    """Find or create a user. Returns the UUID."""
    uid = _find_user_by_external_id(client, external_id)
    if uid is not None:
        return uid
    return _create_user(client, external_id)


# ── Project Management ─────────────────────────────────────────────────


def _ensure_project(client: AsyncOpenZync, name: str) -> str:
    """Find or create a project. Returns the project UUID.

    Uses the ``PROJECT_ID`` env var if set, otherwise discovers the first
    available project or creates a new one named *name*.
    """
    if PROJECT_ID:
        logger.info("Using configured project ID: %s", PROJECT_ID)
        return PROJECT_ID

    try:
        result = _await(client.projects.list(limit=1))
        items = result.get("data", []) if isinstance(result, dict) else result.data
        if items:
            pid = items[0]["id"] if isinstance(items[0], dict) else items[0].id
            logger.info("Using existing project: %s (id=%s)", name, pid)
            return pid
    except Exception as exc:
        logger.warning("Failed to list projects, will create: %s", exc)

    project = _await(client.projects.create(name=name, description="Auto-created for Streamlit Chat"))
    pid: str = project.id if hasattr(project, "id") else project["id"]
    logger.info("Created project: %s (id=%s)", name, pid)
    return pid


# ── Session Management ────────────────────────────────────────────────────────


def _list_sessions(client: AsyncOpenZync, project_id: str) -> list[dict[str, Any]]:
    """List all sessions for a project (newest first)."""
    try:
        result = _await(client.sessions.list(limit=100))
        sessions_raw = result.get("data", []) if isinstance(result, dict) else result.data
        sessions_raw.sort(key=lambda s: (
            s.get("created_at") if isinstance(s, dict) else s.created_at
            or ""
        ), reverse=True)
        return sessions_raw
    except Exception as exc:
        logger.warning("Failed to list sessions: %s", exc)
        return []


def _create_session(client: AsyncOpenZync, project_id: str) -> tuple[str, str]:
    """Create a new session. Returns ``(internal_id, external_id)``.

    Raises:
        RuntimeError: If the session cannot be created (e.g. API key not
            scoped to the project).
    """
    external_id = f"chat-{uuid.uuid4().hex[:12]}"
    try:
        session = _await(client.sessions.create(external_id=external_id))
    except Exception as exc:
        logger.error("Failed to create session: %s", exc)
        raise RuntimeError(
            f"Cannot create session in project {project_id}. "
            f"Make sure your API key is scoped to this project. "
            f"Error: {exc}"
        ) from exc
    sid: str = session.id if hasattr(session, "id") else session["id"]
    logger.info("Created session: %s (id=%s)", external_id, sid)
    return sid, external_id


def _messages_to_dicts(memory: OZMemory) -> list[dict[str, str]]:
    """Load messages from ``OZMemory`` as role/content dicts."""
    try:
        raw = _await(memory.chat_memory.aget_messages())
        return [
            {
                "role": "user" if isinstance(m, HumanMessage) else "assistant",
                "content": m.content,
            }
            for m in raw
        ]
    except Exception as exc:
        logger.warning("Failed to load messages: %s", exc)
        return []


# ── Initialize Session State ──────────────────────────────────────────────────

if "user_id" not in st.session_state:
    st.session_state.user_id = _ensure_user(_async_oz, DEFAULT_USER_EXTERNAL_ID)
    st.session_state.user_external_id = DEFAULT_USER_EXTERNAL_ID
    logger.info("User initialized: %s", st.session_state.user_id)

if "project_id" not in st.session_state:
    st.session_state.project_id = _ensure_project(_async_oz, PROJECT_NAME)
    logger.info("Project initialized: %s", st.session_state.project_id)

if "session_id" not in st.session_state:
    sessions = _list_sessions(_async_oz, st.session_state.project_id)
    if sessions:
        latest = sessions[0]
        st.session_state.session_id = (
            latest.id if hasattr(latest, "id") else latest["id"]
        )
        st.session_state.session_external_id = (
            latest.external_id if hasattr(latest, "external_id") else latest["external_id"]
        )
    else:
        try:
            sid, ext = _create_session(_async_oz, st.session_state.project_id)
        except RuntimeError as exc:
            st.error(str(exc))
            st.info(
                "To use this app you need a project-scoped API key. "
                "Either:\n"
                "1. Set `PROJECT_ID` in `.env` with an existing project UUID, or\n"
                "2. Create a project-scoped API key in the OpenZync dashboard "
                "and use it in `.env`."
            )
            st.stop()
        st.session_state.session_id = sid
        st.session_state.session_external_id = ext

    # Create OZMemory instance for the active session.
    st.session_state.memory = OZMemory(
        session_id=st.session_state.session_id,
        project_id=st.session_state.project_id,
        client=_async_oz,
        return_messages=True,
        max_messages=500,
    )
    st.session_state.messages = _messages_to_dicts(st.session_state.memory)
    logger.info(
        "Session initialized: %s (%s messages)",
        st.session_state.session_external_id,
        len(st.session_state.messages),
    )

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🧠 OpenZync Chat")
    st.caption(f"User: `{st.session_state.user_external_id}`")

    # Connection status
    st.divider()
    st.subheader("Status")
    st.success("✅ OpenZync", icon="✅")
    st.success("✅ OpenRouter", icon="✅")

    # Session list
    st.divider()
    st.subheader("Sessions")

    sessions_list = _list_sessions(_async_oz, st.session_state.project_id)

    for s in sessions_list:
        s_id = s.id if hasattr(s, "id") else s["id"]
        s_ext = s.external_id if hasattr(s, "external_id") else s["external_id"]
        s_msg_count = (
            s.message_count if hasattr(s, "message_count") else s.get("message_count", 0)
        )
        is_active = s_id == st.session_state.session_id
        label = f"{'▶ ' if is_active else ''}{s_ext}"
        if s_msg_count:
            label += f"  ({s_msg_count})"

        if st.button(
            label,
            key=f"sid_{s_id}",
            use_container_width=True,
            type="primary" if is_active else "secondary",
        ):
            st.session_state.session_id = s_id
            st.session_state.session_external_id = s_ext
            st.session_state.memory = OZMemory(
                session_id=s_id,
                project_id=st.session_state.project_id,
                client=_async_oz,
                return_messages=True,
                max_messages=500,
            )
            st.session_state.messages = _messages_to_dicts(st.session_state.memory)
            st.rerun()

    # New session button
    st.divider()
    if st.button("+ New Session", use_container_width=True, type="primary"):
        try:
            sid, ext = _create_session(_async_oz, st.session_state.project_id)
        except RuntimeError as exc:
            st.error(str(exc))
            st.rerun()
        st.session_state.session_id = sid
        st.session_state.session_external_id = ext
        st.session_state.memory = OZMemory(
            session_id=sid,
            project_id=st.session_state.project_id,
            client=_async_oz,
            return_messages=True,
            max_messages=500,
        )
        st.session_state.messages = []
        st.rerun()

    st.divider()
    st.caption("Powered by OpenZync + OpenRouter")

# ── Main Chat Area ────────────────────────────────────────────────────────────

st.title(f"💬 {st.session_state.session_external_id}")
st.caption(f"Session ID: `{st.session_state.session_id}`")

# Render existing messages.
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# ── Chat Input ────────────────────────────────────────────────────────────────

if prompt := st.chat_input("Type a message..."):
    # ── Display user message ──
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # ── Persist user message via LangChain integration ──
    try:
        _await(st.session_state.memory.chat_memory.aadd_messages(
            [HumanMessage(content=prompt)]
        ))
        logger.info("Stored user message via OZMemory integration")
    except Exception as exc:
        logger.error("Failed to store user message: %s", exc)
        st.error(f"Failed to store message: {exc}")

    # ── Retrieve LLM context from OpenZync ──
    context_text = ""
    try:
        context_resp = _await(
            st.session_state.memory.get_context(query=prompt, limit=10)
        )
        context_text = context_resp.context if context_resp.context else ""
    except Exception as exc:
        logger.warning("Could not retrieve context: %s", exc)

    # ── Build LLM message payload ──
    system_content = (
        "You are a helpful assistant with access to conversation memory. "
        "Answer the user's question based on the conversation history and your knowledge."
    )
    if context_text:
        system_content += f"\n\n## Relevant Context\n\n{context_text}"

    llm_messages: list[dict[str, str]] = [
        {"role": "system", "content": system_content},
    ]

    # Add recent conversation history (last 20 messages).
    for m in st.session_state.messages[-20:]:
        llm_messages.append({"role": m["role"], "content": m["content"]})

    # ── Call OpenRouter ──
    reply = ""
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                response = llm.chat.completions.create(
                    model=OPENROUTER_MODEL,
                    messages=llm_messages,
                    max_tokens=1024,
                    temperature=0.7,
                    extra_headers={
                        "HTTP-Referer": "https://openzync-chat.streamlit.app",
                        "X-Title": "OpenZync Chat",
                    },
                )
                reply = response.choices[0].message.content or ""
            except Exception as exc:
                logger.error("OpenRouter request failed: %s", exc)
                reply = f"I'm sorry, I encountered an error communicating with the LLM: {exc}"

        if reply:
            st.markdown(reply)

    # ── Store assistant response via LangChain integration ──
    if reply:
        st.session_state.messages.append({"role": "assistant", "content": reply})
        try:
            _await(st.session_state.memory.chat_memory.aadd_messages(
                [AIMessage(content=reply)]
            ))
            logger.info("Stored assistant response (%d chars) via integration", len(reply))
        except Exception as exc:
            logger.error("Failed to store assistant response: %s", exc)
            st.error(f"Failed to store assistant response: {exc}")
