"""Blob text extraction worker — extract text from uploaded file attachments.

This worker runs as a low-priority ARQ task. For each blob (image, PDF,
document, etc.), it:

1. Downloads the file from S3-compatible storage
2. Extracts text based on MIME type:
   - ``text/*``: direct UTF-8 decode
   - ``application/pdf``: PyMuPDF (fitz) text extraction
   - ``application/vnd.openxmlformats-officedocument.wordprocessingml.document``:
     python-docx
   - ``text/csv``, ``text/plain``: direct UTF-8 decode
   - ``image/*``: skipped (reserved for future OCR/vision integration)
   - ``application/octet-stream``: try magic-based detection, fall back to skip
3. Stores the extracted text in ``episode_blobs.extracted_text``
4. Sets the ``ENRICHMENT_BLOB_TEXT`` bit on the episode's enrichment_status

The extract blob text task is idempotent — it checks the ``ENRICHMENT_BLOB_TEXT``
bit before running and skips if already set.

Bitmask:
    Sets ``episodes.enrichment_status`` bit 7
    (``ENRICHMENT_BLOB_TEXT``) only when text was actually extracted
    and stored. A genuine no-text outcome (unsupported MIME, empty
    payload) leaves the bit unset — not an error, but the work is not
    marked complete either.

Fail-closed: corrupt input, a missing PII policy, redaction failures,
and org-config fetch failures all propagate (no bit, no commit) so
``@with_retry`` retries. Missing extractor libraries (``ImportError``)
fail fast via ``is_retryable`` — a deployment problem retries cannot
heal. Nothing is ever stored unredacted.
"""

from __future__ import annotations

import asyncio
import io
from uuid import UUID

import structlog

from workers.tasks.base import ENRICHMENT_BLOB_TEXT, with_retry, with_task_metrics

logger = structlog.get_logger(__name__)


# ── MIME type routing ────────────────────────────────────────────────────────

_GENERIC_MIMES = frozenset({"application/octet-stream", "binary/octet-stream"})
"""Claimed types carrying no signal — libmagic detection is authoritative."""


def _extract_pdf(data: bytes) -> str | None:
    """Extract text from a PDF using PyMuPDF (fitz).

    Only a missing library is reported here — decode failures propagate
    so the task retries instead of marking the blob done with no text.

    Args:
        data: Raw PDF bytes.

    Returns:
        Extracted text, or ``None`` when the PDF holds no text.

    Raises:
        ImportError: If PyMuPDF is not installed.
        Exception: If the PDF is corrupt or unreadable (retryable).
    """
    try:
        import fitz  # PyMuPDF — optional dependency
    except ImportError:
        logger.warning("extract_blob_text.pymupdf_not_available")
        raise

    doc = fitz.open(stream=data, filetype="pdf")
    try:
        text_parts: list[str] = []
        for page in doc:
            text_parts.append(page.get_text())
    finally:
        doc.close()
    result = "\n".join(text_parts).strip()
    return result if result else None


def _extract_docx(data: bytes) -> str | None:
    """Extract text from a DOCX file using python-docx.

    Only a missing library is reported here — decode failures propagate
    so the task retries instead of marking the blob done with no text.

    Args:
        data: Raw DOCX bytes.

    Returns:
        Extracted text, or ``None`` when the document holds no text.

    Raises:
        ImportError: If python-docx is not installed.
        Exception: If the DOCX is corrupt or unreadable (retryable).
    """
    try:
        from docx import Document  # python-docx — optional dependency
    except ImportError:
        logger.warning("extract_blob_text.docx_not_available")
        raise

    doc = Document(io.BytesIO(data))
    text_parts = [p.text for p in doc.paragraphs if p.text.strip()]
    result = "\n".join(text_parts).strip()
    return result if result else None


def _extract_text_plain(data: bytes) -> str | None:
    """Decode raw bytes as UTF-8 text (lossy — never raises).

    Args:
        data: Raw bytes.

    Returns:
        Decoded text, or ``None`` when the payload holds no text.
    """
    text_content = data.decode("utf-8", errors="replace").strip()
    return text_content if text_content else None


async def _extract_image_ocr(data: bytes) -> str | None:
    """Extract text from an image using Tesseract OCR.

    Offloads the synchronous PIL decode + ``pytesseract`` call to a
    thread pool via ``asyncio.to_thread()`` so the event loop is not
    blocked during CPU-bound OCR processing.

    Args:
        data: Raw image bytes (PNG, JPEG, WebP, TIFF, BMP, etc.).

    Returns:
        Extracted text, or ``None`` when the image holds no text.

    Raises:
        ImportError: If OCR dependencies are not installed.
        Exception: If OCR processing fails (retryable).
    """
    try:
        import pytesseract  # noqa: F401 — availability gate; real import runs in _ocr_sync
        from PIL import (
            Image,  # noqa: F401 — availability gate; real import runs in _ocr_sync
        )
    except ImportError:
        logger.warning(
            "extract_blob_text.ocr_not_available",
            extra={"detail": "Install pytesseract and Pillow for OCR support."},
        )
        raise

    text = await asyncio.to_thread(_ocr_sync, data)
    result = text.strip()
    return result if result else None


