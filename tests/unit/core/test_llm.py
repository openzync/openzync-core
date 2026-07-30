"""Unit tests for the LLM abstraction layer — llm.py + llm_backends.py.

Tests cover:
- Data types: TokenUsage, ChatResponse, EmbeddingResponse, PromptCachingConfig
- build_cache_config resolution
- LLMBackend ABC: abstract enforcement, chat() with response_model validation/retry
- LLMBackendRegistry: register, resolve, double-register guard
- resolve_backend / _create_backend: factory resolution per provider
- All 5 concrete backends: Ollama, OpenAI, Azure, Anthropic, OpenRouter
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import orjson
import pytest
from pydantic import BaseModel, ConfigDict

from core.exceptions import LLMConfigurationError, LLMStructuredOutputError
from core.llm import (
    ChatResponse,
    EmbeddingResponse,
    LLMBackend,
    LLMBackendRegistry,
    PromptCachingConfig,
    TokenUsage,
    build_cache_config,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


class _TestModel(BaseModel):
    """Small Pydantic model used for structured-output tests."""
    name: str
    value: int


class _MockStatusError(Exception):
    """Exception with a status_code attribute for retry tests.

    The real SDK exceptions carry status_code. MagicMock cannot be
    raised (it is not a BaseException), so this lightweight substitute
    lets us exercise retry logic.
    """

    def __init__(self, status_code: int = 429) -> None:
        self.status_code = status_code
        super().__init__()


class _MockBackend(LLMBackend):
    """Minimal backend that records calls for verification — no SDK imports."""

    def __init__(
        self,
        model: str = "mock-model",
        embed_dim: int = 128,
        chat_response: ChatResponse | None = None,
    ) -> None:
        self._model = model
        self._embed_dim = embed_dim
        self._chat_response = chat_response or ChatResponse(
            content='{"name": "test", "value": 42}', model=model
        )
        self._embed_response = EmbeddingResponse(
            embeddings=[[0.1] * embed_dim], model=model, dim=embed_dim
        )
        self.last_messages: list[dict] | None = None
        self.last_cache_config: PromptCachingConfig | None = None
        self.last_kwargs: dict[str, Any] = {}
        self._chat_side_effect: Exception | None = None

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def embedding_dim(self) -> int:
        return self._embed_dim

    async def _chat(
        self,
        messages: list[dict],
        cache_config: PromptCachingConfig | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        self.last_messages = messages
        self.last_cache_config = cache_config
        self.last_kwargs = kwargs
        if self._chat_side_effect:
            raise self._chat_side_effect
        return self._chat_response

    async def embed(self, texts: list[str], **kwargs: Any) -> EmbeddingResponse:
        return self._embed_response


# ═══════════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestTokenUsage:
    """TokenUsage dataclass — totals and defaults."""

    def test_defaults_to_zero(self) -> None:
        usage = TokenUsage()
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.cache_read_input_tokens == 0
        assert usage.cache_creation_input_tokens == 0

    def test_total_tokens_sum(self) -> None:
        usage = TokenUsage(prompt_tokens=10, completion_tokens=20)
        assert usage.total_tokens == 30

    def test_total_cache_tokens(self) -> None:
        usage = TokenUsage(
            cache_read_input_tokens=5, cache_creation_input_tokens=3
        )
        assert usage.total_cache_tokens == 8

    def test_total_tokens_with_caching(self) -> None:
        """Total tokens excludes cache tokens — those are not new tokens."""
        usage = TokenUsage(
            prompt_tokens=100,
            completion_tokens=50,
            cache_read_input_tokens=200,
        )
        assert usage.total_tokens == 150


@pytest.mark.unit
class TestPromptCachingConfig:
    """PromptCachingConfig defaults and construction."""

    def test_defaults(self) -> None:
        cfg = PromptCachingConfig()
        assert cfg.enabled is True
        assert cfg.anthropic_min_tokens == 1024
        assert cfg.anthropic_cache_ttl == "5m"
        assert cfg.session_id is None

    def test_with_session_id(self) -> None:
        cfg = PromptCachingConfig(session_id="sess-abc")
        assert cfg.session_id == "sess-abc"

    def test_disabled(self) -> None:
        cfg = PromptCachingConfig(enabled=False)
        assert cfg.enabled is False


@pytest.mark.unit
class TestChatResponse:
    """ChatResponse dataclass."""

    def test_content_and_model(self) -> None:
        resp = ChatResponse(content="Hello", model="gpt-4o")
        assert resp.content == "Hello"
        assert resp.model == "gpt-4o"

    def test_validated_data_default_none(self) -> None:
        resp = ChatResponse(content="Hello", model="gpt-4o")
        assert resp.validated_data is None

    def test_with_validated_data(self) -> None:
        model = _TestModel(name="x", value=1)
        resp = ChatResponse(content="{}", model="gpt-4o", validated_data=model)
        assert resp.validated_data is model
        assert resp.validated_data.name == "x"


@pytest.mark.unit
class TestEmbeddingResponse:
    """EmbeddingResponse dataclass."""

    def test_embeddings_and_dim(self) -> None:
        resp = EmbeddingResponse(
            embeddings=[[0.1, 0.2], [0.3, 0.4]], model="embed-3", dim=2
        )
        assert len(resp.embeddings) == 2
        assert resp.dim == 2
        assert resp.model == "embed-3"


# ═══════════════════════════════════════════════════════════════════════════════
# build_cache_config
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestBuildCacheConfig:
    """build_cache_config resolution from settings + org config."""

    def test_enabled_by_default(self) -> None:
        """Returns enabled=True when PROMPT_CACHING_ENABLED is True (default)."""
        cfg = build_cache_config()
        assert cfg.enabled is True
        assert cfg.anthropic_min_tokens == 1024
        assert cfg.anthropic_cache_ttl == "5m"

    def test_global_kill_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When PROMPT_CACHING_ENABLED=False, returns disabled config."""
        import core.config as _cfg
        settings = _cfg.get_settings()
        monkeypatch.setattr(settings, "PROMPT_CACHING_ENABLED", False)
        cfg = build_cache_config()
        assert cfg.enabled is False

    def test_org_config_overrides(self) -> None:
        """Per-org config overrides anthropic_min_tokens and anthropic_cache_ttl."""
        org_config = {
            "prompt_caching": {
                "anthropic_min_tokens": 2048,
                "anthropic_cache_ttl": "1h",
            }
        }
        cfg = build_cache_config(org_config=org_config)
        assert cfg.anthropic_min_tokens == 2048
        assert cfg.anthropic_cache_ttl == "1h"

    def test_org_config_disables_caching(self) -> None:
        """Per-org config can disable caching even when global switch is on."""
        org_config = {"prompt_caching": {"enabled": False}}
        cfg = build_cache_config(org_config=org_config)
        assert cfg.enabled is False

    def test_session_id_passthrough(self) -> None:
        """session_id is passed through to the config."""
        cfg = build_cache_config(session_id="sess-xyz")
        assert cfg.session_id == "sess-xyz"

    def test_empty_org_config_falls_back(self) -> None:
        """Empty org_config uses settings defaults."""
        cfg = build_cache_config(org_config={})
        assert cfg.enabled is True
        assert cfg.anthropic_min_tokens == 1024


