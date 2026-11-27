"""Unit tests for repository mappers — episode/message mapping (A2)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from repositories.mappers import episode_to_dict
from schemas.sessions import MessageResponse


def _episode(**kwargs) -> SimpleNamespace:
    """Build a minimal episode-like object for the mapper."""
    return SimpleNamespace(
        id=kwargs.get("id", uuid4()),
        role=kwargs.get("role", "user"),
        content=kwargs.get("content", "Hello"),
        metadata_=kwargs.get("metadata_", {}),
        token_count=kwargs.get("token_count", 10),
        sequence_number=kwargs.get("sequence_number", 0),
        enrichment_status=kwargs.get("enrichment_status", 0),
        created_at=kwargs.get("created_at", datetime.now(UTC)),
    )


@pytest.mark.unit
class TestEpisodeToDict:
    """``episode_to_dict`` enrichment-status mapping."""

    def test_maps_enrichment_status_bits(self) -> None:
        """Raw bitmask is copied through onto the message dict."""
        ep = _episode(enrichment_status=61)
        assert episode_to_dict(ep)["enrichment_status"] == 61

    def test_none_enrichment_status_guards_to_zero(self) -> None:
        """``None`` bits (legacy rows) map to ``0`` via the or-0 guard."""
        ep = _episode(enrichment_status=None)
        assert episode_to_dict(ep)["enrichment_status"] == 0


@pytest.mark.unit
class TestMessageResponseDefaults:
    """``MessageResponse`` enrichment defaults for un-enriched rows."""

    def test_enrichment_defaults(self) -> None:
        """Default ``enrichment_status=0`` and ``is_enrichment_skipped=False``."""
        msg = MessageResponse(
            id=uuid4(),
            role="user",
            content="Hello",
            sequence_number=0,
            created_at=datetime.now(UTC),
        )
        assert msg.enrichment_status == 0
        assert msg.is_enrichment_skipped is False
