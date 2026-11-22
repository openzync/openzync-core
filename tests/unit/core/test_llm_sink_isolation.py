"""Never-raise metering isolation + final-attempt-only retry emission.

GROUP 1: a raising sink never breaks the caller of ``LLMBackend.chat`` /
``LLMBackend.embed``; ``asyncio.CancelledError`` still propagates;
``metered=False`` (connection-ping path) emits nothing.

GROUP 2 (retry half): when the provider-level ``_chat`` retries (429 then
success), only the final attempt is metered — a single emission carrying
the final usage.

All tests use a stub backend or a mocked OpenAI SDK client: no network,
no DB, no sleeps.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.llm import (
    ChatResponse,
    LLMBackend,
    TokenUsage,
    UsageRecord,
)

pytestmark = pytest.mark.unit


class _StubBackend(LLMBackend):
    """Minimal backend with fixed responses for metering-path tests."""

    @property
    def provider_name(self) -> str:
        return "stub"

    @property
    def model_name(self) -> str:
        return "stub-model"

    async def _chat(
        self,
        messages: list[dict],
        cache_config: Any | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        return ChatResponse(
            content="hi",
            model=self.model_name,
            usage=TokenUsage(prompt_tokens=5, completion_tokens=7),
        )


async def _failing_sink(record: UsageRecord) -> None:
    raise RuntimeError("sink boom")


async def _cancelled_sink(record: UsageRecord) -> None:
    raise asyncio.CancelledError()


class TestSinkIsolation:
    async def test_chat_sink_failure_swallowed(self) -> None:
        resp = await _StubBackend().chat(
            [{"role": "user", "content": "hi"}],
            metered=True,
            sink=_failing_sink,
        )
        assert isinstance(resp, ChatResponse)
        assert resp.content == "hi"

    async def test_chat_cancelled_error_propagates(self) -> None:
        with pytest.raises(asyncio.CancelledError):
            await _StubBackend().chat(
                [{"role": "user", "content": "hi"}],
                metered=True,
                sink=_cancelled_sink,
            )

    async def test_metered_false_emits_nothing(self) -> None:
        seen: list[UsageRecord] = []

        async def _recording_sink(record: UsageRecord) -> None:
            seen.append(record)

        backend = _StubBackend()
        await backend.chat(
            [{"role": "user", "content": "hi"}],
            metered=False,
            sink=_recording_sink,
        )
        assert seen == []


def _rate_limit_error() -> Exception:
    err = RuntimeError("rate limited")
    err.status_code = 429  # type: ignore[attr-defined]
    return err


class TestRetryMetering:
    async def test_429_then_success_emits_once_with_final_usage(self) -> None:
        from core.llm_backends import OpenAIBackend

        choice = MagicMock()
        choice.message.content = "ok"
        choice.message.tool_calls = None
        ok_response = MagicMock()
        ok_response.choices = [choice]
        ok_response.usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=20,
            prompt_tokens_details=SimpleNamespace(
                cached_tokens=3, cache_write_tokens=1
            ),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=4),
        )
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[_rate_limit_error(), ok_response]
        )
        with (
            patch("openai.AsyncOpenAI", return_value=client),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            backend = OpenAIBackend(api_key="test-key")

        seen: list[UsageRecord] = []

        async def _recording_sink(record: UsageRecord) -> None:
            seen.append(record)

        resp = await backend.chat(
            [{"role": "user", "content": "hi"}],
            metered=True,
            sink=_recording_sink,
        )
        assert resp.content == "ok"
        assert len(seen) == 1
        record = seen[0]
        assert (record.prompt_tokens, record.completion_tokens) == (10, 20)
        assert record.reasoning_tokens == 4
        assert (record.cache_read_input_tokens, record.cache_creation_input_tokens) == (
            3,
            1,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# H(30). make_sink fail-open: persistence failure swallowed, commit proceeds;
# asyncio.CancelledError always propagates.
# ═══════════════════════════════════════════════════════════════════════════════


class TestMakeSinkFailOpen:
    """The metering sink never fails the call it meters."""

    async def test_sink_failure_swallowed(self) -> None:
        """H(30): a raising persistence target is logged, never raised."""
        import uuid

        from services.usage_service import make_sink

        async def _doomed(target: object, record: object) -> None:
            raise RuntimeError("metering store down")

        import services.usage_service as usage_module
        from unittest.mock import patch

        sink = make_sink(
            target=AsyncMock(),
            user_id=uuid.uuid4(),
            worker="enrich_episode",
        )
        record = UsageRecord(provider="stub", model="stub-model")

        with patch.object(usage_module, "_persist", side_effect=_doomed):
            # Must not raise — metering is fail-open.
            await sink(record)

    async def test_sink_failure_leaves_outer_commit_intact(self) -> None:
        """H(30): after a swallowed sink failure the caller can still commit."""
        import uuid
        from unittest.mock import patch

        import services.usage_service as usage_module
        from services.usage_service import make_sink

        async def _doomed(target: object, record: object) -> None:
            raise RuntimeError("metering store down")

        db = AsyncMock()
        sink = make_sink(
            target=db,
            user_id=uuid.uuid4(),
            worker="enrich_episode",
        )

        with patch.object(usage_module, "_persist", side_effect=_doomed):
            await sink(UsageRecord(provider="stub", model="stub-model"))

        await db.commit()
        db.commit.assert_awaited_once()

    async def test_sink_cancelled_error_propagates(self) -> None:
        """H(30): task cancellation is never swallowed by the sink."""
        import asyncio as _asyncio
        import uuid
        from unittest.mock import patch

        import services.usage_service as usage_module
        from services.usage_service import make_sink

        async def _cancelled(target: object, record: object) -> None:
            raise _asyncio.CancelledError()

        sink = make_sink(
            target=AsyncMock(),
            user_id=uuid.uuid4(),
            worker="enrich_episode",
        )

        with patch.object(usage_module, "_persist", side_effect=_cancelled):
            with pytest.raises(_asyncio.CancelledError):
                await sink(UsageRecord(provider="stub", model="stub-model"))
