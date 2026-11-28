"""Usage metering record — tracking-only payload for one LLM call."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """Tracking-only metering payload for a single LLM/embedding call.

    Built by ``LLMBackend.chat`` / ``LLMBackend.embed`` from the
    provider response and handed to the attached usage sink.
    The sink fills ``user_id``/``worker``/entity ids from its baked scope
    when the record leaves them empty — callers never set them directly.
    """

    user_id: UUID | None = None
    provider: str = ""
    model: str = ""
    worker: str = ""
    project_id: UUID | None = None
    episode_id: UUID | None = None
    session_id: UUID | None = None
    community_id: UUID | None = None
    task_run_id: UUID | None = None
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    embed_count: int | None = None
    embed_dim: int | None = None
    idempotency_key: str = ""