def _ocr_sync(data: bytes) -> str:
    """Decode image bytes and run Tesseract (runs in a worker thread).

    Args:
        data: Raw image bytes.

    Returns:
        Raw OCR text (may be blank — the caller strips and filters).
    """
    import pytesseract
    from PIL import Image

    image = Image.open(io.BytesIO(data))
    return pytesseract.image_to_string(
        image,
        lang="eng",
        config="--psm 3",  # Automatic page segmentation
    )


# ── MIME type dispatch ───────────────────────────────────────────────────────


async def _dispatch_extraction(mime_type: str, data: bytes) -> str | None:
    """Route blob content to the right text extractor based on MIME type.

    Args:
        mime_type: The blob's MIME type string.
        data: Raw file bytes.

    Returns:
        Extracted text, or ``None`` for a genuine no-text outcome (an
        unsupported MIME type or an empty payload). The caller leaves the
        enrichment bit unset and stores nothing for ``None`` — it is not
        an error, but the work is not marked complete either.

    Raises:
        Exception: Propagates extractor failures (corrupt input, missing
            libraries, magic-detection errors) so the caller retries
            instead of marking success.
    """
    if mime_type in _GENERIC_MIMES:
        # Generic container claim carries no signal — libmagic decides.
        # Recursion-safe: a detected generic/empty result is a genuine
        # no-text outcome, not another round of detection.
        try:
            import magic  # python-magic — optional, for MIME detection

            detected = magic.from_buffer(data, mime=True)
        except ImportError:
            return None
        if not detected or detected in _GENERIC_MIMES or detected == mime_type:
            return None
        return await _dispatch_extraction(detected, data)

    if mime_type.startswith("text/"):
        # text/plain, text/csv, text/markdown, etc.
        return _extract_text_plain(data)

    if mime_type == "application/pdf":
        # fitz is CPU-bound sync — off the event loop.
        return await asyncio.to_thread(_extract_pdf, data)

    if mime_type in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ):
        # python-docx is CPU-bound sync — off the event loop.
        return await asyncio.to_thread(_extract_docx, data)

    if mime_type.startswith("image/"):
        return await _extract_image_ocr(data)

    # Unknown type — try magic-based detection, then skip
    try:
        import magic  # python-magic — optional, for MIME detection fallback

        detected = magic.from_buffer(data, mime=True)
        if detected and detected != mime_type:
            return await _dispatch_extraction(detected, data)
    except ImportError:
        pass

    return None


# ── Helper: resolve org PII config (fail-closed) ───────────────────────────


async def _redact_extracted_text(
    extracted_text: str,
    *,
    user_id: str,
    db: object,
    trace_id: str,
    log: structlog.typing.FilteringBoundLogger,
) -> str:
    """Redact PII from extracted blob text, fail-closed.

    Fetches the user PII policy from the DB.  A fetch failure or a
    redaction failure raises ``PIIUnavailableError`` — the caller then
    skips ``update_extracted_text``, skips the ``ENRICHMENT_BLOB_TEXT``
    bit, and lets ``@with_retry`` retry.  A block-mode ``ValidationError``
    downgrades to a single mask pass (preserved semantic).

    Args:
        extracted_text: Raw text from the MIME extractor.
        user_id: The user UUID string (log context only).
        db: Open async DB session (RLS context already set).
        trace_id: Request trace ID for end-to-end correlation.
        log: Bound structlog logger.

    Returns:
        The redacted text (unchanged when mode is ``off`` or clean).

    Raises:
        PIIUnavailableError: If the policy cannot be fetched or redaction
            fails — never store unredacted text.
    """
    from core.exceptions import PIIUnavailableError, ValidationError

    try:
        from core.org_config import get_platform_config

        org_cfg = await get_platform_config(
            UUID(user_id),
            redis=None,
            db=db,  # type: ignore[arg-type]
        )
    except Exception as exc:
        logger.error(
            "pii.fail_closed",
            user_id=user_id,
            trace_id=trace_id,
            exc_info=True,
        )
        raise PIIUnavailableError("pii_unavailable") from exc

    if org_cfg is None or org_cfg.pii_mode is None:
        if org_cfg is None:
            logger.error(
                "pii.fail_closed",
                user_id=user_id,
                trace_id=trace_id,
                reason="org config not found",
            )
            raise PIIUnavailableError("pii_unavailable")
        pii_config: dict = {"mode": "mask"}
    else:
        pii_config = {"mode": org_cfg.pii_mode}
        if org_cfg.pii_sensitivity is not None:
            pii_config["sensitivity"] = org_cfg.pii_sensitivity
        if org_cfg.pii_enabled_types is not None:
            pii_config["enabled_types"] = org_cfg.pii_enabled_types
        if org_cfg.pii_min_confidence is not None:
            pii_config["min_confidence"] = org_cfg.pii_min_confidence

    if pii_config.get("mode", "mask") == "off":
        return extracted_text

    from services.pii_service import PIIService

    try:
        try:
            pii_service = PIIService(pii_config)
            redacted, _, _ = await pii_service.process_message(extracted_text)
        except ValidationError:
            mask_service = PIIService({**pii_config, "mode": "mask"})
            redacted, _, _ = await mask_service.process_message(extracted_text)
            log.info("extract_blob_text.pii_blocked_redacted")
        return redacted
    except PIIUnavailableError:
        raise
    except Exception as exc:
        logger.error(
            "pii.fail_closed",
            user_id=user_id,
            trace_id=trace_id,
        )
        raise PIIUnavailableError("pii_unavailable") from exc