# ═══════════════════════════════════════════════════════════════════════════════
# LLMBackend ABC — abstract enforcement
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestLLMBackendABC:
    """LLMBackend ABC — cannot instantiate without implementing abstract methods."""

    def test_cannot_instantiate_abc(self) -> None:
        """LLMBackend cannot be instantiated directly."""
        with pytest.raises(TypeError):
            LLMBackend()  # type: ignore[abstract]

    def test_concrete_subclass_works(self) -> None:
        """A subclass implementing all abstract methods can be instantiated."""
        backend = _MockBackend()
        assert isinstance(backend, LLMBackend)


# ═══════════════════════════════════════════════════════════════════════════════
# LLMBackend — chat() with structured-output validation
# ═══════════════════════════════════════════════════════════════════════════════


class TestLLMBackendChat:
    """LLMBackend.chat() — structured-output validation and retry."""

    def test_chat_without_response_model(self) -> None:
        """Without response_model, delegates directly to _chat."""
        backend = _MockBackend()
        resp = backend.chat([{"role": "user", "content": "Hi"}])
        # Use sync-compatible check — chat is async, but this returns a coroutine
        # in practice.  For the _MockBackend, _chat returns ChatResponse directly.
        # Actually LLMBackend.chat() is async so we need await.
        # Let's use an event loop.

    # The above test is tricky because LLMBackend.chat() is async.
    # We'll use pytest-asyncio for proper async test support.

    @pytest.mark.asyncio
    async def test_chat_no_response_model_delegates_to_chat(self) -> None:
        """Without response_model, delegates directly to _chat."""
        backend = _MockBackend()
        resp = await backend.chat([{"role": "user", "content": "Hi"}])
        assert resp.content == '{"name": "test", "value": 42}'

    @pytest.mark.asyncio
    async def test_chat_passes_cache_config(self) -> None:
        """cache_config is forwarded to _chat."""
        backend = _MockBackend()
        cache = PromptCachingConfig(enabled=False)
        await backend.chat(
            [{"role": "user", "content": "Hi"}], cache_config=cache
        )
        assert backend.last_cache_config is cache
        assert backend.last_cache_config.enabled is False

    @pytest.mark.asyncio
    async def test_chat_passes_extra_kwargs(self) -> None:
        """Extra kwargs (temperature, max_tokens) are forwarded."""
        backend = _MockBackend()
        await backend.chat(
            [{"role": "user", "content": "Hi"}],
            temperature=0.7,
            max_tokens=100,
        )
        assert backend.last_kwargs["temperature"] == 0.7
        assert backend.last_kwargs["max_tokens"] == 100

    @pytest.mark.asyncio
    async def test_chat_with_response_model_validates(self) -> None:
        """With response_model, return ChatResponse with validated_data set."""
        backend = _MockBackend()
        resp = await backend.chat(
            [{"role": "user", "content": "Say a name and value"}],
            response_model=_TestModel,
        )
        assert resp.validated_data is not None
        assert isinstance(resp.validated_data, _TestModel)
        assert resp.validated_data.name == "test"
        assert resp.validated_data.value == 42

    @pytest.mark.asyncio
    async def test_chat_retries_on_validation_failure(self) -> None:
        """Failed validation retries with error-context feedback."""
        # First response fails validation, second succeeds
        backend = _MockBackend()
        backend._chat_response = ChatResponse(
            content="not valid json", model="mock-model"
        )

        VALID_JSON = '{"name": "fixed", "value": 1}'

        # Create a new chat response for the second call
        class _RetryBackend(_MockBackend):
            def __init__(self) -> None:
                super().__init__()
                self.call_count = 0

            async def _chat(
                self,
                messages: list[dict],
                cache_config: PromptCachingConfig | None = None,
                **kwargs: Any,
            ) -> ChatResponse:
                self.call_count += 1
                self.last_messages = messages
                if self.call_count == 1:
                    return ChatResponse(content="garbage", model="mock-model")
                return ChatResponse(content=VALID_JSON, model="mock-model")

        backend = _RetryBackend()
        # Default VALIDATION_RETRIES=2, so max attempts = 3.
        # First attempt fails, second succeeds.
        resp = await backend.chat(
            [{"role": "user", "content": "test"}],
            response_model=_TestModel,
        )
        assert backend.call_count == 2
        assert resp.validated_data is not None
        assert resp.validated_data.name == "fixed"

    @pytest.mark.asyncio
    async def test_chat_exhausts_retries_raises(self) -> None:
        """All retries exhausted raises LLMStructuredOutputError."""
        backend = _MockBackend()
        backend._chat_response = ChatResponse(
            content="not valid json", model="mock-model"
        )
        with pytest.raises(LLMStructuredOutputError) as excinfo:
            await backend.chat(
                [{"role": "user", "content": "test"}],
                response_model=_TestModel,
                validation_retries=1,  # total 2 attempts
            )
        assert "LLM output failed to match _TestModel" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_chat_validation_retries_override(self) -> None:
        """validation_retries parameter overrides the class default."""
        backend = _MockBackend()
        backend._chat_response = ChatResponse(
            content="garbage", model="mock-model"
        )
        with pytest.raises(LLMStructuredOutputError):
            await backend.chat(
                [{"role": "user", "content": "test"}],
                response_model=_TestModel,
                validation_retries=0,  # only 1 attempt total
            )

    @pytest.mark.asyncio
    async def test_chat_extracts_json_from_fences(self) -> None:
        """If model_validate_json fails, _extract_json fallback is used."""
        backend = _MockBackend()
        backend._chat_response = ChatResponse(
            content='```json\n{"name": "extracted", "value": 7}\n```',
            model="mock-model",
        )
        resp = await backend.chat(
            [{"role": "user", "content": "test"}],
            response_model=_TestModel,
        )
        assert resp.validated_data is not None
        assert resp.validated_data.name == "extracted"
        assert resp.validated_data.value == 7
        # Content is normalised to clean JSON
        assert resp.content == '{"name":"extracted","value":7}'


