"""Unit tests for core/queue_names.py — the ARQ queue-name builder.

The queue label is shared by the API (which enqueues jobs) and the worker
(which consumes them), so the builder lives in ``core/`` — the API-side
modules must not import from the worker package.

The rendered label is a **public contract**: the Prometheus metric
``openzync_worker_queue_depth`` is labelled with it and the Grafana
dashboard selects it with the regex ``OpenZync:.*:queue:high`` /
``OpenZync:.*:queue:low``.  A change to the format silently empties the
dashboard, so the format is pinned here.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import re
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _queue_names() -> Any:
    """Import ``core.queue_names`` — the module the builder must live in.

    Imported lazily inside the test body: a missing module has to fail
    with a clear message rather than a collection error that hides every
    other assertion in this file.
    """
    if importlib.util.find_spec("core.queue_names") is None:
        pytest.fail(
            "core/queue_names.py is missing — the queue-name builder must live "
            "in core/ so the API never has to import from the worker package.",
        )
    return importlib.import_module("core.queue_names")


class TestGetQueueName:
    """``get_queue_name`` — the queue label format."""

    def test_renders_the_documented_format(self) -> None:
        """The label is ``OpenZync:{env}:queue:{queue_type}``."""
        get_queue_name = _queue_names().get_queue_name
        assert get_queue_name("dev", "high") == "OpenZync:dev:queue:high"
        assert get_queue_name("prod", "low") == "OpenZync:prod:queue:low"

    @pytest.mark.parametrize("queue_type", ["high", "low"])
    def test_the_label_matches_the_grafana_dashboard_regex(
        self, queue_type: str
    ) -> None:
        """The label still matches the dashboard's selector.

        The Grafana overview selects the queue-depth gauge with
        ``queue_name=~"OpenZync:.*:queue:high"`` (and the ``low``
        equivalent) — a renamed prefix or a different separator would
        empty the panel without failing anything else.
        """
        label = _queue_names().get_queue_name("prod", queue_type)
        assert re.fullmatch(rf"OpenZync:.*:queue:{queue_type}", label)

    def test_the_two_queue_types_are_high_and_low(self) -> None:
        """Only the two documented queue types exist."""
        assert set(_queue_names().QUEUE_TYPES) == {"high", "low"}

    def test_an_unknown_queue_type_raises(self) -> None:
        """A typo in the queue type is loud, never a silently wrong label."""
        with pytest.raises(ValueError, match="queue_type must be 'high' or 'low'"):
            _queue_names().get_queue_name("prod", "medium")


class TestQueueNameLocation:
    """The builder lives in ``core/`` — the API must not import the worker."""

    def test_the_builder_lives_in_core_queue_names(self) -> None:
        """``get_queue_name`` is defined in ``core/queue_names.py``."""
        source = Path(inspect.getsourcefile(_queue_names().get_queue_name) or "")
        assert source.name == "queue_names.py"
        assert source.parent.name == "core"

    def test_the_worker_settings_module_re_exports_it(self) -> None:
        """``services.worker.worker_settings.get_queue_name`` still resolves.

        The worker module re-exports the builder because it is part of that
        module's public surface — but it must be the *same* function, so
        the API and the worker can never drift apart.
        """
        from services.worker import worker_settings

        assert worker_settings.get_queue_name is _queue_names().get_queue_name

    def test_the_worker_settings_queue_names_use_the_same_format(self) -> None:
        """``WorkerSettings.high_queue_full`` / ``low_queue_full`` match."""
        from services.worker.worker_settings import WorkerSettings

        settings = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost:5432/test",
            REDIS_URL="redis://localhost:6379/0",
            ENV="prod",
        )
        assert settings.high_queue_full == "OpenZync:prod:queue:high"
        assert settings.low_queue_full == "OpenZync:prod:queue:low"
