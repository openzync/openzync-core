"""The embedder — Ollama is the sole embedding backend.

Embeddings are produced out-of-process by a single hardcoded model,
``nomic-embed-text:v1.5``, served by the Ollama service's ``/api/embed``
endpoint (same v1.5 weights as the retired in-process ONNX build, still
768 dims — which is why no migration was needed). There is no provider
routing, no per-org backend selection, and no env-var model override:
the model tag below is the whole configuration surface. Only the
server address is configurable, via the system-level
``OLLAMA_EMBED_URL`` setting (default ``http://ollama:11434``).

Two columns enforce the dimension at the schema level —
``episodes.embedding`` and ``facts.embedding`` are both ``VECTOR(768)``
with a CHECK constraint and an HNSW cosine index — so every vector must
be exactly :data:`CANONICAL_EMBED_DIM` floats.

Rules enforced through this module:

- **Asymmetric prefixes.** ``nomic-embed-text-v1.5`` is trained with
  distinct search/document prefixes. Corrupting them is silent — you
  get 768 valid floats and quietly degraded recall — so write paths
  must call :func:`embed_passage` and the query path must call
  :func:`embed_query`. Never unify them.
- **The prefixes are applied here, not by Ollama.** The model card
  marks them mandatory and ``/api/embed`` takes raw input strings, so
  both entrypoints below prepend :data:`PASSAGE_PREFIX` /
  :data:`QUERY_PREFIX` themselves.
- **Unit norms are guaranteed here.** Ollama returns raw unnormalized
  vectors, so every batch is normalised at this boundary —
  verify-then-normalize, unconditionally.
- Every returned vector passes through :func:`validate_embedding_dim`
  before it reaches a caller, so a wrong-shape vector fails loud at
  this single choke point instead of at the ``CAST(... AS vector(768))``.
- **No fallback.** A failed inference raises — never zeros, never
  skips, never a local model. Duplicate delivery is fine; duplicate
  side effects are not, so callers stay idempotent instead.

⚠️ A same-dimension model swap is invisible to
:func:`validate_embedding_dim`. Stored vectors from the previous model
stay 768-wide and pass every check while occupying a different vector
space. After changing :data:`CANONICAL_EMBED_MODEL`, run
``scripts/reset_embeddings_for_remodel.py`` to null them, then let
``workers/tasks/reconcile_enrichment.py`` re-enqueue the backfill.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

from core.exceptions import ExternalServiceError

logger = logging.getLogger(__name__)

CANONICAL_EMBED_MODEL: str = "nomic-embed-text:v1.5"
"""The single embedding model all stored vectors are produced with."""

CANONICAL_EMBED_DIM: int = 768
"""The single embedding dimension. Matches ``VECTOR(768)`` DDL."""

MODEL_REVISION: str | None = None
"""Pinned revision of :data:`CANONICAL_EMBED_MODEL`, if one is published.

