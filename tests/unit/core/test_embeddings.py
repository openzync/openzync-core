"""Unit tests for ``core.embeddings`` — the Ollama-backed embedder.

HTTP is faked at the module's shared ``httpx.AsyncClient`` boundary, so no
Ollama server is needed in CI.  The fake deliberately mimics the two
behaviours of the real ``nomic-embed-text:v1.5`` ``/api/embed`` endpoint
that bit us in production: it applies **no** task prefix of its own, and
it returns **unnormalised** vectors.  The module's own contract is
asserted: canonical width, float element types, unit normalisation, and
the asymmetric passage/query split.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
from typing import TYPE_CHECKING

import numpy as np
import pytest

from core import embeddings as embeddings_mod
from core.embeddings import (
    CANONICAL_EMBED_DIM,
    CANONICAL_EMBED_MODEL,
    PASSAGE_PREFIX,
    QUERY_PREFIX,
    embed_passage,
    embed_query,
    validate_embedding_dim,
)
from core.exceptions import ExternalServiceError

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.unit


def _vector_for(text: str, dim: int) -> np.ndarray:
    """Return a deterministic UNNORMALIZED vector derived from *text*.

    Deriving the value from the text is what makes the prefix regression
    testable: the same text under two different prefixes must produce two
    different vectors.  No real model is downloaded.
    """
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
    return np.random.default_rng(seed).standard_normal(dim).astype(np.float32)


class _FakeEmbedResponse:
    """Minimal stand-in for the ``httpx.Response`` of ``POST /api/embed``."""

    def __init__(self, vectors: list[np.ndarray]) -> None:
        self._vectors = vectors

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, object]:
        return {
            "embeddings": [
                np.asarray(v, dtype=np.float64).tolist() for v in self._vectors
            ]
        }


class _FakeEmbedClient:
    """Stands in for the module-level ``httpx.AsyncClient``.

    Records the exact ``input`` payload each entrypoint POSTs, so the
    asymmetric-prefix contract can be asserted on the HTTP boundary
    rather than inferred from output shape.  ``values`` overrides the
    derived vectors (used for the unnormalised and zero-norm cases) and
    ``dim`` can be set low to simulate a misbehaving model.  The ``calls``
    side (``passage``/``query``) is inferred from the mandatory prefix —
    the fake itself applies none, mirroring the real ``/api/embed``.
    """

    def __init__(
        self, dim: int = CANONICAL_EMBED_DIM, values: np.ndarray | None = None
    ):
        self._dim = dim
        self._values = values
        self.calls: list[str] = []
        self.received: list[list[str]] = []

    async def post(
        self, url: str, json: dict[str, object] | None = None, **_kwargs: object
    ) -> _FakeEmbedResponse:
        texts = list((json or {}).get("input", []))  # type: ignore[union-attr]
        self.received.append(texts)
        if texts and all(t.startswith(PASSAGE_PREFIX) for t in texts):
            self.calls.append("passage")
        elif texts and all(t.startswith(QUERY_PREFIX) for t in texts):
            self.calls.append("query")
        else:
            self.calls.append("unprefixed")
        if self._values is not None:
            vectors = [np.asarray(v) for v in self._values]
        else:
            vectors = [_vector_for(t, self._dim) for t in texts]
        return _FakeEmbedResponse(vectors)


@contextlib.contextmanager
def _installed(fake: _FakeEmbedClient) -> Iterator[_FakeEmbedClient]:
    """Install *fake* as the module-level HTTP client, restoring after."""
    original = embeddings_mod._CLIENT
    embeddings_mod._CLIENT = fake  # type: ignore[assignment]
    try:
        yield fake
    finally:
        embeddings_mod._CLIENT = original


@pytest.fixture
def model():
    """Install a default fake HTTP client; cleared afterwards.

    Yields:
        The fake ``httpx.AsyncClient``.
    """
    with _installed(_FakeEmbedClient()) as fake:
        yield fake


class TestCanonicalConstants:
    """The frozen model/dimension pair."""

    def test_model_is_nomic_15(self) -> None:
        assert CANONICAL_EMBED_MODEL == "nomic-embed-text:v1.5"

    def test_dim_unchanged_at_768(self) -> None:
        """Two columns, two CHECK constraints and four CASTs depend on this."""
        assert CANONICAL_EMBED_DIM == 768

    def test_prefixes_match_the_model_card(self) -> None:
        """Nomic's mandatory task prefixes — verbatim, trailing space included."""
        assert PASSAGE_PREFIX == "search_document: "
        assert QUERY_PREFIX == "search_query: "


