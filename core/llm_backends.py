"""Concrete LLM backend implementations.

Each backend registers itself with :class:`LLMBackendRegistry` at module load
time so that ``resolve_backend`` can discover it by name.

Supported backends
------------------
* :class:`OllamaBackend` — local LLMs via Ollama (no API key required)
* :class:`OpenAIBackend` — OpenAI API (GPT-4o, GPT-4o-mini, etc.)
* :class:`AzureBackend` — Azure OpenAI service
* :class:`AnthropicBackend` — Anthropic API (Claude models)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, ClassVar

import httpx

from core.exceptions import LLMConfigurationError
from core.llm import (
    ChatResponse,
    EmbeddingResponse,
    LLMBackend,
    LLMBackendRegistry,
    TokenUsage,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Ollama
# ═══════════════════════════════════════════════════════════════════════════════


class OllamaBackend(LLMBackend):
    """LLM backend powered by a local Ollama instance.

    Requires no API key.  Connects to the Ollama REST API at the configured
    base URL (default ``http://localhost:11434``).

    Default models:
        Chat: ``llama3.2:3b``
        Embeddings: ``nomic-embed-text`` (768 dimensions)
    """

    DEFAULT_CHAT_MODEL: ClassVar[str] = "llama3.2:3b"
    DEFAULT_EMBED_MODEL: ClassVar[str] = "nomic-embed-text"
    DEFAULT_EMBED_DIM: ClassVar[int] = 768

    def __init__(self, base_url: str = "http://localhost:11434") -> None:
        self._base_url = base_url.rstrip("/")
        # Model defaults are class constants — no env-var fallback.
        self._chat_model = self.DEFAULT_CHAT_MODEL
        self._embed_model = self.DEFAULT_EMBED_MODEL

    # ── LLMBackend ─────────────────────────────────────────────────────────

    @property
    def model_name(self) -> str:
        return self._chat_model

    @property
    def embedding_dim(self) -> int:
        return self.DEFAULT_EMBED_DIM

    async def _chat(self, messages: list[dict], **kwargs: Any) -> ChatResponse:
        """Send a chat completion request to Ollama's ``/api/chat``.

        Supported kwargs (forwarded to Ollama):
            ``model``, ``temperature``, ``top_p``, ``max_tokens``, ``stream``.
        """
        model = kwargs.pop("model", self._chat_model)
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "num_predict": 2048,
            **kwargs,
        }

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(f"{self._base_url}/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "ollama.chat_http_error",
                extra={
                    "status_code": exc.response.status_code,
                    "detail": exc.response.text[:500],
                    "model": model,
                },
            )
            raise
        except httpx.TimeoutException:
            logger.error("ollama.chat_timeout", extra={"model": model})
            raise

        elapsed = time.monotonic() - start
        content: str = data.get("message", {}).get("content", "")

        usage_data = data.get("metrics", {})
        usage = TokenUsage(
            prompt_tokens=usage_data.get("prompt_eval_count", 0) or 0,
            completion_tokens=usage_data.get("eval_count", 0) or 0,
        )

        logger.info(
            "llm.chat_completed",
            extra={
                "provider": "ollama",
                "model": model,
                "duration_ms": round(elapsed * 1000),
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
            },
        )

        return ChatResponse(content=content, model=data.get("model", model), usage=usage)

    async def embed(self, texts: list[str], **kwargs: Any) -> EmbeddingResponse:
        """Generate embeddings via Ollama's ``/api/embeddings`` endpoint.

        Supported kwargs:
            ``model`` — override the embedding model.
        """
        model = kwargs.pop("model", self._embed_model)
        payload = {
            "model": model,
            "prompt": texts[0] if len(texts) == 1 else texts,
        }

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(f"{self._base_url}/api/embeddings", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "ollama.embed_http_error",
                extra={
                    "status_code": exc.response.status_code,
                    "model": model,
                },
            )
            raise
        except httpx.TimeoutException:
            logger.error("ollama.embed_timeout", extra={"model": model})
            raise

        # Handle both /api/embeddings (singular) and /api/embed (plural) response formats
        raw = data.get("embeddings") or data.get("embedding")
        if raw and isinstance(raw, list):
            if raw and isinstance(raw[0], float):
                # /api/embeddings returns a single embedding vector
                embeddings = [raw]
            else:
                # /api/embed returns a list of embedding vectors
                embeddings = raw
        else:
            embeddings = []
        if not embeddings:
            raise ValueError(f"Empty embedding response for model {model}. Response: {str(data)[:200]}")
        dim = len(embeddings[0])

        logger.info(
            "llm.embed_completed",
            extra={
                "provider": "ollama",
                "model": model,
                "num_texts": len(texts),
                "dim": dim,
            },
        )

        return EmbeddingResponse(embeddings=embeddings, model=data.get("model", model), dim=dim)


# ═══════════════════════════════════════════════════════════════════════════════
# OpenAI
# ═══════════════════════════════════════════════════════════════════════════════


class OpenAIBackend(LLMBackend):
    """LLM backend for the OpenAI API.

    Uses the official ``openai`` library with ``AsyncOpenAI`` client.
    Supports GPT-4o, GPT-4o-mini, GPT-4-turbo, and all OpenAI chat models.

    Handles 429 rate limits with exponential backoff (up to 3 retries).
    """

    DEFAULT_MODEL: ClassVar[str] = "gpt-4o-mini"
    DEFAULT_EMBED_MODEL: ClassVar[str] = "text-embedding-3-small"
    DEFAULT_EMBED_DIM: ClassVar[int] = 1536
    MAX_RETRIES: ClassVar[int] = 3

    def __init__(self, api_key: str, model: str | None = None) -> None:
        if not api_key:
            raise ValueError("OpenAI API key is required")

        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key)
        self._chat_model: str = model or self.DEFAULT_MODEL
        self._embed_model: str = self.DEFAULT_EMBED_MODEL

    # ── LLMBackend ─────────────────────────────────────────────────────────

    @property
    def model_name(self) -> str:
        return self._chat_model

    @property
    def embedding_dim(self) -> int:
        return self.DEFAULT_EMBED_DIM

    async def _chat(self, messages: list[dict], **kwargs: Any) -> ChatResponse:
        """Send a chat completion request.

        Supported kwargs: ``temperature``, ``max_tokens``, ``top_p``,
        ``frequency_penalty``, ``presence_penalty``, ``stop``, ``model``.
        """
        model = kwargs.pop("model", self._chat_model)
        temperature = kwargs.pop("temperature", 0.0)

        last_exception: Exception | None = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                start = time.monotonic()
                response = await self._client.chat.completions.create(
                    model=model,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=temperature,
                    **kwargs,
                )
                elapsed = time.monotonic() - start

                content = response.choices[0].message.content or ""
                usage_data = response.usage
                usage = TokenUsage(
                    prompt_tokens=usage_data.prompt_tokens if usage_data else 0,
                    completion_tokens=usage_data.completion_tokens if usage_data else 0,
                )

                logger.info(
                    "llm.chat_completed",
                    extra={
                        "provider": "openai",
                        "model": model,
                        "duration_ms": round(elapsed * 1000),
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                    },
                )

                return ChatResponse(content=content, model=model, usage=usage)

            except Exception as exc:
                last_exception = exc
                # Retry on 429 (rate limit) or 5xx server errors.
                if hasattr(exc, "status_code") and exc.status_code in (429, 500, 502, 503):
                    wait = 2**attempt  # exponential backoff: 2, 4, 8s
                    logger.warning(
                        "openai.retrying",
                        extra={
                            "attempt": attempt,
                            "status": getattr(exc, "status_code", None),
                            "wait_seconds": wait,
                        },
                    )
                    await asyncio.sleep(wait)
                    continue
                # Non-retryable error — raise immediately.
                logger.error(
                    "openai.chat_error",
                    extra={"error": str(exc), "model": model},
                )
                raise

        # All retries exhausted.
        logger.error(
            "openai.chat_retries_exhausted",
            extra={"model": model, "last_error": str(last_exception)},
        )
        raise RuntimeError(f"OpenAI chat failed after {self.MAX_RETRIES} retries: {last_exception}") from last_exception

    async def embed(self, texts: list[str], **kwargs: Any) -> EmbeddingResponse:
        """Generate embeddings via OpenAI's embeddings API.

        Supported kwargs:
            ``model`` — override the embedding model.
        """
        model = kwargs.pop("model", self._embed_model)

        try:
            response = await self._client.embeddings.create(
                model=model,
                input=texts,
            )
        except Exception as exc:
            logger.error(
                "openai.embed_error",
                extra={"error": str(exc), "model": model},
            )
            raise

        embeddings = [item.embedding for item in response.data]
        dim = len(embeddings[0]) if embeddings else self.DEFAULT_EMBED_DIM

        logger.info(
            "llm.embed_completed",
            extra={
                "provider": "openai",
                "model": model,
                "num_texts": len(texts),
                "dim": dim,
            },
        )

        return EmbeddingResponse(
            embeddings=embeddings,
            model=model,
            dim=dim,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Azure OpenAI
# ═══════════════════════════════════════════════════════════════════════════════


class AzureBackend(LLMBackend):
    """LLM backend for Azure OpenAI Service.

    Uses the ``AzureOpenAI`` client from the ``openai`` library.
    Configured via endpoint URL, API key, and deployment name.
    """

    DEFAULT_EMBED_DIM: ClassVar[int] = 1536
    MAX_RETRIES: ClassVar[int] = 3

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        deployment: str,
    ) -> None:
        if not endpoint:
            raise ValueError("Azure OpenAI endpoint is required")
        if not api_key:
            raise ValueError("Azure OpenAI API key is required")
        if not deployment:
            raise ValueError("Azure OpenAI deployment name is required")

        from openai import AsyncAzureOpenAI

        self._client = AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version="2024-08-01-preview",
        )
        self._chat_model: str = deployment
        self._embed_model: str = deployment

    # ── LLMBackend ─────────────────────────────────────────────────────────

    @property
    def model_name(self) -> str:
        return self._chat_model

    @property
    def embedding_dim(self) -> int:
        return self.DEFAULT_EMBED_DIM

    async def _chat(self, messages: list[dict], **kwargs: Any) -> ChatResponse:
        """Send a chat completion request to Azure OpenAI.

        Supported kwargs: ``temperature``, ``max_tokens``, ``top_p``, etc.
        The model parameter is mapped to the Azure deployment name.
        """
        deployment = kwargs.pop("model", self._chat_model)
        temperature = kwargs.pop("temperature", 0.0)

        last_exception: Exception | None = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                start = time.monotonic()
                response = await self._client.chat.completions.create(
                    model=deployment,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=temperature,
                    **kwargs,
                )
                elapsed = time.monotonic() - start

                content = response.choices[0].message.content or ""
                usage_data = response.usage
                usage = TokenUsage(
                    prompt_tokens=usage_data.prompt_tokens if usage_data else 0,
                    completion_tokens=usage_data.completion_tokens if usage_data else 0,
                )

                logger.info(
                    "llm.chat_completed",
                    extra={
                        "provider": "azure",
                        "model": deployment,
                        "duration_ms": round(elapsed * 1000),
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                    },
                )

                return ChatResponse(content=content, model=deployment, usage=usage)

            except Exception as exc:
                last_exception = exc
                if hasattr(exc, "status_code") and exc.status_code in (429, 500, 502, 503):
                    wait = 2**attempt
                    logger.warning(
                        "azure.retrying",
                        extra={"attempt": attempt, "wait_seconds": wait},
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.error("azure.chat_error", extra={"error": str(exc)})
                raise

        raise RuntimeError(
            f"Azure OpenAI chat failed after {self.MAX_RETRIES} retries: {last_exception}"
        ) from last_exception

    async def embed(self, texts: list[str], **kwargs: Any) -> EmbeddingResponse:
        """Generate embeddings via Azure OpenAI.

        Supported kwargs:
            ``model`` — override the deployment (defaults to the chat deployment).
        """
        deployment = kwargs.pop("model", self._embed_model)

        try:
            response = await self._client.embeddings.create(
                model=deployment,
                input=texts,
            )
        except Exception as exc:
            logger.error("azure.embed_error", extra={"error": str(exc)})
            raise

        embeddings = [item.embedding for item in response.data]
        dim = len(embeddings[0]) if embeddings else self.DEFAULT_EMBED_DIM

        return EmbeddingResponse(embeddings=embeddings, model=deployment, dim=dim)


# ═══════════════════════════════════════════════════════════════════════════════
# Anthropic
# ═══════════════════════════════════════════════════════════════════════════════


class AnthropicBackend(LLMBackend):
    """LLM backend for the Anthropic API (Claude models).

    Uses the official ``anthropic`` library.
    Embeddings are **not supported** — calling ``embed()`` raises
    ``NotImplementedError``.
    """

    DEFAULT_MODEL: ClassVar[str] = "claude-sonnet-4-20250514"
    MAX_RETRIES: ClassVar[int] = 3

    def __init__(self, api_key: str, model: str | None = None) -> None:
        if not api_key:
            raise ValueError("Anthropic API key is required")

        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key)
        self._model: str = model or self.DEFAULT_MODEL

    # ── LLMBackend ─────────────────────────────────────────────────────────

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def embedding_dim(self) -> int:
        return 0  # Anthropic does not offer a public embedding API.

    async def _chat(self, messages: list[dict], **kwargs: Any) -> ChatResponse:
        """Send a chat completion request to the Anthropic API.

        Handles Anthropic's ``system`` message convention: if the first
        message has ``role == "system"``, it is extracted and passed as the
        ``system`` parameter (omitted from the messages array).

        Supported kwargs: ``max_tokens``, ``temperature``, ``top_p``,
        ``top_k``, ``stop_sequences``, ``model``.
        """
        model = kwargs.pop("model", self._model)
        max_tokens = kwargs.pop("max_tokens", 4096)
        temperature = kwargs.pop("temperature", 0.0)

        # Anthropic requires a separate ``system`` parameter.
        system: str | None = None
        anthropic_messages = messages
        if messages and messages[0].get("role") == "system":
            system = messages[0]["content"]
            anthropic_messages = messages[1:]

        last_exception: Exception | None = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                start = time.monotonic()
                response = await self._client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                    messages=anthropic_messages,  # type: ignore[arg-type]
                    **kwargs,
                )
                elapsed = time.monotonic() - start

                # Extract text from content blocks.
                content_parts = [
                    block.text for block in response.content if block.type == "text"
                ]
                content = "\n".join(content_parts)

                usage = TokenUsage(
                    prompt_tokens=response.usage.input_tokens or 0,
                    completion_tokens=response.usage.output_tokens or 0,
                )

                logger.info(
                    "llm.chat_completed",
                    extra={
                        "provider": "anthropic",
                        "model": model,
                        "duration_ms": round(elapsed * 1000),
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                    },
                )

                return ChatResponse(content=content, model=model, usage=usage)

            except Exception as exc:
                last_exception = exc
                # Anthropic SDK raises `anthropic.RateLimitError` and
                # `anthropic.APIStatusError` for 5xx.
                exc_name = type(exc).__name__
                if "RateLimit" in exc_name or "APIStatusError" in exc_name:
                    status = getattr(exc, "status_code", None)
                    if status in (429, 500, 502, 503):
                        wait = 2**attempt
                        logger.warning(
                            "anthropic.retrying",
                            extra={"attempt": attempt, "wait_seconds": wait},
                        )
                        await asyncio.sleep(wait)
                        continue
                logger.error("anthropic.chat_error", extra={"error": str(exc)})
                raise

        raise RuntimeError(
            f"Anthropic chat failed after {self.MAX_RETRIES} retries: {last_exception}"
        ) from last_exception

    async def embed(self, texts: list[str], **kwargs: Any) -> EmbeddingResponse:
        """Embeddings are not supported by the Anthropic API."""
        raise NotImplementedError(
            "Anthropic does not offer a public embedding API. "
            "Use a different backend (Ollama, OpenAI, or Azure) for embeddings."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# OpenRouter
# ═══════════════════════════════════════════════════════════════════════════════


class OpenRouterBackend(LLMBackend):
    """LLM backend powered by OpenRouter's unified API.

    Uses the OpenAI-compatible client pointed at ``https://openrouter.ai/api/v1``.
    The API key must be provided via the constructor (from per-org config).
    There is no env-var fallback.

    Default model: ``openai/gpt-oss-120b:free`` (no-cost tier).

    Does **not** support embeddings — use a different backend (Ollama, OpenAI,
    or Azure) for pgvector embedding generation.
    """

    DEFAULT_CHAT_MODEL: ClassVar[str] = "openai/gpt-oss-120b:free"
    BASE_URL: ClassVar[str] = "https://openrouter.ai/api/v1"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        from openai import AsyncOpenAI

        if not api_key:
            raise LLMConfigurationError(
                "OpenRouter API key is required. "
                "Set it via PATCH /admin/org/config."
            )

        self._client = AsyncOpenAI(
            base_url=self.BASE_URL,
            api_key=api_key,
            default_headers={
                "HTTP-Referer": "https://github.com/rohnsha0/openzync",
                "X-OpenRouter-Title": "OpenZync - Agent Memory Platform",
            },
        )
        self._chat_model = model or self.DEFAULT_CHAT_MODEL

    @property
    def model_name(self) -> str:
        return self._chat_model

    @property
    def embedding_dim(self) -> int:
        raise NotImplementedError("OpenRouter does not support embeddings")

    async def _chat(self, messages: list[dict], **kwargs: Any) -> ChatResponse:
        """Send a chat completion via OpenRouter."""
        import time

        model = kwargs.pop("model", self._chat_model)
        temperature = kwargs.pop("temperature", 0.1)
        max_tokens = kwargs.pop("max_tokens", 1024)

        start = time.monotonic()
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
        except Exception as exc:
            raise

        elapsed = time.monotonic() - start
        choice = response.choices[0]
        usage = TokenUsage(
            prompt_tokens=response.usage.prompt_tokens if response.usage else 0,
            completion_tokens=response.usage.completion_tokens if response.usage else 0,
        )

        logger.info(
            "openrouter.chat_completed",
            extra={
                "model": response.model,
                "duration_ms": round(elapsed * 1000),
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
            },
        )

        return ChatResponse(
            content=choice.message.content or "",
            model=response.model,
            usage=usage,
        )

    async def embed(self, texts: list[str], **kwargs: Any) -> EmbeddingResponse:
        """Embeddings are not supported by OpenRouter."""
        raise NotImplementedError(
            "OpenRouter does not offer an embedding API. "
            "Use a different backend (Ollama, OpenAI, or Azure) for embeddings."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Auto-registration with the global registry
# ═══════════════════════════════════════════════════════════════════════════════

LLMBackendRegistry.register("ollama", OllamaBackend)
LLMBackendRegistry.register("openai", OpenAIBackend)
LLMBackendRegistry.register("azure", AzureBackend)
LLMBackendRegistry.register("anthropic", AnthropicBackend)
LLMBackendRegistry.register("openrouter", OpenRouterBackend)
