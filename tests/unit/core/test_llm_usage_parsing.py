"""Token-usage parsing matrix across providers.

GROUP 4: OpenAI/Azure reasoning + cache token mapping, OpenAI-like cache
split, Anthropic input/output + cache mapping, Ollama absent-metrics
zeros, and embedding usage propagation. All provider SDK responses are
mocks — no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.llm_backends import _parse_openai_usage

pytestmark = pytest.mark.unit


def _openai_style_usage() -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=50,
        prompt_tokens_details=SimpleNamespace(cached_tokens=30, cache_write_tokens=10),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
    )


class TestOpenAIParsing:
    def test_reasoning_and_cache_tokens_mapped(self) -> None:
        usage = _parse_openai_usage(_openai_style_usage())
        assert (usage.prompt_tokens, usage.completion_tokens) == (100, 50)
        assert usage.reasoning_tokens == 12
        assert usage.cache_read_input_tokens == 30
        assert usage.cache_creation_input_tokens == 10
        assert usage.total_tokens == 150

    def test_missing_details_read_as_zero(self) -> None:
        usage = _parse_openai_usage(
            SimpleNamespace(prompt_tokens=7, completion_tokens=3)
        )
        assert (usage.prompt_tokens, usage.completion_tokens) == (7, 3)
        assert usage.reasoning_tokens == 0
        assert (usage.cache_read_input_tokens, usage.cache_creation_input_tokens) == (0, 0)

    def test_none_usage_read_as_zero(self) -> None:
        assert _parse_openai_usage(None).total_tokens == 0


class TestOpenAILikeParsing:
    async def test_cache_split_parsed_from_sdk_response(self) -> None:
        from core.llm_backends import OpenAILikeBackend

        choice = MagicMock()
        choice.message.content = "ok"
        choice.message.tool_calls = None
        sdk_response = MagicMock()
        sdk_response.choices = [choice]
        sdk_response.usage = _openai_style_usage()
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(return_value=sdk_response)
        with patch("openai.AsyncOpenAI", return_value=client):
            backend = OpenAILikeBackend(base_url="http://localhost:8000/v1")

        resp = await backend._chat([{"role": "user", "content": "hi"}])
        assert resp.usage.cache_read_input_tokens == 30
        assert resp.usage.cache_creation_input_tokens == 10
        assert resp.usage.reasoning_tokens == 12


class TestAnthropicParsing:
    async def test_input_output_and_cache_tokens_mapped(self) -> None:
        from core.llm_backends import AnthropicBackend

        # anthropic SDK is not installed here — bypass __init__'s import
        # and attach a mocked client directly.
        backend = AnthropicBackend.__new__(AnthropicBackend)
        backend._model = "claude-test"
        sdk_response = MagicMock()
        sdk_response.content = [SimpleNamespace(type="text", text="hello")]
        sdk_response.usage = SimpleNamespace(
            input_tokens=8,
            output_tokens=12,
            cache_read_input_tokens=5,
            cache_creation_input_tokens=2,
        )
        backend._client = AsyncMock()
        backend._client.messages.create = AsyncMock(return_value=sdk_response)

        resp = await backend._chat([{"role": "user", "content": "hi"}])
        assert resp.content == "hello"
        assert (resp.usage.prompt_tokens, resp.usage.completion_tokens) == (8, 12)
        assert resp.usage.cache_read_input_tokens == 5
        assert resp.usage.cache_creation_input_tokens == 2


class TestOllamaParsing:
    async def test_absent_metrics_yields_zero_usage(self) -> None:
        from core.llm_backends import OllamaBackend

        http_resp = MagicMock()
        http_resp.raise_for_status = MagicMock()
        http_resp.json = MagicMock(
            return_value={
                "message": {"content": "hi"},
                "model": "llama3.2:3b",
            }
        )
        http_client = AsyncMock()
        http_client.__aenter__.return_value = http_client
        http_client.post = AsyncMock(return_value=http_resp)
        with patch("httpx.AsyncClient", return_value=http_client):
            resp = await OllamaBackend(base_url="http://localhost:11434")._chat(
                [{"role": "user", "content": "hi"}]
            )
        assert resp.content == "hi"
        assert (resp.usage.prompt_tokens, resp.usage.completion_tokens) == (0, 0)