class TestEmbedPassage:
    """``embed_passage`` — the corpus side."""

    @pytest.mark.asyncio
    async def test_returns_canonical_normalized_vectors(self, model) -> None:
        vectors = await embed_passage(["first doc", "second doc"])

        assert len(vectors) == 2
        for vec in vectors:
            assert len(vec) == CANONICAL_EMBED_DIM
            assert all(isinstance(v, float) for v in vec)
            assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, abs_tol=1e-3)
        assert model.calls == ["passage"]

    @pytest.mark.asyncio
    async def test_prepends_document_prefix_to_model_input(self, model) -> None:
        """The prefix must reach the model — Ollama will not add it."""
        await embed_passage(["x"])

        assert model.received == [[f"{PASSAGE_PREFIX}x"]]

    @pytest.mark.asyncio
    async def test_preserves_input_order_and_count(self, model) -> None:
        """One vector per input, positionally matched."""
        texts = ["a", "b", "c", "d"]
        vectors = await embed_passage(texts)
        assert len(vectors) == len(texts)

    @pytest.mark.asyncio
    async def test_empty_batch(self, model) -> None:
        assert await embed_passage([]) == []

    @pytest.mark.asyncio
    async def test_wrong_dim_vector_raises(self) -> None:
        """A misbehaving model fails loud at the single validation choke point."""
        with (
            _installed(_FakeEmbedClient(dim=512)),
            pytest.raises(ExternalServiceError) as exc,
        ):
            await embed_passage(["doc"])

        assert exc.value.detail == {
            "source": "embed_passage",
            "got": 512,
            "expected": CANONICAL_EMBED_DIM,
        }


class TestEmbedQuery:
    """``embed_query`` — the search side."""

    @pytest.mark.asyncio
    async def test_returns_canonical_normalized_vectors(self, model) -> None:
        vectors = await embed_query(["a search query"])

        assert len(vectors) == 1
        vec = vectors[0]
        assert len(vec) == CANONICAL_EMBED_DIM
        assert all(isinstance(v, float) for v in vec)
        assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, abs_tol=1e-3)
        assert model.calls == ["query"]

    @pytest.mark.asyncio
    async def test_prepends_query_prefix_to_model_input(self, model) -> None:
        """The prefix must reach the model — Ollama will not add it."""
        await embed_query(["x"])

        assert model.received == [[f"{QUERY_PREFIX}x"]]

    @pytest.mark.asyncio
    async def test_never_calls_the_passage_entrypoint(self, model) -> None:
        """Query side must not touch ``passage_embed`` (retriever pin, mirrored)."""
        await embed_query(["x"])

        assert model.calls == ["query"]
        assert "passage" not in model.calls

    @pytest.mark.asyncio
    async def test_wrong_dim_vector_raises(self) -> None:
        with (
            _installed(_FakeEmbedClient(dim=1024)),
            pytest.raises(ExternalServiceError) as exc,
        ):
            await embed_query(["q"])

        assert exc.value.detail["got"] == 1024


class TestAsymmetricPrefixes:
    """The passage/query split is silent if broken — pin it explicitly."""

    @pytest.mark.asyncio
    async def test_passage_and_query_are_separate_entrypoints(self, model) -> None:
        await embed_passage(["doc"])
        await embed_query(["query"])
        assert model.calls == ["passage", "query"]

    @pytest.mark.asyncio
    async def test_same_text_embeds_differently_per_side(self, model) -> None:
        """Regression guard: identical text, two prefixes, two distinct vectors.

        Before the fix both sides handed the bare text to Ollama, which
        applies no prefix — so the vectors were byte-identical (cosine 1.0).
        """
        passage = (await embed_passage(["same text"]))[0]
        query = (await embed_query(["same text"]))[0]

        # Both sides are unit vectors, so the dot product is the cosine.
        cosine = sum(a * b for a, b in zip(passage, query, strict=True))
        assert cosine < 0.999, f"prefix not applied — cosine {cosine}"

    @pytest.mark.asyncio
    async def test_different_prefixes_reach_the_model(self, model) -> None:
        """The two sides must not collapse onto one prefix."""
        await embed_passage(["x"])
        await embed_query(["x"])

        passage_input, query_input = model.received
        assert passage_input != query_input
        assert passage_input[0].startswith("search_document:")
        assert query_input[0].startswith("search_query:")

    def test_module_docstring_documents_the_split(self) -> None:
        """The requirement is documented, so it survives future edits."""
        doc = embeddings_mod.__doc__ or ""
        assert "embed_passage" in doc
        assert "embed_query" in doc
        assert "asymmetric" in doc.lower()


