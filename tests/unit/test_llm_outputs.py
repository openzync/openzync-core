"""Unit tests for ``CombinedLLMOutput`` disabled-section guard + ``chat()`` context.

Covers the user-dictated list E:

- (18) no validation context → no-op (content in every section validates).
- (19) each disabled section with content → ``ValueError`` (5 sections).
- (20) disabled + empty passes, including the default classification.
- (21) ``chat()`` forwards ``validation_context`` on the fast
  (``model_validate_json``) and fallback (fence-stripped ``model_validate``)
  paths; absent context leaves behaviour unchanged.

No I/O: Pydantic validation + a stub ``LLMBackend`` only.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from core.exceptions import LLMStructuredOutputError
from core.llm import ChatResponse, LLMBackend, TokenUsage
from schemas.llm_outputs import CombinedLLMOutput

pytestmark = pytest.mark.unit


def _full_payload() -> dict[str, Any]:
    """Payload with non-empty content in every guard-relevant section."""
    return {
        "classification": {
            "intent": "question",
            "emotion": "joy",
            "valence": "positive",
            "arousal": "low",
            "confidence": 0.9,
        },
        "entities": [{"name": "Alice", "type": "Person"}],
        "relationships": [
            {"subject": "Alice", "predicate": "knows", "object": "Bob"}
        ],
        "facts": [
            {
                "subject": "Alice",
                "predicate": "works_at",
                "object": "Acme",
                "confidence": 0.9,
            }
        ],
        "structured_extractions": {"profile": {"role": "eng"}},
        "invalidations": [
            {
                "existing_fact_ref": "E1",
                "action": "invalidate",
                "reason": "Alice changed teams.",
                "successor_fact_ref": "N1",
            }
        ],
    }


def _empty_payload() -> dict[str, Any]:
    """Payload with every section empty (default classification)."""
    return {
        "classification": {},
        "entities": [],
        "relationships": [],
        "facts": [],
        "structured_extractions": {},
        "invalidations": [],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# E(18). No context → no-op
# ═══════════════════════════════════════════════════════════════════════════════


class TestNoContextNoOp:
    """Without ``context`` the after-validator lets everything through."""

    def test_full_payload_passes_without_context(self) -> None:
        """E(18): every section non-empty, no context → validates."""
        parsed = CombinedLLMOutput.model_validate(_full_payload())
        assert len(parsed.entities) == 1
        assert len(parsed.facts) == 1

    def test_full_payload_passes_with_none_context(self) -> None:
        """E(18): explicit ``context=None`` → validates."""
        parsed = CombinedLLMOutput.model_validate(_full_payload(), context=None)
        assert len(parsed.invalidations) == 1

    def test_full_payload_passes_with_empty_context(self) -> None:
        """E(18): ``context={}`` (no ``disabled`` key) → validates."""
        parsed = CombinedLLMOutput.model_validate(_full_payload(), context={})
        assert parsed.classification.intent == "question"


# ═══════════════════════════════════════════════════════════════════════════════
# E(19). Each disabled section with content → ValueError (5 sections)
# ═══════════════════════════════════════════════════════════════════════════════


def _payload_with_only(section: str) -> dict[str, Any]:
    """Empty payload with exactly one guard-relevant section filled."""
    payload = _empty_payload()
    if section == "classification":
        payload["classification"] = {"intent": "question", "emotion": None}
    elif section == "entities":
        payload["entities"] = [{"name": "Alice", "type": "Person"}]
        payload["relationships"] = [
            {"subject": "Alice", "predicate": "knows", "object": "Bob"}
        ]
    elif section == "facts":
        payload["facts"] = [
            {
                "subject": "Alice",
                "predicate": "works_at",
                "object": "Acme",
                "confidence": 0.5,
            }
        ]
    elif section == "llm_invalidation":
        payload["invalidations"] = [
            {
                "existing_fact_ref": "E1",
                "action": "invalidate",
                "reason": "Retracted.",
                "successor_fact_ref": None,
            }
        ]
    elif section == "structured":
        payload["structured_extractions"] = {"profile": {"role": "eng"}}
    return payload


class TestDisabledSectionsRejected:
    """A disabled section's prompt block was never sent — content is a hallucination."""

    @pytest.mark.parametrize(
        "section",
        ["classification", "entities", "facts", "llm_invalidation", "structured"],
    )
    def test_disabled_section_with_content_raises(self, section: str) -> None:
        """E(19): disabled + non-empty → ``ValueError`` naming the section."""
        payload = _payload_with_only(section)
        with pytest.raises(PydanticValidationError, match=section):
            CombinedLLMOutput.model_validate(payload, context={"disabled": {section}})