The pin IS the Ollama tag — Ollama tags are mutable, so digest-pinning
lives in infra (image/model digest), not here. Do NOT invent a hash.
"""

PASSAGE_PREFIX: str = "search_document: "
"""Mandatory task prefix for the corpus side. Applied by :func:`embed_passage`."""

QUERY_PREFIX: str = "search_query: "
"""Mandatory task prefix for the query side. Applied by :func:`embed_query`."""

OLLAMA_KEEP_ALIVE: str = "24h"
"""``keep_alive`` sent on every ``/api/embed`` call — keeps the 274MB
F16 weights resident so steady-state inference never pays a reload."""

EMBED_TIMEOUT_S: float = 30.0
"""Per-request read/write budget for ``/api/embed`` (10–30s budget)."""

EMBED_CONNECT_TIMEOUT_S: float = 5.0
"""TCP+TLS connect budget — a hung connect must fail fast, not eat the 30s."""

EMBED_MAX_INFLIGHT: int = 8
"""Per-process cap on concurrent ``/api/embed`` calls (Ollama serialises
batches on one GPU; unbounded fan-out from N worker tasks just queues
inside Ollama while holding event-loop slots here)."""

_EMBED_SEMAPHORE: asyncio.Semaphore = asyncio.Semaphore(EMBED_MAX_INFLIGHT)
"""Bounds concurrent embed calls per process — see :data:`EMBED_MAX_INFLIGHT`."""

_DEFAULT_BASE_URL: str = "http://ollama:11434"
"""Compose-DNS default for the Ollama server. Overridden by the
system-level ``OLLAMA_EMBED_URL`` setting once settings initialise."""

# ── Module-level HTTP client (lazy) ───────────────────────────────────

_CLIENT: httpx.AsyncClient | None = None
"""Shared client. Created lazily — never at import time, so importing
this module needs no running loop and no reachable Ollama."""

_WARMED: bool = False
"""True once an inference has succeeded in this process (set by
:func:`prewarm_embeddings` at boot or by the first successful embed)."""


# ── Public API ─────────────────────────────────────────────────────────


async def embed_passage(texts: list[str]) -> list[list[float]]:
    """Embed corpus-side text, prefixing it with :data:`PASSAGE_PREFIX`.

    Use this for everything that is written to ``episodes.embedding`` or
    ``facts.embedding``. Calling :func:`embed_query` here silently
    degrades retrieval quality — the prefixes are not interchangeable.

    The prefix is prepended by this module, because Ollama does not
    prepend it (see the module docstring) — callers pass bare text.

    Args:
        texts: Texts to embed, one vector returned per input, in order.

    Returns:
        A list of 768-float unit vectors, positionally matching ``texts``.

    Raises:
        ExternalServiceError: If the embedder returns a vector that is
            not exactly :data:`CANONICAL_EMBED_DIM` floats, a zero-norm
            vector (unnormalisable, and NaN-poisoning downstream), or a
            malformed payload (missing key, count mismatch).
        Exception: Whatever ``httpx`` raises on connection or inference
            — logged with the model tag, propagated unmodified, never
            swallowed.
    """
    # note: the prefix is NOT optional — /api/embed takes raw strings,
    # and omitting it silently degrades recall with no error. Do not
    # "simplify" this into a bare pass-through of `texts`.
    vectors = await _post_embed(
        [PASSAGE_PREFIX + text for text in texts], source="embed_passage"
    )
    for vec in vectors:
        validate_embedding_dim(vec, source="embed_passage")
    return vectors


async def embed_query(texts: list[str]) -> list[list[float]]:
    """Embed search queries, prefixing each with :data:`QUERY_PREFIX`.

    Use this only for the query side of retrieval (see
    :meth:`services.hybrid_retriever.HybridRetriever._embed_query`).
    Calling :func:`embed_passage` here silently degrades recall.

    The prefix is prepended by this module, because Ollama does not
    prepend it (see the module docstring) — callers pass bare text.

    Args:
        texts: Query strings to embed, one vector returned per input, in
            order.

    Returns:
        A list of 768-float unit vectors, positionally matching ``texts``.

    Raises:
        ExternalServiceError: If the embedder returns a vector that is
            not exactly :data:`CANONICAL_EMBED_DIM` floats, a zero-norm
            vector (unnormalisable, and NaN-poisoning downstream), or a
            malformed payload (missing key, count mismatch).
        Exception: Whatever ``httpx`` raises on connection or inference
            — logged with the model tag, propagated unmodified, never
            swallowed.
    """
    # note: see embed_passage — the prefix is mandatory and applied here.
    vectors = await _post_embed(
        [QUERY_PREFIX + text for text in texts], source="embed_query"
    )
    for vec in vectors:
        validate_embedding_dim(vec, source="embed_query")
    return vectors


def validate_embedding_dim(vec: list[float], *, source: str) -> None:
    """Reject any embedding that is not exactly canonical-dim.

    Args:
        vec: The embedding vector returned by the provider.
        source: Caller name for the error detail (e.g. ``"embed_fact"``).

    Raises:
        ExternalServiceError: If ``len(vec) != CANONICAL_EMBED_DIM`` or any
            element is not a float/int.
    """
    if len(vec) != CANONICAL_EMBED_DIM or not all(
        isinstance(v, (float, int)) and not isinstance(v, bool) for v in vec
    ):
        raise ExternalServiceError(
            message=(
                f"Invalid embedding in {source}: len {len(vec)} "
                f"(expected canonical {CANONICAL_EMBED_DIM} float elements). "
                "Refusing to store."
            ),
            detail={
                "source": source,
                "got": len(vec),
                "expected": CANONICAL_EMBED_DIM,
            },
        )


def is_model_loaded() -> bool:
    """Report whether the embedder has proven reachable in this process.

    Used by readiness probes so ``/ready`` stays 503 until the boot-time
    :func:`prewarm_embeddings` completes, while ``/health`` stays
    liveness-only.

    Returns:
        True once an inference has succeeded (prewarm or first embed),
        else False.
    """
    return _WARMED


async def prewarm_embeddings() -> None:
    """Prove Ollama reachable and run one warm inference.

    Called once at boot (API lifespan, worker startup) so an unreachable
    Ollama fails fast instead of surfacing as user-facing 503s.
    Callers must NOT catch — any error aborts startup loudly by design.

    Raises:
        Exception: Whatever the reachability check or warm inference
            raises, unmodified.
    """
    await _check_reachable()
    await embed_query(["ready"])


async def prewarm_with_retry(max_attempts: int = 3, base_delay_s: float = 1.0) -> None:
    """Prewarm with jittered retry — absorbs slow Ollama cold starts.

    Only the elected leader calls this at boot (see worker leader lock);
    followers skip prewarm entirely so N replicas don't stampede one
    cold Ollama.  Retries cover the cold-start window (model load after
    an Ollama restart); a still-dead Ollama after ``max_attempts`` raises
    loud — same fail-fast contract as :func:`prewarm_embeddings`.

    Args:
        max_attempts: Total prewarm attempts before giving up.
        base_delay_s: Base backoff between attempts, plus ±50% jitter.

    Raises:
        Exception: The last attempt's error, unmodified.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            await prewarm_embeddings()
            return
        except Exception as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            delay = base_delay_s * attempt * random.uniform(0.5, 1.5)  # noqa: S311 — backoff jitter, not cryptographic
            logger.warning(
                "embeddings.prewarm_retry",
                extra={"attempt": attempt, "delay_s": round(delay, 2)},
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


# ── Internal helpers ───────────────────────────────────────────────────


def _embed_base_url() -> str:
    """Return the configured Ollama base URL.

    Reads the system-level ``OLLAMA_EMBED_URL`` setting. When settings
    are not initialised yet (unit tests, operator scripts), the
    compose-DNS default applies — the same value the setting defaults
    to, so behaviour never diverges silently.

    Returns:
        The base URL with no trailing slash.
    """
    try:
        from core.config import get_settings  # noqa: PLC0415

        url = get_settings().OLLAMA_EMBED_URL
    except (RuntimeError, AttributeError):
        return _DEFAULT_BASE_URL
    return (url or _DEFAULT_BASE_URL).rstrip("/")


def _get_client() -> httpx.AsyncClient:
    """Return the shared client, creating it lazily on first use.

    Creation is synchronous and loop-free, so this is safe to call from
    any coroutine without lifespan wiring.  Connect and read/write get
    separate budgets: a hung TCP connect fails in 5s instead of eating
    the 30s inference budget.

    Returns:
        The module-level shared ``httpx.AsyncClient``.
    """
    global _CLIENT  # noqa: PLW0603 — intentional module-level cache
    if _CLIENT is None:
        _CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=EMBED_CONNECT_TIMEOUT_S,
                read=EMBED_TIMEOUT_S,
                write=EMBED_TIMEOUT_S,
                pool=EMBED_CONNECT_TIMEOUT_S,
            )
        )
    return _CLIENT