class TestNormalization:
    """Ollama returns unnormalized vectors for this model — we normalize."""

    @pytest.mark.asyncio
    async def test_unnormalized_model_output_becomes_unit_norm(self) -> None:
        """All-2.0s input (|v| ~ 39) must come back as a unit vector."""
        with _installed(_FakeEmbedClient(values=np.full((2, CANONICAL_EMBED_DIM), 2.0))):
            vectors = await embed_passage(["a", "b"])

        for vec in vectors:
            norm = math.sqrt(sum(v * v for v in vec))
            assert math.isclose(norm, 1.0, abs_tol=1e-6), norm
            assert all(isinstance(v, float) for v in vec)

    @pytest.mark.asyncio
    async def test_query_side_is_normalized_too(self) -> None:
        with _installed(_FakeEmbedClient(values=np.full((1, CANONICAL_EMBED_DIM), 20.0))):
            vec = (await embed_query(["q"]))[0]

        assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, abs_tol=1e-6)

    @pytest.mark.asyncio
    async def test_zero_norm_vector_fails_loud(self) -> None:
        """An unnormalisable vector raises rather than becoming NaN."""
        with (
            _installed(_FakeEmbedClient(values=np.zeros((2, CANONICAL_EMBED_DIM)))),
            pytest.raises(ExternalServiceError) as exc,
        ):
            await embed_passage(["a", "b"])

        assert exc.value.detail["source"] == "embed_passage"
        assert exc.value.detail["zero_norm_rows"] == [0, 1]
        assert "Refusing to store." in str(exc.value)

    @pytest.mark.asyncio
    async def test_nan_vector_fails_loud(self) -> None:
        """NaN norms are not > 0, so they take the same loud path — never stored."""
        with (
            _installed(_FakeEmbedClient(values=np.full((1, CANONICAL_EMBED_DIM), np.nan))),
            pytest.raises(ExternalServiceError),
        ):
            await embed_query(["q"])


class TestValidateEmbeddingDim:
    """The guard itself."""

    def test_accepts_canonical_float_vector(self) -> None:
        assert validate_embedding_dim([0.1] * CANONICAL_EMBED_DIM, source="t") is None

    def test_accepts_ints(self) -> None:
        assert validate_embedding_dim([1] * CANONICAL_EMBED_DIM, source="t") is None

    @pytest.mark.parametrize("dim", [0, 1, 512, 767, 769, 1536])
    def test_rejects_other_widths(self, dim: int) -> None:
        with pytest.raises(ExternalServiceError):
            validate_embedding_dim([0.1] * dim, source="t")

    def test_rejects_bool_elements(self) -> None:
        """``bool`` is an ``int`` subclass — must not sneak through."""
        # Deliberately untyped: the guard exists precisely for callers that
        # hand back a vector of the wrong runtime type.
        vec: list = [False] * CANONICAL_EMBED_DIM
        with pytest.raises(ExternalServiceError):
            validate_embedding_dim(vec, source="t")

    def test_rejects_non_numeric_elements(self) -> None:
        vec: list = ["0.1"] * CANONICAL_EMBED_DIM
        with pytest.raises(ExternalServiceError):
            validate_embedding_dim(vec, source="t")

    def test_error_detail_identifies_source(self) -> None:
        with pytest.raises(ExternalServiceError) as exc_info:
            validate_embedding_dim([0.1] * 512, source="embed_fact")
        assert exc_info.value.detail["source"] == "embed_fact"
        assert "Refusing to store." in str(exc_info.value)
