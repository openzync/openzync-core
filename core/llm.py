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

import orjson
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, ValidationError

from core.exceptions import LLMStructuredOutputError

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
    """Uniform response from any LLM chat backend.

    When ``response_model`` was passed to :meth:`LLMBackend.chat` and
    validation succeeded, :attr:`validated_data` holds the parsed Pydantic
    model instance so callers can access typed fields directly instead
    of re-parsing ``content``.
    """

    content: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    validated_data: BaseModel | None = None


@dataclass
class EmbeddingResponse:
    """Uniform response from any embedding backend."""

    embeddings: list[list[float]]
    model: str
    dim: int


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _last_validation_error(content: str, model: type[BaseModel]) -> str:
    """Return a short diagnostic message for a validation failure.

    Tries to parse *content* and validate it against *model*, then returns
    the validation error string.  If even JSON parsing fails, returns a
    message indicating that.
    """
    # Try JSON parse first
    try:
        parsed: Any = orjson.loads(content.encode())
    except orjson.JSONDecodeError as exc:
        return f"Invalid JSON: {exc}"

    # Try model validation
    try:
        model.model_validate(parsed)
    except Exception as exc:
        return str(exc)

    return "Unknown validation error"


# ═══════════════════════════════════════════════════════════════════════════════
# Abstract backend
# ═══════════════════════════════════════════════════════════════════════════════