async def _check_reachable() -> None:
    """GET Ollama's ``/api/tags`` to prove the embed server is up.

    Raises:
        Exception: Whatever ``httpx`` raises — logged with the model
            tag, propagated unmodified, never swallowed.
    """
    client = _get_client()
    url = f"{_embed_base_url()}/api/tags"
    started = time.perf_counter()
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except Exception as exc:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        logger.error(
            "embeddings.prewarm_unreachable",
            extra={
                "model": CANONICAL_EMBED_MODEL,
                "error": str(exc),
                "duration_ms": duration_ms,
            },
            exc_info=True,
        )
        raise


async def _post_embed(texts: list[str], *, source: str) -> list[list[float]]:
    """POST one prefixed batch to Ollama ``/api/embed`` and normalise.

    Runs fully on-loop — one ``httpx`` POST needs no executor, and
    concurrency is bounded by the per-process semaphore (not the loop).
    Exactly one retry on transient transport errors AND on retryable
    statuses (429/5xx) with backoff; anything else fails loud
    immediately with the status and body snippet logged.

    Args:
        texts: Already-prefixed input strings (prefix applied by the
            caller — this helper must never see bare text).
        source: Caller name for error details and log extras.

    Returns:
        One unit-norm list of floats per input text, in order. Empty in,
        empty out — no HTTP call for an empty batch.

    Raises:
        ExternalServiceError: If the payload is malformed (missing
            ``embeddings`` key, count mismatch) or any row has zero (or
            NaN) norm, which cannot be normalised into a unit vector.
        Exception: Whatever ``httpx`` raises — logged with the model
            tag, propagated unmodified, never swallowed.
    """
    global _WARMED  # noqa: PLW0603 — intentional module-level flag
    if not texts:
        _get_client()
        return []

    client = _get_client()
    url = f"{_embed_base_url()}/api/embed"
    payload = {
        "model": CANONICAL_EMBED_MODEL,
        "input": texts,
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }
    started = time.perf_counter()
    data: dict = {}
    async with _EMBED_SEMAPHORE:
        for attempt in (1, 2):
            try:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                break
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                retryable = status == 429 or status >= 500
                if retryable and attempt == 1:
                    # Ollama 429/5xx under load or mid-reload — back off
                    # once instead of failing a whole batch loud.
                    delay = 0.5 * random.uniform(0.5, 1.5)  # noqa: S311 — backoff jitter, not cryptographic
                    logger.warning(
                        "embeddings.inference_retry_status",
                        extra={
                            "source": source,
                            "model": CANONICAL_EMBED_MODEL,
                            "status_code": status,
                            "attempt": attempt,
                            "delay_s": round(delay, 2),
                        },
                    )
                    await asyncio.sleep(delay)
                    continue
                duration_ms = round((time.perf_counter() - started) * 1000, 1)
                logger.error(
                    "embeddings.inference_failed",
                    extra={
                        "source": source,
                        "model": CANONICAL_EMBED_MODEL,
                        "status_code": status,
                        "detail": exc.response.text[:500],
                        "duration_ms": duration_ms,
                    },
                    exc_info=True,
                )
                raise
            except httpx.TransportError as exc:
                if attempt == 2:
                    duration_ms = round((time.perf_counter() - started) * 1000, 1)
                    logger.error(
                        "embeddings.inference_failed",
                        extra={
                            "source": source,
                            "model": CANONICAL_EMBED_MODEL,
                            "error": str(exc),
                            "duration_ms": duration_ms,
                        },
                        exc_info=True,
                    )
                    raise
                logger.warning(
                    "embeddings.inference_retry",
                    extra={
                        "source": source,
                        "model": CANONICAL_EMBED_MODEL,
                        "attempt": attempt,
                        "error": str(exc),
                    },
                )
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    logger.info(
        "embeddings.inference_completed",
        extra={
            "source": source,
            "model": CANONICAL_EMBED_MODEL,
            "count": len(texts),
            "duration_ms": duration_ms,
        },
    )

    rows = data.get("embeddings")
    if not isinstance(rows, list) or len(rows) != len(texts):
        raise ExternalServiceError(
            message=(
                f"Invalid embedding in {source}: Ollama returned "
                f"{len(rows) if isinstance(rows, list) else type(rows).__name__} "
                f"vectors for {len(texts)} inputs. Refusing to store."
            ),
            detail={
                "source": source,
                "got": len(rows) if isinstance(rows, list) else 0,
                "expected": len(texts),
            },
        )
    vectors = _normalize_vectors([list(row) for row in rows], source=source)
    _WARMED = True
    return vectors