# ═══════════════════════════════════════════════════════════════════════════════
# E(20). Disabled + empty passes (incl. default classification)
# ═══════════════════════════════════════════════════════════════════════════════


class TestDisabledEmptyPasses:
    """Empty sections are not hallucinations — they pass even when disabled."""

    def test_all_disabled_all_empty_passes(self) -> None:
        """E(20): every section disabled + empty payload → validates."""
        parsed = CombinedLLMOutput.model_validate(
            _empty_payload(),
            context={
                "disabled": {
                    "classification",
                    "entities",
                    "facts",
                    "invalidations",
                    "structured",
                }
            },
        )
        assert parsed.entities == []
        assert parsed.facts == []

    def test_default_classification_counts_as_empty(self) -> None:
        """E(20): default-constructed classification (null intent+emotion) passes."""
        parsed = CombinedLLMOutput.model_validate(
            _empty_payload(), context={"disabled": {"classification"}}
        )
        assert parsed.classification.intent is None
        assert parsed.classification.emotion is None


# ═══════════════════════════════════════════════════════════════════════════════
# E(21). chat() forwards validation_context (fast + fallback); absent unchanged
# ═══════════════════════════════════════════════════════════════════════════════


class _ScriptedBackend(LLMBackend):
    """Backend replaying one canned ``content`` string per ``_chat`` call."""

    def __init__(self, contents: list[str]) -> None:
        self._contents = list(contents)

    @property
    def provider_name(self) -> str:
        return "scripted"

    @property
    def model_name(self) -> str:
        return "scripted-model"

    async def _chat(
        self,
        messages: list[dict],
        cache_config: Any | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        content = self._contents.pop(0) if self._contents else "{}"
        return ChatResponse(
            content=content,
            model=self.model_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
        )


def _entities_payload_json() -> str:
    """Clean JSON with entity content (violates ``disabled={"entities"}``)."""
    import orjson

    payload = _empty_payload()
    payload["entities"] = [{"name": "Alice", "type": "Person"}]
    return orjson.dumps(payload).decode()


class TestChatForwardsValidationContext:
    """The disabled-section guard only fires when ``chat()`` forwards context."""

    @pytest.mark.asyncio
    async def test_fast_path_forwards_context(self) -> None:
        """E(21): clean-JSON path validates with context → exhausts retries."""
        import orjson

        backend = _ScriptedBackend([_entities_payload_json()] * 3)
        with pytest.raises(LLMStructuredOutputError):
            await backend.chat(
                [{"role": "user", "content": "hi"}],
                response_model=CombinedLLMOutput,
                validation_context={"disabled": {"entities"}},
            )
        # Fast path (model_validate_json) ran on every attempt.
        assert backend._contents == []

    @pytest.mark.asyncio
    async def test_fallback_path_forwards_context(self) -> None:
        """E(21): fence-wrapped path validates with context → exhausts retries."""
        fenced = "```json\n" + _entities_payload_json() + "\n```"
        backend = _ScriptedBackend([fenced] * 3)
        with pytest.raises(LLMStructuredOutputError):
            await backend.chat(
                [{"role": "user", "content": "hi"}],
                response_model=CombinedLLMOutput,
                validation_context={"disabled": {"entities"}},
            )
        # The fence-wrapped payload fails the fast path (fences are not
        # JSON) and fails the fallback path only because of the disabled
        # section — all 3 attempts consumed proves context was forwarded
        # on the fallback path (without it, attempt 1 would succeed).
        assert backend._contents == []

    @pytest.mark.asyncio
    async def test_absent_context_leaves_behaviour_unchanged(self) -> None:
        """E(21): no context → same entity payload validates on first try."""
        backend = _ScriptedBackend([_entities_payload_json()])
        resp = await backend.chat(
            [{"role": "user", "content": "hi"}],
            response_model=CombinedLLMOutput,
        )
        assert isinstance(resp.validated_data, CombinedLLMOutput)
        assert len(resp.validated_data.entities) == 1

    @pytest.mark.asyncio
    async def test_absent_context_fallback_path_unchanged(self) -> None:
        """E(21): no context + fences → fallback validates, no retry."""
        fenced = "```json\n" + _entities_payload_json() + "\n```"
        backend = _ScriptedBackend([fenced])
        resp = await backend.chat(
            [{"role": "user", "content": "hi"}],
            response_model=CombinedLLMOutput,
        )
        assert isinstance(resp.validated_data, CombinedLLMOutput)
        assert len(resp.validated_data.entities) == 1
        assert backend._contents == []
