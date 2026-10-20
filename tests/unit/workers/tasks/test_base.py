"""Unit tests for shared task utilities — ``with_retry`` decorator and constants.

Tests cover:
- ``with_retry``:
  - Success on first attempt (no retry)
  - Retries on exception up to max_retries
  - Max retries exhausted with ``on_exhaustion="raise"`` (default)
  - Max retries exhausted with ``on_exhaustion="log"`` (returns None)
  - Non-retryable exception propagates immediately via ``is_retryable``
  - Exponential backoff (delay doubles each attempt)
  - Custom ``max_retries`` and ``base_delay_s``
  - Invalid ``on_exhaustion`` value raises ``ValueError``
  - Wrapped function preserves ``__name__`` and ``__wrapped__``
- Enrichment bitmask constants:
  - Correct bit positions
  - ``ENRICHMENT_ALL`` includes all active bits
  - ``ENRICHMENT_OBSERVATIONS`` excluded from ``ENRICHMENT_ALL``
  - No overlapping bits
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

# ═══════════════════════════════════════════════════════════════════════════════
# with_retry — success cases
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestWithRetrySuccess:
    """``with_retry`` — happy paths (no retry needed)."""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self) -> None:
        """Success returns immediately without retrying."""
        from workers.tasks.base import with_retry

        call_count = 0

        @with_retry(max_retries=3, base_delay_s=0)
        async def my_task(_ctx: object) -> dict:
            nonlocal call_count
            call_count += 1
            return {"status": "ok"}

        result = await my_task(AsyncMock())
        assert result == {"status": "ok"}
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_preserves_function_name(self) -> None:
        """Decorated function keeps its original ``__name__``."""
        from workers.tasks.base import with_retry

        @with_retry(max_retries=3, base_delay_s=0)
        async def my_named_task(_ctx: object) -> str:
            return "done"

        assert my_named_task.__name__ == "my_named_task"

    @pytest.mark.asyncio
    async def test_returns_value_from_function(self) -> None:
        """Return value from the wrapped function is propagated."""
        from workers.tasks.base import with_retry

        @with_retry(max_retries=2, base_delay_s=0)
        async def my_task(_ctx: object) -> int:
            return 42

        result = await my_task(AsyncMock())
        assert result == 42


# ═══════════════════════════════════════════════════════════════════════════════
# with_retry — retry behaviour
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestWithRetryRetries:
    """``with_retry`` — retries on transient failures."""

    @pytest.mark.asyncio
    async def test_retries_on_exception_and_succeeds(self) -> None:
        """Exception on attempt 1 → retry succeeds on attempt 2."""
        from workers.tasks.base import with_retry

        call_count = 0

        @with_retry(max_retries=3, base_delay_s=0)
        async def my_task(_ctx: object) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ValueError("Not yet")
            return {"status": "completed"}

        result = await my_task(AsyncMock())
        assert result == {"status": "completed"}
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retries_multiple_times(self) -> None:
        """3 failures followed by success on 4th attempt."""
        from workers.tasks.base import with_retry

        call_count = 0

        @with_retry(max_retries=5, base_delay_s=0)
        async def my_task(_ctx: object) -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 4:
                raise RuntimeError("Not ready")
            return "success"

        result = await my_task(AsyncMock())
        assert result == "success"
        assert call_count == 4

    @pytest.mark.asyncio
    async def test_max_retries_exhausted_raises(self) -> None:
        """After max retries, the last exception propagates."""
        from workers.tasks.base import with_retry

        @with_retry(max_retries=2, base_delay_s=0)
        async def my_task(_ctx: object) -> str:
            raise ValueError("Always fails")

        with pytest.raises(ValueError, match="Always fails"):
            await my_task(AsyncMock())

    @pytest.mark.asyncio
    async def test_max_retries_exhausted_logs_and_returns_none(self) -> None:
        """When ``on_exhaustion='log'``, returns None instead of raising."""
        from workers.tasks.base import with_retry

        @with_retry(max_retries=2, base_delay_s=0, on_exhaustion="log")
        async def my_task(_ctx: object) -> str:
            raise ValueError("Always fails")

        result = await my_task(AsyncMock())
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# with_retry — is_retryable predicate
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestWithRetryIsRetryable:
    """``with_retry`` — ``is_retryable`` predicate filtering."""

    @pytest.mark.asyncio
    async def test_non_retryable_exception_propagates_immediately(self) -> None:
        """A non-retryable exception is raised without retry."""
        from workers.tasks.base import with_retry

        call_count = 0

        @with_retry(
            max_retries=3,
            base_delay_s=0,
            is_retryable=lambda e: isinstance(e, ValueError),
        )
        async def my_task(_ctx: object) -> str:
            nonlocal call_count
            call_count += 1
            raise TypeError("Non-retryable")

        with pytest.raises(TypeError, match="Non-retryable"):
            await my_task(AsyncMock())

        assert call_count == 1  # no retry

    @pytest.mark.asyncio
    async def test_retryable_exception_triggers_retry(self) -> None:
        """A retryable exception triggers retry as normal."""
        from workers.tasks.base import with_retry

        call_count = 0

        @with_retry(
            max_retries=3,
            base_delay_s=0,
            is_retryable=lambda e: isinstance(e, ValueError),
        )
        async def my_task(_ctx: object) -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ValueError("Transient")
            return "ok"

        result = await my_task(AsyncMock())
        assert result == "ok"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_none_predicate_retries_all(self) -> None:
        """When ``is_retryable`` is None, all exceptions are retried."""
        from workers.tasks.base import with_retry

        call_count = 0

        @with_retry(max_retries=3, base_delay_s=0, is_retryable=None)
        async def my_task(_ctx: object) -> str:
            nonlocal call_count
            call_count += 1
            raise KeyError("Any error is retryable")

        with pytest.raises(KeyError):
            await my_task(AsyncMock())

        assert call_count == 3  # retried max_retries times


# ═══════════════════════════════════════════════════════════════════════════════
# with_retry — exponential backoff
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestWithRetryBackoff:
    """``with_retry`` — exponential backoff timing."""

    @pytest.mark.asyncio
    async def test_exponential_backoff_doubles_delay(self) -> None:
        """Delay doubles each retry: 1s → 2s → 4s (capped at max_delay_s)."""
        from workers.tasks.base import with_retry

        with patch("workers.tasks.base.asyncio.sleep", AsyncMock()) as mock_sleep:

            @with_retry(max_retries=3, base_delay_s=1.0, max_delay_s=10.0)
            async def my_task(_ctx: object) -> str:
                raise ValueError("Fail")

            with pytest.raises(ValueError):
                await my_task(AsyncMock())

            # 3 retries = 2 sleeps (between attempt 1→2 and 2→3)
            assert mock_sleep.call_count == 2
            sleep_args = [call[0][0] for call in mock_sleep.call_args_list]
            assert sleep_args == pytest.approx([1.0, 2.0])

    @pytest.mark.asyncio
    async def test_backoff_capped_at_max_delay(self) -> None:
        """Delay never exceeds max_delay_s."""
        from workers.tasks.base import with_retry

        with patch("workers.tasks.base.asyncio.sleep", AsyncMock()) as mock_sleep:

            @with_retry(max_retries=4, base_delay_s=2.0, max_delay_s=5.0)
            async def my_task(_ctx: object) -> str:
                raise ValueError("Fail")

            with pytest.raises(ValueError):
                await my_task(AsyncMock())

            sleep_args = [call[0][0] for call in mock_sleep.call_args_list]
            for arg in sleep_args:
                assert arg <= 5.0, f"Delay {arg} exceeds max_delay_s 5.0"
            # Last sleep should be capped at 5.0
            assert sleep_args[-1] == 5.0


# ═══════════════════════════════════════════════════════════════════════════════
# with_retry — invalid arguments
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestWithRetryValidation:
    """``with_retry`` — argument validation."""

    def test_invalid_on_exhaustion_raises_value_error(self) -> None:
        """Invalid ``on_exhaustion`` value raises ValueError at definition time."""
        from workers.tasks.base import with_retry

        with pytest.raises(ValueError, match="on_exhaustion must be 'raise' or 'log'"):
            with_retry(max_retries=3, base_delay_s=0, on_exhaustion="invalid")


# ═══════════════════════════════════════════════════════════════════════════════
# with_retry — default constants
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestRetryDefaults:
    """Default retry constants exported by ``workers/tasks/base.py``."""

    def test_default_max_retries_is_3(self) -> None:
        """``DEFAULT_MAX_RETRIES`` is 3."""
        from workers.tasks.base import DEFAULT_MAX_RETRIES

        assert DEFAULT_MAX_RETRIES == 3

    def test_default_base_delay_is_1_second(self) -> None:
        """``DEFAULT_BASE_DELAY_S`` is 1.0."""
        from workers.tasks.base import DEFAULT_BASE_DELAY_S

        assert DEFAULT_BASE_DELAY_S == 1.0

    def test_default_max_delay_is_30_seconds(self) -> None:
        """``DEFAULT_MAX_DELAY_S`` is 30.0."""
        from workers.tasks.base import DEFAULT_MAX_DELAY_S

        assert DEFAULT_MAX_DELAY_S == 30.0


# ═══════════════════════════════════════════════════════════════════════════════
# Enrichment bitmask constants
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEnrichmentBitmask:
    """Enrichment status bitmask constants."""

    def test_entity_extraction_bit(self) -> None:
        """``ENRICHMENT_ENTITIES`` is bit 0 (value 1)."""
        from workers.tasks.base import ENRICHMENT_ENTITIES

        assert ENRICHMENT_ENTITIES == 1 << 0
        assert ENRICHMENT_ENTITIES == 1

    def test_embedding_bit(self) -> None:
        """``ENRICHMENT_EMBEDDING`` is bit 1 (value 2)."""
        from workers.tasks.base import ENRICHMENT_EMBEDDING

        assert ENRICHMENT_EMBEDDING == 1 << 1
        assert ENRICHMENT_EMBEDDING == 2

    def test_facts_bit(self) -> None:
        """``ENRICHMENT_FACTS`` is bit 2 (value 4)."""
        from workers.tasks.base import ENRICHMENT_FACTS

        assert ENRICHMENT_FACTS == 1 << 2
        assert ENRICHMENT_FACTS == 4

    def test_entity_links_bit(self) -> None:
        """``ENRICHMENT_ENTITY_LINKS`` is bit 3 (value 8)."""
        from workers.tasks.base import ENRICHMENT_ENTITY_LINKS

        assert ENRICHMENT_ENTITY_LINKS == 1 << 3
        assert ENRICHMENT_ENTITY_LINKS == 8

    def test_classification_bit(self) -> None:
        """``ENRICHMENT_CLASSIFICATION`` is bit 4 (value 16)."""
        from workers.tasks.base import ENRICHMENT_CLASSIFICATION

        assert ENRICHMENT_CLASSIFICATION == 1 << 4
        assert ENRICHMENT_CLASSIFICATION == 16

    def test_structured_extraction_bit(self) -> None:
        """``ENRICHMENT_STRUCTURED_EXTRACTION`` is bit 5 (value 32)."""
        from workers.tasks.base import ENRICHMENT_STRUCTURED_EXTRACTION

        assert ENRICHMENT_STRUCTURED_EXTRACTION == 1 << 5
        assert ENRICHMENT_STRUCTURED_EXTRACTION == 32

    def test_observations_bit(self) -> None:
        """``ENRICHMENT_OBSERVATIONS`` is bit 6 (value 64) — reserved."""
        from workers.tasks.base import ENRICHMENT_OBSERVATIONS

        assert ENRICHMENT_OBSERVATIONS == 1 << 6
        assert ENRICHMENT_OBSERVATIONS == 64

    def test_blob_text_bit(self) -> None:
        """``ENRICHMENT_BLOB_TEXT`` is bit 7 (value 128)."""
        from workers.tasks.base import ENRICHMENT_BLOB_TEXT

        assert ENRICHMENT_BLOB_TEXT == 1 << 7
        assert ENRICHMENT_BLOB_TEXT == 128

    def test_enrichment_all_includes_active_bits(self) -> None:
        """``ENRICHMENT_ALL`` bits 0,1,2,3,4,5 (excludes 6,7)."""
        from workers.tasks.base import (
            ENRICHMENT_ALL,
            ENRICHMENT_CLASSIFICATION,
            ENRICHMENT_EMBEDDING,
            ENRICHMENT_ENTITIES,
            ENRICHMENT_ENTITY_LINKS,
            ENRICHMENT_FACTS,
            ENRICHMENT_STRUCTURED_EXTRACTION,
        )

        expected = (
            ENRICHMENT_ENTITIES
            | ENRICHMENT_EMBEDDING
            | ENRICHMENT_FACTS
            | ENRICHMENT_ENTITY_LINKS
            | ENRICHMENT_CLASSIFICATION
            | ENRICHMENT_STRUCTURED_EXTRACTION
        )
        assert expected == ENRICHMENT_ALL

    def test_observations_excluded_from_all(self) -> None:
        """``ENRICHMENT_OBSERVATIONS`` is NOT in ``ENRICHMENT_ALL``."""
        from workers.tasks.base import ENRICHMENT_ALL, ENRICHMENT_OBSERVATIONS

        assert ENRICHMENT_ALL & ENRICHMENT_OBSERVATIONS == 0

    def test_blob_text_excluded_from_all(self) -> None:
        """``ENRICHMENT_BLOB_TEXT`` is NOT in ``ENRICHMENT_ALL``."""
        from workers.tasks.base import ENRICHMENT_ALL, ENRICHMENT_BLOB_TEXT

        assert ENRICHMENT_ALL & ENRICHMENT_BLOB_TEXT == 0

    def test_no_overlapping_bits(self) -> None:
        """All defined bitmask constants have unique bit positions."""
        from workers.tasks.base import (
            ENRICHMENT_BLOB_TEXT,
            ENRICHMENT_CLASSIFICATION,
            ENRICHMENT_EMBEDDING,
            ENRICHMENT_ENTITIES,
            ENRICHMENT_ENTITY_LINKS,
            ENRICHMENT_FACTS,
            ENRICHMENT_OBSERVATIONS,
            ENRICHMENT_STRUCTURED_EXTRACTION,
        )

        bits = [
            ENRICHMENT_ENTITIES,
            ENRICHMENT_EMBEDDING,
            ENRICHMENT_FACTS,
            ENRICHMENT_ENTITY_LINKS,
            ENRICHMENT_CLASSIFICATION,
            ENRICHMENT_STRUCTURED_EXTRACTION,
            ENRICHMENT_OBSERVATIONS,
            ENRICHMENT_BLOB_TEXT,
        ]

        # All should be powers of 2 (single bits)
        for bit in bits:
            assert bit & (bit - 1) == 0, f"{bit} is not a power of 2"

        # No two constants share the same bit
        combined = 0
        for bit in bits:
            assert combined & bit == 0, f"Bit conflict: {bit}"
            combined |= bit


# ═══════════════════════════════════════════════════════════════════════════════
# with_task_metrics — per-job metrics on the ARQ worker context
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestWithTaskMetrics:
    """``with_task_metrics`` — writes task metrics onto the ARQ ctx dict.

    The decorator and the worker's ``on_job_end`` callback share the same
    ctx dict, so these tests pin the keys ``on_job_end`` reads:
    ``task_type``, ``org_id``, ``trace_id``, ``runtime`` and ``status``.
    """

    @pytest.mark.asyncio
    async def test_writes_task_type_onto_ctx(self) -> None:
        """``ctx["task_type"]`` is the wrapped function's name."""
        from workers.tasks.base import with_task_metrics

        @with_task_metrics
        async def my_task(ctx: dict, **_: object) -> str:
            return "done"

        ctx: dict = {}
        await my_task(ctx)

        assert ctx["task_type"] == "my_task"

    @pytest.mark.asyncio
    async def test_writes_org_id_and_trace_id_from_kwargs(self) -> None:
        """``org_id`` and ``trace_id`` are lifted out of the task kwargs."""
        from workers.tasks.base import with_task_metrics

        @with_task_metrics
        async def my_task(ctx: dict, *, org_id: str, trace_id: str) -> str:
            return "done"

        ctx: dict = {}
        await my_task(ctx, org_id="org-1", trace_id="trace-1")

        assert ctx["org_id"] == "org-1"
        assert ctx["trace_id"] == "trace-1"

    @pytest.mark.asyncio
    async def test_accepts_organization_id_alias(self) -> None:
        """``organization_id`` is accepted as an alias for ``org_id``."""
        from workers.tasks.base import with_task_metrics

        @with_task_metrics
        async def my_task(ctx: dict, *, organization_id: str) -> str:
            return "done"

        ctx: dict = {}
        await my_task(ctx, organization_id="org-2")

        assert ctx["org_id"] == "org-2"

    @pytest.mark.asyncio
    async def test_status_is_success_on_success(self) -> None:
        """``ctx["status"]`` is ``"success"`` when the task returns."""
        from workers.tasks.base import with_task_metrics

        @with_task_metrics
        async def my_task(ctx: dict) -> str:
            return "done"

        ctx: dict = {}
        await my_task(ctx)

        assert ctx["status"] == "success"

    @pytest.mark.asyncio
    async def test_status_is_failure_and_exception_reraised_unchanged(self) -> None:
        """A raising task records ``"failure"`` and re-raises the original."""
        from workers.tasks.base import with_task_metrics

        boom = ValueError("task exploded")

        @with_task_metrics
        async def my_task(ctx: dict) -> str:
            raise boom

        ctx: dict = {}
        with pytest.raises(ValueError) as exc_info:
            await my_task(ctx)

        # Re-raised unchanged — not wrapped, not replaced.
        assert exc_info.value is boom
        assert ctx["status"] == "failure"

    @pytest.mark.asyncio
    async def test_runtime_is_recorded_as_positive_number(self) -> None:
        """``ctx["runtime"]`` is a positive elapsed-seconds float."""
        from workers.tasks.base import with_task_metrics

        @with_task_metrics
        async def my_task(ctx: dict) -> str:
            return "done"

        ctx: dict = {}
        await my_task(ctx)

        assert isinstance(ctx["runtime"], float)
        assert ctx["runtime"] > 0

    @pytest.mark.asyncio
    async def test_mutates_the_same_ctx_dict_the_worker_reads(self) -> None:
        """The decorator writes into the caller's dict (not a copy).

        ARQ hands the *same* dict to the task and to ``on_job_end``; a copy
        would leave the worker callback reading an empty context.
        """
        from workers.tasks.base import with_task_metrics

        @with_task_metrics
        async def my_task(ctx: dict) -> str:
            return "done"

        ctx: dict = {}
        await my_task(ctx)

        assert ctx  # the caller's dict was mutated in place

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "SOURCE BUG: with_task_metrics looks the context up by the "
            "literal parameter name 'ctx' (params.get('ctx')) instead of "
            "finding the first positional argument via inspect.signature, "
            "so a task whose context parameter has another name records no "
            "metrics at all.  Fix: resolve the first positional parameter."
        ),
    )
    @pytest.mark.asyncio
    async def test_finds_ctx_regardless_of_parameter_name(self) -> None:
        """The ctx is located through the signature, not by its name."""
        from workers.tasks.base import with_task_metrics

        @with_task_metrics
        async def my_task(context: dict, *, org_id: str) -> str:
            return "done"

        ctx: dict = {}
        await my_task(ctx, org_id="org-3")

        assert ctx["task_type"] == "my_task"
        assert ctx["org_id"] == "org-3"
        assert ctx["status"] == "success"
        assert ctx["runtime"] > 0