def _normalize_vectors(rows: list[list[float]], *, source: str) -> list[list[float]]:
    """Normalise one Ollama batch into unit-norm nested lists.

    Ollama returns raw unnormalized vectors, so normalisation happens
    unconditionally at this boundary — every stored vector has a
    guaranteed unit norm, and a future switch to dot-product / ``<#>``
    index ops cannot silently change the ranking scale.

    Args:
        rows: Raw vectors from ``/api/embed`` (already count-checked).
        source: Caller name for the error detail.

    Returns:
        One unit-norm list of floats per input row.

    Raises:
        ExternalServiceError: If any row has zero (or NaN) norm, which
            cannot be normalised into a unit vector.
    """
    import numpy as np  # noqa: PLC0415 — keeps module import light

    if not rows:
        return []
    matrix = np.array(rows, dtype=np.float64)
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    if not bool(np.all(norms > 0)):
        raise ExternalServiceError(
            message=(
                f"Invalid embedding in {source}: model returned a zero-norm "
                "vector, which cannot be normalised. Refusing to store."
            ),
            detail={
                "source": source,
                "zero_norm_rows": np.flatnonzero(norms <= 0).tolist(),
            },
        )
    return (matrix / norms).tolist()


if __name__ == "__main__":  # pragma: no cover — operator self-check
    import asyncio as _asyncio
    import math as _math

    async def _self_check() -> None:
        texts = ["openzyc persists agent memory in a pgvector graph", "unrelated text"]

        for fn in (embed_passage, embed_query):
            vecs = await fn(texts)
            assert len(vecs) == len(texts), f"{fn.__name__}: wrong vector count"
            for vec in vecs:
                assert len(vec) == CANONICAL_EMBED_DIM, (
                    f"{fn.__name__}: got {len(vec)}, want {CANONICAL_EMBED_DIM}"
                )
                assert all(isinstance(v, float) for v in vec), (
                    f"{fn.__name__}: non-float element"
                )
                norm = _math.sqrt(sum(v * v for v in vec))
                assert abs(norm - 1.0) < 1e-3, f"{fn.__name__}: not normalized ({norm})"
            print(f"{fn.__name__}: OK — {len(vecs)}x{CANONICAL_EMBED_DIM}, |v|=1")

        # The prefixes must actually reach the model: the same text embedded on
        # both sides must NOT land on the same vector (both are unit vectors, so
        # the dot product IS the cosine).
        probe = "openzyc persists agent memory in a pgvector graph"
        passage_vec = (await embed_passage([probe]))[0]
        query_vec = (await embed_query([probe]))[0]
        same_text_cosine = sum(
            a * b for a, b in zip(passage_vec, query_vec, strict=True)
        )
        assert same_text_cosine < 0.999, (
            f"prefixes not applied — identical text scored {same_text_cosine:.4f}"
        )
        print(
            "prefix asymmetry: OK — identical text cosine "
            f"{same_text_cosine:.4f} < 0.999"
        )

        # Dim validation must reject a wrong-width vector, not pad or truncate.
        try:
            validate_embedding_dim([0.1] * 512, source="self_check")
        except Exception as exc:
            assert type(exc).__name__ == "ExternalServiceError", type(exc).__name__
            print("validate_embedding_dim: OK — rejects 512")
        else:
            raise AssertionError("validate_embedding_dim accepted a 512-dim vector")

    _asyncio.run(_self_check())