# ── Main task ────────────────────────────────────────────────────────────────


def _is_retryable(exc: BaseException) -> bool:
    """Retry everything except a missing optional dependency.

    ``ImportError`` (PyMuPDF, python-docx, pytesseract/Pillow) is a
    deployment problem — retrying burns ``@with_retry`` attempts for a
    failure that cannot self-heal. It fails fast to the outer handler,
    which logs the failure site and re-raises. Corrupt input, download
    failures, and PII outages stay retryable (transient or self-healing).
    """
    return not isinstance(exc, ImportError)


@with_task_metrics
@with_retry(max_retries=2, base_delay_s=2.0, is_retryable=_is_retryable)
async def extract_blob_text(
    ctx: object,
    *,
    blob_id: str,
    user_id: str,
    project_id: str,
    episode_id: str,
    storage_key: str,
    mime_type: str,
    file_name: str = "",
    trace_id: str = "",
    enrichments: dict | None = None,
) -> None:
    """Extract text from a blob and store it on the episode_blobs record.

    Pipeline:
        1. Open DB session with RLS context.
        2. Load the blob record; skip if ``ENRICHMENT_BLOB_TEXT`` already set.
        3. Build the global blob client from Settings.
        4. Download blob bytes from S3 via ``BlobStorage``.
        5. Route to the appropriate text extractor by MIME type.
        6. Store extracted text on the ``episode_blobs`` record.
        7. Set ``ENRICHMENT_BLOB_TEXT`` bit on the episode.

    Args:
        ctx: ARQ worker context (``db_session_factory``, ``redis``).
        blob_id: UUID of the ``episode_blobs`` record (string).
        user_id: UUID of the owning user (string).
        project_id: UUID of the project (string).
        episode_id: UUID of the source episode (string).
        storage_key: S3 object key for the blob.
        mime_type: MIME type of the blob for dispatch.
        file_name: Original filename (for logging).
        trace_id: Request trace ID for end-to-end correlation.
        enrichments: Effective per-episode toggles (``None`` → all-ON).
            When ``blob_text`` is disabled the bit is mark-skipped.

    Raises:
        PIIUnavailableError: If the PII policy cannot be fetched or
            redaction fails — nothing is stored, nothing commits.
        ImportError: If an extractor library is missing — fails fast
            (``is_retryable`` excludes it), re-raised without retries.
        Exception: On corrupt input or download failure — re-raised
            after retry exhaustion.
    """
    if trace_id:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

    log = logger.bind(
        blob_id=blob_id,
        user_id=user_id,
        episode_id=episode_id,
        file_name=file_name,
        mime_type=mime_type,
    )
    log.info("extract_blob_text.start")

    # Lazy imports — ARQ workers run in a separate process.
    from core import rls
    from core.blob_storage import BlobStorage, BlobStorageConfig
    from core.config import get_settings, settings
    from core.db import get_async_session
    from repositories.episode_blob_repository import EpisodeBlobRepository
    from repositories.episode_repository import EpisodeRepository
    from repositories.project_repository import ProjectRepository

    # ── Resolve DB engine from ARQ context or create one ──────────────────
    engine = ctx.get("db_engine") if isinstance(ctx, dict) else None
    if engine is None:
        from core.db import init_db_engine

        engine = init_db_engine(str(settings.DATABASE_URL), pool_size=2, max_overflow=1)
        _own_engine = True
    else:
        _own_engine = False
    session_factory = ctx.get("db_session_factory") if isinstance(ctx, dict) else None
    if session_factory is None:
        session_factory = get_async_session(engine)

    try:
        async with session_factory() as db:
            # ── Set RLS context ─────────────────────────────────────────
            # The binding is remembered by the session, so the commit below
            # re-applies it — a bare ``set_config`` would leave the session
            # un-tenanted after that commit.
            await rls.set_user_context(db, user_id)

            blob_repo = EpisodeBlobRepository(db)
            episode_repo = EpisodeRepository(db)

            # ── Load the blob record ────────────────────────────────────
            blob = await blob_repo.get_by_id(UUID(blob_id))
            if blob is None:
                log.warning("extract_blob_text.blob_not_found")
                return

            # ── Idempotency check via episode enrichment_status bit ─────
            episode = await episode_repo.get_by_id(UUID(episode_id))
            if episode is None:
                log.warning("extract_blob_text.episode_not_found")
                return

            if episode.enrichment_status & ENRICHMENT_BLOB_TEXT:
                log.info("extract_blob_text.already_done")
                return

            # Archived-project guard: pause-and-resume. Fail-closed
            # (missing row counts as archived). Early return sets no
            # bits, so un-archiving resumes via reconcile or retry.
            if await ProjectRepository(db).is_archived(UUID(user_id), UUID(project_id)):
                log.info(
                    "extract_blob_text.project_archived_skipping",
                    episode_id=episode_id,
                    user_id=user_id,
                    project_id=project_id,
                )
                return

            # Enrichment toggle: mark-skipped when ``blob_text`` is
            # disabled (old jobs without the field default to all-ON).
            if isinstance(enrichments, dict) and not enrichments.get(
                "blob_text", True
            ):
                await episode_repo.apply_enrichment_bits(
                    UUID(episode_id), ENRICHMENT_BLOB_TEXT
                )
                await db.commit()
                log.info("extract_blob_text.enrichment_skipped")
                return

            # ── Build the global blob client ────────────────────────────
            # ⚠️ BREAKING: per-org blob storage was removed — one global
            # backend from Settings (fail-fast at startup, never per-org).
            blob_config = BlobStorageConfig.from_settings(get_settings())
            storage = BlobStorage(blob_config)

            # ── Download blob from S3 ───────────────────────────────────
            try:
                data = await storage.download(storage_key)
            except Exception:
                log.exception("extract_blob_text.download_failed")
                raise

            # ── Extract text based on MIME type ─────────────────────────
            # Raises on corrupt input or missing libs (retryable) — the
            # bit below is NOT set and nothing commits on that path.
            # Dispatch returns None only for a genuine no-text outcome
            # (unsupported MIME or empty payload).
            extracted_text = await _dispatch_extraction(mime_type, data)

            # ── PII redaction (fail-closed) — only when there is text ──
            # Raises PIIUnavailableError when the policy cannot be fetched
            # or redaction fails — update_extracted_text, the enrichment
            # bit, and the commit are all skipped and @with_retry retries.
            if extracted_text is not None:
                extracted_text = await _redact_extracted_text(
                    extracted_text,
                    user_id=user_id,
                    db=db,
                    trace_id=trace_id,
                    log=log,
                )

            if not extracted_text:
                # Genuine no-text outcome (unsupported MIME, empty payload):
                # not an error, but the enrichment bit stays unset so the
                # work remains visible to reconcile instead of being
                # silently marked complete.
                log.info(
                    "extract_blob_text.no_text_extracted",
                    mime_type=mime_type,
                )
                return

            await blob_repo.update_extracted_text(UUID(blob_id), extracted_text)
            log.info(
                "extract_blob_text.extraction_done",
                extracted_length=len(extracted_text),
            )

            # ── Set enrichment status bit atomically ────────────────────
            # Atomic OR (no read-modify-write on a stale snapshot) so a
            # concurrent worker's bits are never wiped; expire forces a
            # fresh re-read on next access.
            await episode_repo.apply_enrichment_bits(
                UUID(episode_id), ENRICHMENT_BLOB_TEXT
            )
            db.expire(episode, ["enrichment_status"])

            await db.commit()
            log.info("extract_blob_text.complete")

    except Exception:
        # Failure-site log: blob_id + mime identify the poison input without
        # opening the payload; storage_key locates the S3 object for replay.
        log.exception(
            "extract_blob_text.failed",
            blob_id=blob_id,
            user_id=user_id,
            episode_id=episode_id,
            mime_type=mime_type,
            file_name=file_name,
            storage_key=storage_key,
        )
        raise
    finally:
        if _own_engine:
            await engine.dispose()