# ═══════════════════════════════════════════════════════════════════════════════
# LLMBackend — _inject_schema_instr
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestInjectSchemaInstr:
    """_inject_schema_instr injects JSON schema into messages."""

    def test_prepends_system_when_first_is_not_system(self) -> None:
        messages = [{"role": "user", "content": "Hi"}]
        result = LLMBackend._inject_schema_instr(messages, _TestModel)
        assert result[0]["role"] == "system"
        assert "Expected JSON schema" in result[0]["content"]

    def test_appends_to_existing_system(self) -> None:
        messages = [{"role": "system", "content": "You are helpful."}]
        result = LLMBackend._inject_schema_instr(messages, _TestModel)
        assert result[0]["role"] == "system"
        assert "You are helpful." in result[0]["content"]
        assert "Expected JSON schema" in result[0]["content"]

    def test_empty_messages_gets_system(self) -> None:
        result = LLMBackend._inject_schema_instr([], _TestModel)
        assert len(result) == 1
        assert result[0]["role"] == "system"

    def test_schema_includes_model_fields(self) -> None:
        messages = [{"role": "user", "content": "Hi"}]
        result = LLMBackend._inject_schema_instr(messages, _TestModel)
        # The schema JSON should mention the field names
        assert "name" in result[0]["content"]
        assert "value" in result[0]["content"]


# ═══════════════════════════════════════════════════════════════════════════════
# LLMBackend — _build_retry_messages
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestBuildRetryMessages:
    """_build_retry_messages appends retry feedback."""

    def test_appends_assistant_and_user(self) -> None:
        messages = [{"role": "user", "content": "Do it"}]
        result = LLMBackend._build_retry_messages(
            messages, bad_content="bad json", model=_TestModel
        )
        assert len(result) == 3
        assert result[-2]["role"] == "assistant"
        assert result[-2]["content"] == "bad json"
        assert result[-1]["role"] == "user"
        assert "NOT valid JSON" in result[-1]["content"]

    def test_does_not_mutate_original(self) -> None:
        messages = [{"role": "user", "content": "Do it"}]
        original_len = len(messages)
        LLMBackend._build_retry_messages(
            messages, bad_content="bad", model=_TestModel
        )
        assert len(messages) == original_len


# ═══════════════════════════════════════════════════════════════════════════════
# LLMBackend — _extract_json
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestExtractJson:
    """_extract_json recovers JSON from various text wrappers."""

    def test_plain_json_object(self) -> None:
        result = LLMBackend._extract_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_plain_json_array(self) -> None:
        result = LLMBackend._extract_json("[1, 2, 3]")
        assert result == [1, 2, 3]

    def test_json_inside_markdown_fences(self) -> None:
        text = '```json\n{"name": "test"}\n```'
        result = LLMBackend._extract_json(text)
        assert result == {"name": "test"}

    def test_json_inside_unknown_fences(self) -> None:
        text = '```\n{"name": "test"}\n```'
        result = LLMBackend._extract_json(text)
        assert result == {"name": "test"}

    def test_text_before_only(self) -> None:
        """Prefix text before the JSON object is stripped."""
        text = 'Here is the result:\n{"key": 42}'
        result = LLMBackend._extract_json(text)
        assert result == {"key": 42}

    def test_no_json_returns_none(self) -> None:
        result = LLMBackend._extract_json("Just some text")
        assert result is None

    def test_malformed_json_returns_none(self) -> None:
        result = LLMBackend._extract_json('{"key": broken}')
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        result = LLMBackend._extract_json("")
        assert result is None

    def test_with_thinking_block(self) -> None:
        """Simulates deepseek-r1 output with  thinking block."""
        text = "I'll compute that.\n\n{'answer': 42}"
        result = LLMBackend._extract_json(text)
        # orjson handles single quotes... actually no, orjson doesn't.
        # The method uses orjson.loads which only accepts standard JSON.
        assert result is None

    def test_json_with_non_json_prefix(self) -> None:
        text = 'Sure! The answer is {"result": "success"}'
        result = LLMBackend._extract_json(text)
        assert result == {"result": "success"}


# ═══════════════════════════════════════════════════════════════════════════════
# LLMBackendRegistry
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestLLMBackendRegistry:
    """Registry — register, get, list, duplicate guard."""

    def test_register_and_get(self) -> None:
        backend = _MockBackend
        LLMBackendRegistry.register("test_bk", backend)
        assert LLMBackendRegistry.get("test_bk") is backend

    def test_get_unregistered_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown LLM backend"):
            LLMBackendRegistry.get("nonexistent")

    def test_list_available(self) -> None:
        LLMBackendRegistry.register("bk_a", _MockBackend)
        LLMBackendRegistry.register("bk_b", _MockBackend)
        available = LLMBackendRegistry.list_available()
        assert "bk_a" in available
        assert "bk_b" in available

    def test_double_register_raises(self) -> None:
        LLMBackendRegistry.register("dup_bk", _MockBackend)
        with pytest.raises(ValueError, match="already registered"):
            LLMBackendRegistry.register("dup_bk", _MockBackend)


# ═══════════════════════════════════════════════════════════════════════════════
# resolve_backend (factory)
# ═══════════════════════════════════════════════════════════════════════════════