# ═══════════════════════════════════════════════════════════════════════════════
# __all__ exports
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestModuleExports:
    """``workers.tasks.base.__all__`` is correctly defined."""

    def test_all_exports_match_constants(self) -> None:
        """Every public constant is in __all__."""
        from workers.tasks.base import __all__

        expected = [
            "ENRICHMENT_ALL",
            "ENRICHMENT_CLASSIFICATION",
            "ENRICHMENT_EMBEDDING",
            "ENRICHMENT_ENTITIES",
            "ENRICHMENT_ENTITY_LINKS",
            "ENRICHMENT_FACTS",
            "ENRICHMENT_BLOB_TEXT",
            "ENRICHMENT_OBSERVATIONS",
            "ENRICHMENT_STRUCTURED_EXTRACTION",
            "with_retry",
            "with_task_metrics",
        ]
        # Subset, not equality: the module gains exports over time and this
        # test must not break when a new public helper is added.  The
        # complementary invariant (no stale entries) is pinned by
        # ``test_no_stale_exports`` below.
        assert set(expected) <= set(__all__)

    def test_no_stale_exports(self) -> None:
        """Every name in __all__ resolves to a real module attribute."""
        import workers.tasks.base as base

        for name in base.__all__:
            assert hasattr(base, name), f"{name} is exported but does not exist"
