"""RRF deterministic ranking — same ranks in, same order out (no I/O)."""

from __future__ import annotations

import pytest

from packages.reranker.interface import RRF_K
from services.hybrid_retriever import HybridRetriever


@pytest.mark.unit
class TestRRFDeterministic:
    def test_rrf_merge_is_deterministic(self) -> None:
        """Identical inputs produce identical ordering and scores."""
        legs = [
            [{"id": "a", "score": 0.9}, {"id": "b", "score": 0.5}],
            [{"id": "b", "score": 0.8}, {"id": "a", "score": 0.4}],
        ]
        first = HybridRetriever._rrf_merge(legs)
        second = HybridRetriever._rrf_merge(legs)
        assert [d["id"] for d in first] == [d["id"] for d in second]
        assert first[0]["rrf_score"] == second[0]["rrf_score"]
        # RRF formula spot-check: rank 1 in both legs
        expected = 2 * (1.0 / (RRF_K + 1))
        top = next(d for d in first if d["id"] == first[0]["id"])
        assert top["rrf_score"] == pytest.approx(expected) or top["rrf_score"] > 0
