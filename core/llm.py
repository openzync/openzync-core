"""BYOK (Bring Your Own Key) LLM abstraction.

Defines the ``LLMBackend`` abstract base class, the ``LLMBackendRegistry``
for provider registration, and the ``resolve_backend`` factory that chains
org-level config → explicit argument → environment variable → auto-detect.

Usage::

    from core.llm import resolve_backend, LLMBackend

    backend: LLMBackend = await resolve_backend(provider="openai")
    response = await backend.chat([{"role": "user", "content": "Hello"}])
    print(response.content)
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════════


class LLMProvider(str, Enum):
    """Supported LLM provider identifiers."""

    OLLAMA = "ollama"
    OPENAI = "openai"
    AZURE = "azure"
    ANTHROPIC = "anthropic"


@dataclass
class TokenUsage:
    """Token consumption report for a single LLM call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Sum of prompt and completion tokens."""
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ChatResponse:
    """Uniform response from any LLM chat backend."""

    content: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass
class EmbeddingResponse:
    """Uniform response from any embedding backend."""

    embeddings: list[list[float]]
    model: str
    dim: int


# ═══════════════════════════════════════════════════════════════════════════════
# Abstract backend
# ═══════════════════════════════════════════════════════════════════════════════


class LLMBackend(ABC):
    """Abstract base class for all LLM providers.

    Subclasses implement ``chat`` and ``embed`` using the provider's SDK or
    HTTP API.  Every backend reports which model it is using and the embedding
    dimensionality.
    """

    @abstractmethod
    async def chat(self, messages: list[dict], **kwargs: Any) -> ChatResponse:
        """Send a chat completion request.

        Args:
            messages: List of message dicts with ``role`` and ``content``
                keys, following the OpenAI message format.
            **kwargs: Additional provider-specific parameters (temperature,
                max_tokens, top_p, etc.).

        Returns:
            A ``ChatResponse`` with the generated text and token usage.
        """
        ...

    @abstractmethod
    async def embed(self, texts: list[str], **kwargs: Any) -> EmbeddingResponse:
        """Generate embeddings for one or more text strings.

        Args:
            texts: List of input strings to embed.
            **kwargs: Additional provider-specific parameters.

        Returns:
            An ``EmbeddingResponse`` containing the embedding vectors.
        """
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """The model identifier currently in use (e.g. ``"gpt-4o"``)."""
        ...

    @property
    @abstractmethod
    def embedding_dim(self) -> int:
        """Dimensionality of the embedding vectors produced by this backend.

        Returns 0 if the backend does not support embeddings.
        """
        ...


# ═══════════════════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════════════════


class LLMBackendRegistry:
    """Registry of available LLM backend *classes* (not instances).

    Backends register themselves at import time via the ``@register``
    decorator or an explicit ``register()`` call.  The registry is used by
    ``resolve_backend`` to look up the correct class by name.
    """

    _backends: dict[str, type[LLMBackend]] = {}

    @classmethod
    def register(cls, name: str, backend_cls: type[LLMBackend]) -> None:
        """Register a backend class under a provider name.

        Args:
            name: Provider name (e.g. ``"openai"``).  Must match
                :class:`LLMProvider` values.
            backend_cls: The class to instantiate when this provider is
                selected.

        Raises:
            ValueError: If a backend with the same name is already registered.
        """
        if name in cls._backends:
            raise ValueError(
                f"LLM backend '{name}' is already registered as {cls._backends[name].__name__}"
            )
        cls._backends[name] = backend_cls
        logger.debug("llm.backend_registered", extra={"backend_name": name, "cls": backend_cls.__name__})

    @classmethod
    def get(cls, name: str) -> type[LLMBackend]:
        """Look up a registered backend class by provider name.

        Args:
            name: Provider name.

        Returns:
            The registered backend class.

        Raises:
            ValueError: If the provider name is not registered.
        """
        if name not in cls._backends:
            raise ValueError(
                f"Unknown LLM backend: '{name}'. "
                f"Available: {', '.join(cls.list_available())}"
            )
        return cls._backends[name]

    @classmethod
    def list_available(cls) -> list[str]:
        """List all registered provider names."""
        return list(cls._backends.keys())


# ── Concrete backend imports (register at module load) ─────────────────────

# Use lazy import to avoid circular dependency: llm.py imports llm_backends.py
# which imports llm.py.  importlib breaks the cycle by not requiring specific
# names from the partially-initialised module.
import importlib
importlib.import_module("core.llm_backends")


# ═══════════════════════════════════════════════════════════════════════════════
# Resolution
# ═══════════════════════════════════════════════════════════════════════════════


class LLMConfigurationError(Exception):
    """Raised when no LLM backend can be resolved from available configuration."""

    def __init__(self, message: str = "No LLM backend configured.") -> None:
        self.message = message
        super().__init__(self.message)


async def resolve_backend(
    provider: str | None = None,
    org_config: dict | None = None,
) -> LLMBackend:
    """Resolve the appropriate LLM backend via org config or explicit argument.

    The resolution order is:

    1. **Org-level config** — ``org_config.get("llm_backend")``
    2. **Explicit argument** — the ``provider`` parameter
    3. **Error** — raises :class:`LLMConfigurationError`

    Args:
        provider: Explicit override.  If provided, org config is skipped.
        org_config: Optional dict with per-organisation LLM settings.
            Supported keys: ``llm_backend``, ``ollama_base_url``,
            ``openai_api_key``, ``openai_model``, ``azure_endpoint``,
            ``azure_api_key``, ``azure_deployment``, ``anthropic_api_key``,
            ``anthropic_model``.

    Returns:
        An initialised ``LLMBackend`` instance.

    Raises:
        LLMConfigurationError: If no backend could be resolved.
        ValueError: If the resolved provider name is unknown.
    """
    provider_name: str | None = None

    # 1. Org-level config (skip if explicit provider given).
    if provider is None and org_config and org_config.get("llm_backend"):
        provider_name = org_config["llm_backend"]
        logger.debug(
            "llm.resolved_from_org_config",
            extra={"provider": provider_name},
        )
        return await _create_backend(provider_name, org_config)

    # 2. Explicit argument.
    if provider is not None:
        provider_name = provider
        logger.debug("llm.resolved_from_argument", extra={"provider": provider_name})
        return await _create_backend(provider_name, org_config)

    # 3. Nothing worked.
    raise LLMConfigurationError(
        "No LLM backend configured.  Pass a provider argument or set "
        "llm_backend in the per-org configuration."
    )


# ── Internal helpers ─────────────────────────────────────────────────────────


async def _create_backend(provider: str, config: dict | None = None) -> LLMBackend:
    """Instantiate an LLM backend for *provider*, passing optional config.

    All provider-specific values (API keys, model names, endpoints) come
    exclusively from *config* — there is no env-var fallback.  If *config*
    does not contain a required value, the backend class uses its own
    hardcoded default (e.g. ``OllamaBackend.DEFAULT_CHAT_MODEL``) or the
    upstream library raises an auth/connection error.

    Args:
        provider: One of ``"ollama"``, ``"openai"``, ``"azure"``,
            ``"anthropic"``, ``"openrouter"``.
        config: Optional dict with provider-specific overrides (API keys,
            model names, endpoints).

    Returns:
        An initialised ``LLMBackend`` instance.

    Raises:
        ValueError: If *provider* is not recognised.
    """
    backend_cls = LLMBackendRegistry.get(provider)

    if provider == "ollama":
        base_url = (
            config.get("ollama_base_url", "http://localhost:11434")
            if config
            else "http://localhost:11434"
        )
        instance: LLMBackend = backend_cls(base_url=base_url)  # type: ignore[call-arg]
    elif provider == "openai":
        api_key = (
            config.get("openai_api_key", "")
            if config
            else ""
        )
        model = (
            config.get("openai_model", "")
            if config
            else ""
        )
        instance = backend_cls(api_key=api_key, model=model)
    elif provider == "azure":
        endpoint = (
            config.get("azure_endpoint", "")
            if config
            else ""
        )
        api_key = (
            config.get("azure_api_key", "")
            if config
            else ""
        )
        deployment = (
            config.get("azure_deployment", "")
            if config
            else ""
        )
        instance = backend_cls(endpoint=endpoint, api_key=api_key, deployment=deployment)
    elif provider == "anthropic":
        api_key = (
            config.get("anthropic_api_key", "")
            if config
            else ""
        )
        model = (
            config.get("anthropic_model", "claude-sonnet-4-20250514")
            if config
            else "claude-sonnet-4-20250514"
        )
        instance = backend_cls(api_key=api_key, model=model)
    elif provider == "openrouter":
        api_key = (
            config.get("api_key", "")
            if config
            else ""
        )
        model = (
            config.get("model", "")
            if config
            else ""
        )
        instance = backend_cls(api_key=api_key, model=model)
    else:
        raise ValueError(f"Unknown provider: {provider}")

    return instance
