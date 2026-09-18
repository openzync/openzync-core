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
    (``ENRICHMENT_BLOB_TEXT``) on success — including unsupported MIME
    types (dispatch returns ``None``: nothing to extract, work complete).

Fail-closed: corrupt input, a missing PII policy, and redaction failures
propagate (no bit, no commit) so ``@with_retry`` retries. Missing
extractor libraries (``ImportError``) fail fast via ``is_retryable`` —
a deployment problem retries cannot heal. Nothing is ever stored
unredacted.
"""

from __future__ import annotations

import asyncio
import io
from uuid import UUID

import structlog
from sqlalchemy import text

from workers.tasks.base import ENRICHMENT_BLOB_TEXT, with_retry

logger = structlog.get_logger(__name__)


# ── MIME type routing ────────────────────────────────────────────────────────


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

    Offloads the synchronous ``pytesseract`` call to a thread pool via
    ``asyncio.to_thread()`` so the event loop is not blocked during CPU-bound
    OCR processing.

    Args:
        data: Raw image bytes (PNG, JPEG, WebP, TIFF, BMP, etc.).

    Returns:
        Extracted text, or ``None`` when the image holds no text.

    Raises:
        ImportError: If OCR dependencies are not installed.
        Exception: If OCR processing fails (retryable).
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        logger.warning(
            "extract_blob_text.ocr_not_available",
            extra={"detail": "Install pytesseract and Pillow for OCR support."},
        )
        raise

    image = Image.open(io.BytesIO(data))
    text = await asyncio.to_thread(
        pytesseract.image_to_string,
        image,
        lang="eng",
        config="--psm 3",  # Automatic page segmentation
    )
    result = text.strip()
    return result if result else None


# ── MIME type dispatch ───────────────────────────────────────────────────────


async def _dispatch_extraction(mime_type: str, data: bytes) -> str | None:
    """Route blob content to the right text extractor based on MIME type.

    Args:
        mime_type: The blob's MIME type string.
        data: Raw file bytes.

    Returns:
        Extracted text, or ``None`` when the type is unsupported (the
        caller then marks the work complete without storing text).

    Raises:
        Exception: Propagates extractor failures (corrupt input, missing
            libraries) so the caller retries instead of marking success.
    """
    if mime_type.startswith("text/"):
        # text/plain, text/csv, text/markdown, etc.
        return _extract_text_plain(data)

    if mime_type == "application/pdf":
        return _extract_pdf(data)

    if mime_type in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ):
        return _extract_docx(data)

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
    except Exception:
        logger.warning(
            "extract_blob_text.magic_failed",
            mime_type=mime_type,
        )

    return None


# ── Helper: resolve blob storage config ──────────────────────────────────────


async def _get_org_storage_config(
    org_id: str,
    bao_client: object | None,
) -> dict:
    """Fetch the org's blob storage config from OpenBao.

    Args:
        org_id: The organization UUID string.
        bao_client: An authenticated OpenBao client from the ARQ context.

    Returns:
        A dict suitable for ``BlobStorageConfig.from_org_config()``.
        Returns defaults if config cannot be fetched.
    """
    if bao_client is not None:
        from core.org_config import get_org_config

        try:
            org_cfg = await get_org_config(
                UUID(org_id),
                redis=None,
                bao_client=bao_client,
            )
            return org_cfg.to_blob_storage_config()
        except Exception:
            logger.warning(
                "extract_blob_text.org_config_fetch_failed",
                org_id=org_id,
                exc_info=True,
            )

    # Default config (matches docker-compose MinIO defaults)
    return {
        "backend": "s3",
        "endpoint_url": "http://minio:9000",
        "region": "auto",
        "access_key_id": "",
        "secret_access_key": "",
        "bucket_name": "openzync-blobs",
        "max_blob_size_mb": 50,
    }


# ── Helper: resolve org PII config (fail-closed) ───────────────────────────


async def _redact_extracted_text(
    extracted_text: str,
    *,
    org_id: str,
    bao_client: object | None,
    trace_id: str,
    log: structlog.typing.FilteringBoundLogger,
) -> str:
    """Redact PII from extracted blob text, fail-closed.

    Fetches the org PII policy from OpenBao (no quotas fallback).  A
    missing client, a fetch failure, or a redaction failure raises
    ``PIIUnavailableError`` — the caller then skips
    ``update_extracted_text``, skips the ``ENRICHMENT_BLOB_TEXT`` bit, and
    lets ``@with_retry`` retry.  A block-mode ``ValidationError``
    downgrades to a single mask pass (preserved semantic).

    Args:
        extracted_text: Raw text from the MIME extractor.
        org_id: The organization UUID string (log context only).
        bao_client: An authenticated OpenBao client from the ARQ context.
        trace_id: Request trace ID for end-to-end correlation.
        log: Bound structlog logger.

    Returns:
        The redacted text (unchanged when mode is ``off`` or clean).

    Raises:
        PIIUnavailableError: If the policy cannot be fetched or redaction
            fails — never store unredacted text.
    """
    from core.exceptions import PIIUnavailableError, ValidationError

    if bao_client is None:
        logger.error(
            "pii.fail_closed",
            org_id=org_id,
            trace_id=trace_id,
            reason="no OpenBao client in worker context",
        )
        raise PIIUnavailableError("pii_unavailable")

    try:
        from core.org_config import get_org_config

        org_cfg = await get_org_config(
            UUID(org_id),
            redis=None,
            bao_client=bao_client,  # type: ignore[arg-type]
        )
    except Exception as exc:
        logger.error(
            "pii.fail_closed",
            org_id=org_id,
            trace_id=trace_id,
            exc_info=True,
        )
        raise PIIUnavailableError("pii_unavailable") from exc

    if org_cfg is None or org_cfg.pii_mode is None:
        if org_cfg is None:
            logger.error(
                "pii.fail_closed",
                org_id=org_id,
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
            org_id=org_id,
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


@with_retry(max_retries=2, base_delay_s=2.0, is_retryable=_is_retryable)
async def extract_blob_text(
    ctx: object,
    *,
    blob_id: str,
    org_id: str,
    project_id: str,
    episode_id: str,
    storage_key: str,
    mime_type: str,
    file_name: str = "",
    trace_id: str = "",
) -> None:
    """Extract text from a blob and store it on the episode_blobs record.

    Pipeline:
        1. Open DB session with RLS context.
        2. Load the blob record; skip if ``ENRICHMENT_BLOB_TEXT`` already set.
        3. Fetch org blob storage config from OpenBao.
        4. Download blob bytes from S3 via ``BlobStorage``.
        5. Route to the appropriate text extractor by MIME type.
        6. Store extracted text on the ``episode_blobs`` record.
        7. Set ``ENRICHMENT_BLOB_TEXT`` bit on the episode.

    Args:
        ctx: ARQ worker context (``db_session_factory``, ``redis``,
            ``openbao_client``).
        blob_id: UUID of the ``episode_blobs`` record (string).
        org_id: UUID of the owning organization (string).
        project_id: UUID of the project (string).
        episode_id: UUID of the source episode (string).
        storage_key: S3 object key for the blob.
        mime_type: MIME type of the blob for dispatch.
        file_name: Original filename (for logging).
        trace_id: Request trace ID for end-to-end correlation.

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
        org_id=org_id,
        episode_id=episode_id,
        file_name=file_name,
        mime_type=mime_type,
    )
    log.info("extract_blob_text.start")

    # Lazy imports — ARQ workers run in a separate process.
    from core.blob_storage import BlobStorage, BlobStorageConfig
    from core.config import settings
    from core.db import get_async_session
    from repositories.episode_blob_repository import EpisodeBlobRepository
    from repositories.episode_repository import EpisodeRepository

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

    bao_client = ctx.get("openbao_client") if isinstance(ctx, dict) else None

    try:
        async with session_factory() as db:
            # ── Set RLS context ─────────────────────────────────────────
            await db.execute(
                text("SELECT set_config('app.org_id', :oid, true)"),
                {"oid": org_id},
            )

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

            # ── Fetch org storage config ────────────────────────────────
            storage_config = await _get_org_storage_config(org_id, bao_client)
            blob_config = BlobStorageConfig.from_org_config(storage_config)
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
            # Dispatch returns None only for unsupported types, which keeps
            # the set-bit path (nothing to extract, work is complete).
            extracted_text = await _dispatch_extraction(mime_type, data)

            # ── PII redaction (fail-closed) ─────────────────────────────
            # Raises PIIUnavailableError when the policy cannot be fetched
            # or redaction fails — update_extracted_text, the enrichment
            # bit, and the commit are all skipped and @with_retry retries.
            if extracted_text is not None:
                extracted_text = await _redact_extracted_text(
                    extracted_text,
                    org_id=org_id,
                    bao_client=bao_client,
                    trace_id=trace_id,
                    log=log,
                )

            if extracted_text:
                await blob_repo.update_extracted_text(UUID(blob_id), extracted_text)
                log.info(
                    "extract_blob_text.extraction_done",
                    extracted_length=len(extracted_text),
                )
            else:
                log.info(
                    "extract_blob_text.no_text_extracted",
                    mime_type=mime_type,
                )

            # ── Set enrichment status bit via ORM ───────────────────────
            episode.enrichment_status |= ENRICHMENT_BLOB_TEXT
            await db.flush()

            await db.commit()
            log.info("extract_blob_text.complete")

    except Exception:
        # Failure-site log: blob_id + mime identify the poison input without
        # opening the payload; storage_key locates the S3 object for replay.
        log.exception(
            "extract_blob_text.failed",
            blob_id=blob_id,
            org_id=org_id,
            episode_id=episode_id,
            mime_type=mime_type,
            file_name=file_name,
            storage_key=storage_key,
        )
        raise
    finally:
        if _own_engine:
            await engine.dispose()