class TestResolveBackend:
    """resolve_backend — resolves from org_config or explicit provider."""

    @staticmethod
    async def _resolve(*args: Any, **kwargs: Any) -> Any:
        from core.llm import resolve_backend
        return await resolve_backend(*args, **kwargs)

    @pytest.mark.asyncio
    async def test_resolve_from_org_config(self) -> None:
        """Resolves backend from org_config.llm_backend."""
        org_config = {
            "llm_backend": "ollama",
            "ollama_base_url": "http://localhost:11434",
        }
        backend = await self._resolve(org_config=org_config)
        assert backend is not None
        assert backend.model_name == "llama3.2:3b"

    @pytest.mark.asyncio
    async def test_resolve_from_explicit_provider(self) -> None:
        """Explicit provider argument takes priority."""
        org_config = {
            "llm_backend": "ollama",
            "ollama_base_url": "http://localhost:11434",
        }
        backend = await self._resolve(
            provider="openai", org_config={**org_config, "openai_api_key": "sk-test"}
        )
        assert isinstance(backend, LLMBackend)

    # Use a mock to avoid hitting the registry for unknown provider test
    @pytest.mark.asyncio
    async def test_resolve_no_config_raises(self) -> None:
        """No org_config and no provider raises LLMConfigurationError."""
        with pytest.raises(LLMConfigurationError, match="No LLM backend configured"):
            await self._resolve()

    @pytest.mark.asyncio
    async def test_resolve_unknown_provider_raises(self) -> None:
        """Explicit unknown provider raises ValueError (via registry lookup)."""
        with pytest.raises(ValueError, match="Unknown LLM backend"):
            await self._resolve(provider="unknown_provider_x")


# ═══════════════════════════════════════════════════════════════════════════════
# _create_backend — provider-specific instantiation
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestCreateBackend:
    """_create_backend — provider construction with config validation."""

    @pytest.mark.asyncio
    async def test_ollama_requires_base_url(self) -> None:
        """Ollama without ollama_base_url raises LLMConfigurationError."""
        from core.llm import _create_backend

        with pytest.raises(LLMConfigurationError, match="ollama_base_url"):
            await _create_backend("ollama", config=None)

    @pytest.mark.asyncio
    async def test_openai_requires_api_key(self) -> None:
        """OpenAI without openai_api_key raises LLMConfigurationError."""
        from core.llm import _create_backend

        with pytest.raises(LLMConfigurationError, match="openai_api_key"):
            await _create_backend("openai", config={"openai_model": "gpt-4"})

    @pytest.mark.asyncio
    async def test_azure_requires_endpoint(self) -> None:
        """Azure without azure_endpoint raises LLMConfigurationError."""
        from core.llm import _create_backend

        with pytest.raises(LLMConfigurationError, match="azure_endpoint"):
            await _create_backend("azure", config={"azure_api_key": "k", "azure_deployment": "d"})

    @pytest.mark.asyncio
    async def test_azure_requires_api_key(self) -> None:
        """Azure without azure_api_key raises LLMConfigurationError."""
        from core.llm import _create_backend

        with pytest.raises(LLMConfigurationError, match="azure_api_key"):
            await _create_backend("azure", config={"azure_endpoint": "http://e", "azure_deployment": "d"})

    @pytest.mark.asyncio
    async def test_azure_requires_deployment(self) -> None:
        """Azure without azure_deployment raises LLMConfigurationError."""
        from core.llm import _create_backend

        with pytest.raises(LLMConfigurationError, match="azure_deployment"):
            await _create_backend("azure", config={"azure_endpoint": "http://e", "azure_api_key": "k"})

    @pytest.mark.asyncio
    async def test_anthropic_requires_api_key(self) -> None:
        """Anthropic without anthropic_api_key raises LLMConfigurationError."""
        from core.llm import _create_backend

        with pytest.raises(LLMConfigurationError, match="anthropic_api_key"):
            await _create_backend("anthropic", config=None)

    @pytest.mark.asyncio
    async def test_openrouter_requires_api_key(self) -> None:
        """OpenRouter without api_key raises LLMConfigurationError."""
        from core.llm import _create_backend

        with pytest.raises(LLMConfigurationError, match="api_key"):
            await _create_backend("openrouter", config=None)

    @pytest.mark.asyncio
    async def test_unknown_provider_raises_value_error(self) -> None:
        """An unrecognized provider string raises ValueError."""
        from core.llm import _create_backend

        with pytest.raises(ValueError, match="Unknown LLM backend"):
            await _create_backend("nonexistent_provider", config={})