class LLMBackend(ABC):
    """Abstract base class for all LLM providers.

    Subclasses implement ``_chat`` and ``embed`` using the provider's SDK or
    HTTP API.  Every backend reports which model it is using and the embedding
    dimensionality.

    The public ``chat`` method adds optional structured-output validation:
    when a ``response_model`` is provided, the method auto-injects a system
    prompt with the expected JSON schema, validates the response against the
    model, and retries up to ``validation_retries`` times on failure with
    error-context feedback in the retry messages.
    """

    #: Number of validation retries when a Pydantic ``response_model`` is
    #: provided but the LLM output fails to parse into it.
    VALIDATION_RETRIES: int = 2

    async def chat(
        self,
        messages: list[dict],
        response_model: type[BaseModel] | None = None,
        validation_retries: int | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """Send a chat completion, optionally validating against a Pydantic model.

        When ``response_model`` is ``None`` (default) this method delegates
        directly to ``_chat()`` — the per-backend provider call.

        When ``response_model`` is provided:

        1. A system instruction with the model's JSON schema is injected into
           *messages* so the LLM knows the expected output shape.
        2. The provider is called via ``_chat()``.
        3. The response is parsed and validated against *response_model*.
        4. On success the ``ChatResponse`` is returned as-is.
        5. On failure the conversation history is amended with the bad output
           and a retry prompt explaining *why* it failed, then the provider
           is called again.
        6. After exhausting ``validation_retries`` attempts a
           :class:`LLMStructuredOutputError` is raised.

        Args:
            messages: List of message dicts with ``role`` and ``content``
                keys, following the OpenAI message format.
            response_model: Optional Pydantic model to validate the output
                content against.  When provided, the LLM is instructed to
                emit JSON matching the model's schema.
            validation_retries: Override for the number of validation retry
                attempts.  Defaults to :attr:`VALIDATION_RETRIES`.
            **kwargs: Additional provider-specific parameters (temperature,
                max_tokens, top_p, etc.).

        Returns:
            A ``ChatResponse`` with the generated text and token usage.

        Raises:
            LLMStructuredOutputError: If the output cannot be validated
                against *response_model* after exhausting retries.
        """
        retries = (
            validation_retries
            if validation_retries is not None
            else self.VALIDATION_RETRIES
        )

        # No schema — fast path, delegate directly.
        if response_model is None:
            return await self._chat(messages, **kwargs)

        messages = self._inject_schema_instr(messages, response_model)

        for attempt in range(retries + 1):
            response: ChatResponse = await self._chat(messages, **kwargs)

            # ── Try clean model_validate_json first ───────────────────────────
            try:
                response.validated_data = response_model.model_validate_json(
                    response.content
                )
                return response
            except ValidationError:
                pass

            # ── Fallback: strip fences, hunt for JSON, try again ──────────────
            extracted: Any = self._extract_json(response.content)
            if extracted is not None:
                try:
                    response.validated_data = response_model.model_validate(
                        extracted
                    )
                    # Normalise content to clean JSON so callers can use
                    # ``model_validate_json()`` without pre-processing.
                    response.content = orjson.dumps(extracted).decode()
                    return response
                except ValidationError:
                    pass  # fall through to retry

            # ── Retry or exhaust ──────────────────────────────────────────────
            if attempt >= retries:
                raise LLMStructuredOutputError(
                    f"LLM output failed to match {response_model.__name__} "
                    f"after {retries + 1} attempt(s).",
                    model_name=self.model_name,
                    content_preview=response.content[:300],
                    validation_error=_last_validation_error(response.content, response_model),
                )

            messages = self._build_retry_messages(
                messages,
                response.content,
                response_model,
            )

        raise RuntimeError("unreachable")  # pragma: no cover

    @abstractmethod
    async def _chat(self, messages: list[dict], **kwargs: Any) -> ChatResponse:
        """Provider-specific chat implementation.

        Override this in each backend.  The public :meth:`chat` wraps
        this with validation, retry, and structured-output logic.

        Args:
            messages: List of message dicts following OpenAI format.
            **kwargs: Provider-specific parameters.

        Returns:
            A ``ChatResponse``.
        """
        ...

    # ── Structured-output helpers ──────────────────────────────────────────────

    @staticmethod
    def _inject_schema_instr(
        messages: list[dict], model: type[BaseModel]
    ) -> list[dict]:
        """Prepend (or append to existing) system instruction with JSON schema.

        Builds a directive telling the LLM to output valid JSON matching
        the Pydantic model's schema, then injects it into *messages*.

        If the first message already has ``role == "system"`` the schema
        instruction is appended to its content.  Otherwise a new system
        message is prepended.
        """
        schema_json: str = orjson.dumps(
            model.model_json_schema(), option=orjson.OPT_INDENT_2
        ).decode()
        instruction: str = (
            "You MUST respond with valid JSON only. "
            "Do NOT include markdown code blocks, explanations, "
            "or any text outside the JSON object.\n\n"
            f"Expected JSON schema:\n{schema_json}"
        )

        if messages and messages[0].get("role") == "system":
            return [
                {**messages[0], "content": f"{messages[0]['content']}\n\n{instruction}"},
                *messages[1:],
            ]

        return [{"role": "system", "content": instruction}, *messages]

    @staticmethod
    def _build_retry_messages(
        messages: list[dict],
        bad_content: str,
        model: type[BaseModel],
    ) -> list[dict]:
        """Append assistant error + user retry prompt after a validation failure.

        Adds the failed output as an ``assistant`` message and follows it
        with a ``user`` message explaining the failure and repeating the
        expected schema.
        """
        schema_json: str = orjson.dumps(
            model.model_json_schema(), option=orjson.OPT_INDENT_2
        ).decode()
        return [
            *messages,
            {"role": "assistant", "content": bad_content},
            {
                "role": "user",
                "content": (
                    "The previous response was NOT valid JSON matching the "
                    "expected schema.\n\n"
                    "Please try again. Output ONLY valid JSON. "
                    "No markdown fences, no extra text.\n"
                    f"Expected schema:\n{schema_json}"
                ),
            },
        ]

    @staticmethod
    def _extract_json(text: str) -> Any | None:
        """Strip markdown fences and other wrappers, then parse JSON.

        Attempts to recover a JSON value from text that may be wrapped in
        `````json```` markers, preceded by thinking blocks (e.g. deepseek-r1),
        or contain leading/trailing non-JSON text.

        Returns the parsed Python value (dict / list / str / etc.) on
        success, or ``None`` if no valid JSON could be extracted.
        """
        # Strip markdown code fences
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0].strip()

        text = text.strip()

        # Find the first JSON object or array start
        json_start: int = -1
        for c in ("{", "["):
            pos: int = text.find(c)
            if pos >= 0 and (json_start < 0 or pos < json_start):
                json_start = pos

        if json_start < 0:
            return None

        text = text[json_start:]

        try:
            return orjson.loads(text.encode())
        except orjson.JSONDecodeError:
            return None

    # ── Abstract members ───────────────────────────────────────────────────────

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