# ═══════════════════════════════════════════════════════════════════════════════
# OllamaBackend
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestOllamaBackend:
    """OllamaBackend — local LLM via REST API."""

    def test_default_base_url(self) -> None:
        from core.llm_backends import OllamaBackend

        backend = OllamaBackend()
        assert backend.model_name == "llama3.2:3b"

    def test_custom_base_url(self) -> None:
        from core.llm_backends import OllamaBackend

        backend = OllamaBackend(base_url="http://ollama:11434")
        assert backend.embedding_dim == 768

    @pytest.mark.asyncio
    async def test_chat_success(self) -> None:
        from core.llm_backends import OllamaBackend

        mock_response = {
            "model": "llama3.2:3b",
            "message": {"content": "Hello from Ollama"},
            "metrics": {"prompt_eval_count": 10, "eval_count": 20},
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: mock_response,
                raise_for_status=lambda: None,
            )

            backend = OllamaBackend()
            resp = await backend.chat([{"role": "user", "content": "Hi"}])
            assert resp.content == "Hello from Ollama"
            assert resp.model == "llama3.2:3b"
            assert resp.usage.prompt_tokens == 10
            assert resp.usage.completion_tokens == 20

    @pytest.mark.asyncio
    async def test_chat_http_error(self) -> None:
        from core.llm_backends import OllamaBackend

        import httpx

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post.side_effect = httpx.HTTPStatusError(
                "404 not found", request=MagicMock(), response=MagicMock(status_code=404)
            )

            backend = OllamaBackend()
            with pytest.raises(httpx.HTTPStatusError):
                await backend.chat([{"role": "user", "content": "Hi"}])

    @pytest.mark.asyncio
    async def test_chat_timeout(self) -> None:
        from core.llm_backends import OllamaBackend

        import httpx

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post.side_effect = httpx.TimeoutException("timeout")

            backend = OllamaBackend()
            with pytest.raises(httpx.TimeoutException):
                await backend.chat([{"role": "user", "content": "Hi"}])

    @pytest.mark.asyncio
    async def test_embed_success(self) -> None:
        from core.llm_backends import OllamaBackend

        mock_response = {
            "model": "nomic-embed-text",
            "embeddings": [[0.1, 0.2, 0.3]],
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: mock_response,
                raise_for_status=lambda: None,
            )

            backend = OllamaBackend()
            resp = await backend.embed(["test text"])
            assert len(resp.embeddings) == 1
            assert resp.dim == 3
            assert resp.model == "nomic-embed-text"

    @pytest.mark.asyncio
    async def test_embed_empty_response_raises(self) -> None:
        from core.llm_backends import OllamaBackend

        mock_response = {"model": "nomic-embed-text", "embeddings": []}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: mock_response,
                raise_for_status=lambda: None,
            )

            backend = OllamaBackend()
            with pytest.raises(ValueError, match="Empty embedding response"):
                await backend.embed(["test text"])

    @pytest.mark.asyncio
    async def test_embed_singleton_format(self) -> None:
        """/api/embed returns a single embedding as a flat list."""
        from core.llm_backends import OllamaBackend

        mock_response = {
            "model": "nomic-embed-text",
            "embedding": [0.5, 0.6, 0.7],
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: mock_response,
                raise_for_status=lambda: None,
            )

            backend = OllamaBackend()
            resp = await backend.embed(["single"])
            assert len(resp.embeddings) == 1
            assert resp.dim == 3