from core.exceptions import LLMConfigurationError as LLMConfigurationError


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
    exclusively from *config* — there is **no** env-var fallback and **no**
    hardcoded default at this layer.  Every required field must be present
    in *config* or the function raises :class:`LLMConfigurationError`.

    Args:
        provider: One of ``"ollama"``, ``"openai"``, ``"azure"``,
            ``"anthropic"``, ``"openrouter"``.
        config: Optional dict with provider-specific overrides (API keys,
            model names, endpoints).  Required fields vary by provider.

    Returns:
        An initialised ``LLMBackend`` instance.

    Raises:
        LLMConfigurationError: If a required config field is missing or empty.
        ValueError: If *provider* is not recognised.
    """
    backend_cls = LLMBackendRegistry.get(provider)

    if provider == "ollama":
        if config is None or not config.get("ollama_base_url"):
            raise LLMConfigurationError(
                "Ollama backend requires ollama_base_url in per-org "
                "configuration.  Set it via PATCH /admin/org/config."
            )
        instance: LLMBackend = backend_cls(base_url=config["ollama_base_url"])  # type: ignore[call-arg]
    elif provider == "openai":
        if config is None or not config.get("openai_api_key"):
            raise LLMConfigurationError(
                "OpenAI backend requires openai_api_key in per-org "
                "configuration.  Set it via PATCH /admin/org/config."
            )
        api_key: str = config["openai_api_key"]
        model: str | None = config.get("openai_model")
        instance = backend_cls(api_key=api_key, model=model)
    elif provider == "azure":
        if config is None or not config.get("azure_endpoint"):
            raise LLMConfigurationError(
                "Azure backend requires azure_endpoint in per-org "
                "configuration.  Set it via PATCH /admin/org/config."
            )
        if config is None or not config.get("azure_api_key"):
            raise LLMConfigurationError(
                "Azure backend requires azure_api_key in per-org "
                "configuration.  Set it via PATCH /admin/org/config."
            )
        if config is None or not config.get("azure_deployment"):
            raise LLMConfigurationError(
                "Azure backend requires azure_deployment in per-org "
                "configuration.  Set it via PATCH /admin/org/config."
            )
        instance = backend_cls(
            endpoint=config["azure_endpoint"],
            api_key=config["azure_api_key"],
            deployment=config["azure_deployment"],
        )
    elif provider == "anthropic":
        if config is None or not config.get("anthropic_api_key"):
            raise LLMConfigurationError(
                "Anthropic backend requires anthropic_api_key in per-org "
                "configuration.  Set it via PATCH /admin/org/config."
            )
        api_key = config["anthropic_api_key"]
        model = config.get("anthropic_model")
        instance = backend_cls(api_key=api_key, model=model)
    elif provider == "openrouter":
        if config is None or not config.get("api_key"):
            raise LLMConfigurationError(
                "OpenRouter backend requires api_key in per-org "
                "configuration.  Set it via PATCH /admin/org/config."
            )
        api_key = config["api_key"]
        model = config.get("model")
        instance = backend_cls(api_key=api_key, model=model)
    else:
        raise ValueError(f"Unknown provider: {provider}")

    return instance