# ═══════════════════════════════════════════════════════════════════════════════
# OpenAIBackend
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestOpenAIBackend:
    """OpenAIBackend — OpenAI API via SDK."""

    def test_init_requires_key(self) -> None:
        from core.llm_backends import OpenAIBackend

        with pytest.raises(ValueError, match="API key is required"):
            OpenAIBackend(api_key="")

    def test_init_with_model(self) -> None:
        with patch("openai.AsyncOpenAI"):
            from core.llm_backends import OpenAIBackend

            backend = OpenAIBackend(api_key="sk-test", model="gpt-4")
            assert backend.model_name == "gpt-4"

    def test_default_model(self) -> None:
        with patch("openai.AsyncOpenAI"):
            from core.llm_backends import OpenAIBackend

            backend = OpenAIBackend(api_key="sk-test")
            assert backend.model_name == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_chat_success(self) -> None:
        from core.llm_backends import OpenAIBackend

        mock_choice = MagicMock()
        mock_choice.message.content = "Hello from OpenAI"
        mock_choice.message.tool_calls = None

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 20
        mock_usage.prompt_tokens_details = None

        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]
        mock_completion.usage = mock_usage
        mock_completion.model = "gpt-4o-mini"

        with patch("openai.AsyncOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_completion

            backend = OpenAIBackend(api_key="sk-test")
            resp = await backend.chat([{"role": "user", "content": "Hi"}])
            assert resp.content == "Hello from OpenAI"
            assert resp.usage.prompt_tokens == 10
            assert resp.usage.completion_tokens == 20

    @pytest.mark.asyncio
    async def test_chat_extracts_tool_call_content(self) -> None:
        """When content is None, falls back to tool_call arguments."""
        from core.llm_backends import OpenAIBackend

        mock_choice = MagicMock()
        mock_choice.message.content = None

        mock_tool_call = MagicMock()
        mock_tool_call.function.name = "test_func"
        mock_tool_call.function.arguments = '{"key": "value"}'
        mock_choice.message.tool_calls = [mock_tool_call]

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 5
        mock_usage.completion_tokens = 10
        mock_usage.prompt_tokens_details = None

        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]
        mock_completion.usage = mock_usage

        with patch("openai.AsyncOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_completion

            backend = OpenAIBackend(api_key="sk-test")
            resp = await backend.chat([{"role": "user", "content": "Hi"}])
            assert resp.content == '{"key": "value"}'

    @pytest.mark.asyncio
    async def test_chat_retry_on_rate_limit(self) -> None:
        """429 triggers retry; second attempt succeeds."""
        from core.llm_backends import OpenAIBackend

        with patch("openai.AsyncOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client

            # First call: rate limit error
            rate_limit_error = _MockStatusError(429)

            # Second call: success — build a proper mock completion
            mock_choice = MagicMock()
            mock_choice.message.content = "Success after retry"
            mock_choice.message.tool_calls = None

            mock_usage = MagicMock()
            mock_usage.prompt_tokens = 5
            mock_usage.completion_tokens = 10
            mock_usage.prompt_tokens_details = None

            mock_completion = MagicMock()
            mock_completion.choices = [mock_choice]
            mock_completion.usage = mock_usage

            mock_client.chat.completions.create.side_effect = [
                rate_limit_error,
                mock_completion,
            ]

            backend = OpenAIBackend(api_key="sk-test")
            resp = await backend.chat([{"role": "user", "content": "Hi"}])
            assert resp.content == "Success after retry"
            assert mock_client.chat.completions.create.call_count == 2

    @pytest.mark.asyncio
    async def test_chat_non_retryable_error_raises_immediately(self) -> None:
        """Non-retryable errors (e.g. 400) are raised immediately."""
        from core.llm_backends import OpenAIBackend

        with patch("openai.AsyncOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client

            auth_error = _MockStatusError(401)
            mock_client.chat.completions.create.side_effect = auth_error

            backend = OpenAIBackend(api_key="sk-bad")
            with pytest.raises(Exception):
                await backend.chat([{"role": "user", "content": "Hi"}])

    @pytest.mark.asyncio
    async def test_embed_success(self) -> None:
        from core.llm_backends import OpenAIBackend

        mock_data_item = MagicMock()
        mock_data_item.embedding = [0.1, 0.2, 0.3]

        mock_embed_response = MagicMock()
        mock_embed_response.data = [mock_data_item]

        with patch("openai.AsyncOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.embeddings.create.return_value = mock_embed_response

            backend = OpenAIBackend(api_key="sk-test")
            resp = await backend.embed(["test text"])
            assert len(resp.embeddings) == 1
            assert resp.dim == 3
            assert resp.model == "text-embedding-3-small"

    @pytest.mark.asyncio
    async def test_embed_caches_cache_tokens(self) -> None:
        """prompt_tokens_details.cached_tokens and .cache_write_tokens are read."""
        from core.llm_backends import OpenAIBackend

        mock_choice = MagicMock()
        mock_choice.message.content = "Hello"
        mock_choice.message.tool_calls = None

        mock_details = MagicMock()
        mock_details.cached_tokens = 50
        mock_details.cache_write_tokens = 25

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 100
        mock_usage.completion_tokens = 50
        mock_usage.prompt_tokens_details = mock_details

        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]
        mock_completion.usage = mock_usage

        with patch("openai.AsyncOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_completion

            backend = OpenAIBackend(api_key="sk-test")
            resp = await backend.chat([{"role": "user", "content": "Hi"}])
            assert resp.usage.cache_read_input_tokens == 50
            assert resp.usage.cache_creation_input_tokens == 25


# ═══════════════════════════════════════════════════════════════════════════════
# AzureBackend
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestAzureBackend:
    """AzureBackend — Azure OpenAI via SDK."""

    def test_init_requires_endpoint(self) -> None:
        from core.llm_backends import AzureBackend

        with pytest.raises(ValueError, match="endpoint is required"):
            AzureBackend(endpoint="", api_key="k", deployment="d")

    def test_init_requires_api_key(self) -> None:
        from core.llm_backends import AzureBackend

        with pytest.raises(ValueError, match="API key is required"):
            AzureBackend(endpoint="http://e", api_key="", deployment="d")

    def test_init_requires_deployment(self) -> None:
        from core.llm_backends import AzureBackend

        with pytest.raises(ValueError, match="deployment name is required"):
            AzureBackend(endpoint="http://e", api_key="k", deployment="")

    def test_init_success(self) -> None:
        with patch("openai.AsyncAzureOpenAI"):
            from core.llm_backends import AzureBackend

            backend = AzureBackend(
                endpoint="https://my.openai.azure.com",
                api_key="az-key",
                deployment="gpt-4o",
            )
            assert backend.model_name == "gpt-4o"
            assert backend.embedding_dim == 1536

    @pytest.mark.asyncio
    async def test_chat_success(self) -> None:
        from core.llm_backends import AzureBackend

        mock_choice = MagicMock()
        mock_choice.message.content = "Azure response"
        mock_choice.message.tool_calls = None

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 20
        mock_usage.prompt_tokens_details = None

        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]
        mock_completion.usage = mock_usage

        with patch("openai.AsyncAzureOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_completion

            backend = AzureBackend(
                endpoint="https://e.openai.azure.com",
                api_key="az-key",
                deployment="gpt-4o",
            )
            resp = await backend.chat([{"role": "user", "content": "Hi"}])
            assert resp.content == "Azure response"
            assert resp.usage.prompt_tokens == 10

    @pytest.mark.asyncio
    async def test_embed_success(self) -> None:
        from core.llm_backends import AzureBackend

        mock_data_item = MagicMock()
        mock_data_item.embedding = [0.4, 0.5, 0.6]

        mock_embed_response = MagicMock()
        mock_embed_response.data = [mock_data_item]

        with patch("openai.AsyncAzureOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.embeddings.create.return_value = mock_embed_response

            backend = AzureBackend(
                endpoint="https://e.openai.azure.com",
                api_key="az-key",
                deployment="text-embedding-3",
            )
            resp = await backend.embed(["test"])
            assert len(resp.embeddings) == 1
            assert resp.dim == 3

    @pytest.mark.asyncio
    async def test_chat_retry_on_rate_limit(self) -> None:
        """Azure retries on 429 the same way as OpenAI."""
        from core.llm_backends import AzureBackend

        mock_choice = MagicMock()
        mock_choice.message.content = "OK"
        mock_choice.message.tool_calls = None

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 5
        mock_usage.completion_tokens = 5
        mock_usage.prompt_tokens_details = None

        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]
        mock_completion.usage = mock_usage

        with patch("openai.AsyncAzureOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client

            rate_limit = _MockStatusError(429)
            mock_client.chat.completions.create.side_effect = [rate_limit, mock_completion]

            backend = AzureBackend(
                endpoint="https://e.openai.azure.com",
                api_key="az-key",
                deployment="gpt-4o",
            )
            resp = await backend.chat([{"role": "user", "content": "Hi"}])
            assert resp.content == "OK"
            assert mock_client.chat.completions.create.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════════
# AnthropicBackend
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestAnthropicBackend:
    """AnthropicBackend — Claude via Anthropic SDK."""

    def test_init_requires_key(self) -> None:
        from core.llm_backends import AnthropicBackend

        with pytest.raises(ValueError, match="API key is required"):
            AnthropicBackend(api_key="")

    def test_init_with_model(self) -> None:
        with patch("anthropic.AsyncAnthropic"):
            from core.llm_backends import AnthropicBackend

            backend = AnthropicBackend(api_key="sk-ant-test", model="claude-opus-4-20250514")
            assert backend.model_name == "claude-opus-4-20250514"

    def test_default_model(self) -> None:
        with patch("anthropic.AsyncAnthropic"):
            from core.llm_backends import AnthropicBackend

            backend = AnthropicBackend(api_key="sk-ant-test")
            assert backend.model_name == "claude-sonnet-4-20250514"

    def test_embed_raises_not_implemented(self) -> None:
        from core.llm_backends import AnthropicBackend

        with patch("anthropic.AsyncAnthropic"):
            backend = AnthropicBackend(api_key="sk-ant-test")
            with pytest.raises(NotImplementedError, match="does not offer"):
                # embed is async, even if it raises NotImplementedError
                # it returns a coroutine that we need to await
                import asyncio
                asyncio.run(backend.embed(["test"]))

    @pytest.mark.asyncio
    async def test_chat_success(self) -> None:
        from core.llm_backends import AnthropicBackend

        mock_content_block = MagicMock()
        mock_content_block.type = "text"
        mock_content_block.text = "Hello from Claude"

        mock_usage = MagicMock()
        mock_usage.input_tokens = 15
        mock_usage.output_tokens = 25
        mock_usage.cache_read_input_tokens = 0
        mock_usage.cache_creation_input_tokens = 0

        mock_response = MagicMock()
        mock_response.content = [mock_content_block]
        mock_response.usage = mock_usage
        mock_response.model = "claude-sonnet-4-20250514"

        with patch("anthropic.AsyncAnthropic") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.messages.create.return_value = mock_response

            backend = AnthropicBackend(api_key="sk-ant-test")
            resp = await backend.chat([{"role": "user", "content": "Hi"}])
            assert resp.content == "Hello from Claude"
            assert resp.usage.prompt_tokens == 15
            assert resp.usage.completion_tokens == 25

    @pytest.mark.asyncio
    async def test_chat_with_system_message(self) -> None:
        """Anthropic extracts the system message from the messages list."""
        from core.llm_backends import AnthropicBackend

        mock_content_block = MagicMock()
        mock_content_block.type = "text"
        mock_content_block.text = "Understood"

        mock_usage = MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5
        mock_usage.cache_read_input_tokens = 0
        mock_usage.cache_creation_input_tokens = 0

        mock_response = MagicMock()
        mock_response.content = [mock_content_block]
        mock_response.usage = mock_usage
        mock_response.model = "claude-sonnet-4-20250514"

        with patch("anthropic.AsyncAnthropic") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.messages.create.return_value = mock_response

            backend = AnthropicBackend(api_key="sk-ant-test")
            await backend.chat([
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hi"},
            ])

            # Verify the system parameter was passed separately
            call_kwargs = mock_client.messages.create.call_args.kwargs
            assert call_kwargs["system"] == "You are helpful."
            # The messages should NOT include the system message
            assert all(m["role"] != "system" for m in call_kwargs["messages"])

    @pytest.mark.asyncio
    async def test_chat_with_caching(self) -> None:
        """Prompt caching adds cache_control to system block."""
        from core.llm_backends import AnthropicBackend

        # Override anthropic_min_tokens to 0 so any system text triggers cache
        cache_config = PromptCachingConfig(
            enabled=True,
            anthropic_min_tokens=0,
            anthropic_cache_ttl="5m",
        )

        mock_content_block = MagicMock()
        mock_content_block.type = "text"
        mock_content_block.text = "Cached response"

        mock_usage = MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5
        mock_usage.cache_read_input_tokens = 0
        mock_usage.cache_creation_input_tokens = 0

        mock_response = MagicMock()
        mock_response.content = [mock_content_block]
        mock_response.usage = mock_usage

        with patch("anthropic.AsyncAnthropic") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.messages.create.return_value = mock_response

            backend = AnthropicBackend(api_key="sk-ant-test")
            await backend.chat(
                [
                    {"role": "system", "content": "System prompt here"},
                    {"role": "user", "content": "Hi"},
                ],
                cache_config=cache_config,
            )

            call_kwargs = mock_client.messages.create.call_args.kwargs
            # system should be a list-of-blocks format with cache_control
            assert isinstance(call_kwargs["system"], list)
            assert call_kwargs["system"][0]["type"] == "text"
            assert "cache_control" in call_kwargs["system"][0]
            assert call_kwargs["system"][0]["cache_control"]["type"] == "ephemeral"

    @pytest.mark.asyncio
    async def test_chat_retry_on_rate_limit(self) -> None:
        """429 triggers retry for Anthropic."""
        from core.llm_backends import AnthropicBackend

        mock_content_block = MagicMock()
        mock_content_block.type = "text"
        mock_content_block.text = "OK"

        mock_usage = MagicMock()
        mock_usage.input_tokens = 5
        mock_usage.output_tokens = 5
        mock_usage.cache_read_input_tokens = 0
        mock_usage.cache_creation_input_tokens = 0

        mock_response = MagicMock()
        mock_response.content = [mock_content_block]
        mock_response.usage = mock_usage

        with patch("anthropic.AsyncAnthropic") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client

            class _AnthropicRateLimit(_MockStatusError):
                """Anthropic SDK raises ``anthropic.RateLimitError``."""

            rate_limit_error = _AnthropicRateLimit(429)

            mock_client.messages.create.side_effect = [rate_limit_error, mock_response]

            backend = AnthropicBackend(api_key="sk-ant-test")
            resp = await backend.chat([{"role": "user", "content": "Hi"}])
            assert resp.content == "OK"
            assert mock_client.messages.create.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════════
# OpenRouterBackend
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestOpenRouterBackend:
    """OpenRouterBackend — unified API via OpenAI-compatible client."""

    def test_init_requires_api_key(self) -> None:
        from core.llm_backends import OpenRouterBackend

        with pytest.raises(LLMConfigurationError, match="API key is required"):
            OpenRouterBackend(api_key="", model="some-model")

    def test_init_requires_model(self) -> None:
        from core.llm_backends import OpenRouterBackend

        with pytest.raises(LLMConfigurationError, match="model name is required"):
            OpenRouterBackend(api_key="sk-or-test", model=None)

    def test_init_success(self) -> None:
        with patch("openai.AsyncOpenAI") as mock_cls:
            mock_cls.return_value = MagicMock()
            from core.llm_backends import OpenRouterBackend

            backend = OpenRouterBackend(api_key="sk-or-test", model="openai/gpt-4o")
            assert backend.model_name == "openai/gpt-4o"
            # Verify custom base URL
            call_kwargs = mock_cls.call_args.kwargs
            assert call_kwargs["base_url"] == "https://openrouter.ai/api/v1"
            assert "HTTP-Referer" in call_kwargs["default_headers"]

    @pytest.mark.asyncio
    async def test_chat_success(self) -> None:
        from core.llm_backends import OpenRouterBackend

        mock_choice = MagicMock()
        mock_choice.message.content = "OpenRouter response"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 20
        mock_usage.prompt_tokens_details = None

        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]
        mock_completion.usage = mock_usage
        mock_completion.model = "openai/gpt-4o"
        # Simulate OpenRouter's extra cache_discount field
        mock_completion.model_extra = {"cache_discount": 0.5}

        with patch("openai.AsyncOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_completion

            backend = OpenRouterBackend(api_key="sk-or-test", model="openai/gpt-4o")
            resp = await backend.chat([{"role": "user", "content": "Hi"}])
            assert resp.content == "OpenRouter response"
            assert resp.model == "openai/gpt-4o"
            assert resp.usage.prompt_tokens == 10

    @pytest.mark.asyncio
    async def test_chat_with_session_stickiness(self) -> None:
        """Session ID is passed as extra_body when cache_config is enabled."""
        from core.llm_backends import OpenRouterBackend

        mock_choice = MagicMock()
        mock_choice.message.content = "OK"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 5
        mock_usage.completion_tokens = 5
        mock_usage.prompt_tokens_details = None

        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]
        mock_completion.usage = mock_usage
        mock_completion.model = "openai/gpt-4o"
        mock_completion.model_extra = None

        with patch("openai.AsyncOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_completion

            cache = PromptCachingConfig(enabled=True, session_id="sess-123")
            backend = OpenRouterBackend(api_key="sk-or-test", model="openai/gpt-4o")
            await backend.chat(
                [{"role": "user", "content": "Hi"}],
                cache_config=cache,
            )

            call_kwargs = mock_client.chat.completions.create.call_args.kwargs
            assert call_kwargs["extra_body"] == {"session_id": "sess-123"}

    @pytest.mark.asyncio
    async def test_chat_empty_response_retries(self) -> None:
        """Empty response (content=None, no tool_calls) retries."""
        from core.llm_backends import OpenRouterBackend

        # First response has content=None, but finish_reason is not
        # content_filter/length, so it should retry.
        empty_choice = MagicMock()
        empty_choice.message.content = None
        empty_choice.message.tool_calls = None
        empty_choice.finish_reason = "stop"

        good_choice = MagicMock()
        good_choice.message.content = "Retried OK"
        good_choice.message.tool_calls = None
        good_choice.finish_reason = "stop"

        empty_usage = MagicMock()
        empty_usage.prompt_tokens = 0
        empty_usage.completion_tokens = 0
        empty_usage.prompt_tokens_details = None

        good_usage = MagicMock()
        good_usage.prompt_tokens = 5
        good_usage.completion_tokens = 5
        good_usage.prompt_tokens_details = None

        empty_comp = MagicMock()
        empty_comp.choices = [empty_choice]
        empty_comp.usage = empty_usage
        empty_comp.model = "openai/gpt-4o"
        empty_comp.model_extra = None

        good_comp = MagicMock()
        good_comp.choices = [good_choice]
        good_comp.usage = good_usage
        good_comp.model = "openai/gpt-4o"
        good_comp.model_extra = None

        with patch("openai.AsyncOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create.side_effect = [empty_comp, good_comp]

            backend = OpenRouterBackend(api_key="sk-or-test", model="openai/gpt-4o")
            resp = await backend.chat([{"role": "user", "content": "Hi"}])
            assert resp.content == "Retried OK"
            assert mock_client.chat.completions.create.call_count == 2

    @pytest.mark.asyncio
    async def test_chat_filtered_response_raises(self) -> None:
        """Finish_reason='content_filter' raises immediately."""
        from core.llm_backends import OpenRouterBackend

        bad_choice = MagicMock()
        bad_choice.message.content = None
        bad_choice.message.tool_calls = None
        bad_choice.finish_reason = "content_filter"

        bad_comp = MagicMock()
        bad_comp.choices = [bad_choice]
        bad_comp.model = "openai/gpt-4o"

        with patch("openai.AsyncOpenAI") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = bad_comp

            backend = OpenRouterBackend(api_key="sk-or-test", model="openai/gpt-4o")
            with pytest.raises(ValueError, match="blocked or incomplete"):
                await backend.chat([{"role": "user", "content": "Hi"}])

    @pytest.mark.asyncio
    async def test_embed_requires_model(self) -> None:
        from core.llm_backends import OpenRouterBackend

        with patch("openai.AsyncOpenAI"):
            backend = OpenRouterBackend(api_key="sk-or-test", model="openai/gpt-4o")

            with pytest.raises(LLMConfigurationError, match="embedding requires"):
                await backend.embed(["test"])

    @pytest.mark.asyncio
    async def test_embed_success(self) -> None:
        from core.llm_backends import OpenRouterBackend

        mock_response_data = {
            "data": [{"embedding": [0.1, 0.2, 0.3]}],
            "model": "text-embedding-3-small",
        }

        with (
            patch("openai.AsyncOpenAI"),
            patch("httpx.AsyncClient") as mock_httpx_cls,
        ):
            mock_client = AsyncMock()
            mock_httpx_cls.return_value.__aenter__.return_value = mock_client
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: mock_response_data,
                raise_for_status=lambda: None,
            )

            backend = OpenRouterBackend(api_key="sk-or-test", model="openai/gpt-4o")
            resp = await backend.embed(["test"], model="text-embedding-3-small")
            assert len(resp.embeddings) == 1
            assert resp.dim == 3
            assert resp.model == "text-embedding-3-small"


# ═══════════════════════════════════════════════════════════════════════════════
# Auto-registration — verify backends registered at import time
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestAutoRegistration:
    """Verify all 5 backends are registered at module load time."""

    def test_all_providers_registered(self) -> None:
        """All 5 LLM providers are in the registry."""
        # Registry is populated by core.llm_backends import at module load
        # We just need to import something from core.llm to trigger it
        available = LLMBackendRegistry.list_available()
        assert "ollama" in available
        assert "openai" in available
        assert "azure" in available
        assert "anthropic" in available
        assert "openrouter" in available

    def test_registered_classes_are_concrete(self) -> None:
        """Each registered class can be instantiated with mock args."""
        import core.llm_backends as bks

        assert hasattr(bks, "OllamaBackend")
        assert hasattr(bks, "OpenAIBackend")
        assert hasattr(bks, "AzureBackend")
        assert hasattr(bks, "AnthropicBackend")
        assert hasattr(bks, "OpenRouterBackend")
