"""Unit tests for ``extract_blob_text`` worker task.

Tests cover:
- ``_extract_pdf`` helper (PyMuPDF extraction, missing dep, errors, empty result)
- ``_extract_docx`` helper (python-docx extraction, missing dep, errors, empty paragraphs)
- ``_extract_text_plain`` helper (UTF-8 decode, replacement chars, empty)
- ``_extract_image_ocr`` helper (pytesseract OCR, missing deps, errors, empty result)
- ``_dispatch_extraction`` MIME routing (text/*, PDF, DOCX, image/*, magic fallback, unknown)
- ``_get_org_storage_config`` (OpenBao path, fetch failure, default fallback)
- ``extract_blob_text`` main decorated task (happy path, idempotency, blob/episode not
  found, download failures, no text extracted, ctx resource resolution, engine lifecycle)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from workers.tasks.base import ENRICHMENT_BLOB_TEXT

_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())
_EPISODE_ID = str(uuid4())
_BLOB_ID = str(uuid4())
_STORAGE_KEY = f"blobs/{_BLOB_ID}"
_MIME_TYPE = "application/pdf"
_FILE_NAME = "test.pdf"
_TRACE_ID = "test-trace-001"

# ═══════════════════════════════════════════════════════════════════════════════
# _extract_pdf
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestExtractPdf:
    """``_extract_pdf`` — PyMuPDF-based PDF text extraction."""

    def _mock_fitz(self, pages: list[str] | None = None) -> MagicMock:
        """Create a mock ``fitz`` module with the given page texts.

        Args:
            pages: List of per-page text strings.  Defaults to one page
                with ``"Page content"``.

        Returns:
            A MagicMock that acts as the ``fitz`` module.
        """
        if pages is None:
            pages = ["Page content"]
        mock_fitz = MagicMock()
        mock_pages = []
        for text in pages:
            p = MagicMock()
            p.get_text.return_value = text
            mock_pages.append(p)

        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter(mock_pages))
        mock_doc.close = MagicMock()
        mock_fitz.open.return_value = mock_doc
        return mock_fitz

    def test_extracts_text_from_single_page(self) -> None:
        """PDF with one page returns its text content."""
        from workers.tasks.extract_blob_text import _extract_pdf

        mock_fitz = self._mock_fitz(["Hello world"])
        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            result = _extract_pdf(b"dummy pdf bytes")

        assert result == "Hello world"
        mock_fitz.open.assert_called_once_with(
            stream=b"dummy pdf bytes", filetype="pdf"
        )

    def test_joins_multiple_pages(self) -> None:
        """PDF with multiple pages joins them with newlines."""
        from workers.tasks.extract_blob_text import _extract_pdf

        mock_fitz = self._mock_fitz(["Page 1", "Page 2", "Page 3"])
        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            result = _extract_pdf(b"multi-page pdf")

        assert result == "Page 1\nPage 2\nPage 3"

    def test_returns_none_when_fitz_not_installed(self) -> None:
        """Missing PyMuPDF dependency returns None."""
        from workers.tasks.extract_blob_text import _extract_pdf

        with patch.dict("sys.modules", {"fitz": None}):
            result = _extract_pdf(b"data")

        assert result is None

    def test_returns_none_on_extraction_failure(self) -> None:
        """Exception during PDF open/read returns None."""
        from workers.tasks.extract_blob_text import _extract_pdf

        mock_fitz = MagicMock()
        mock_fitz.open.side_effect = RuntimeError("Corrupt PDF")
        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            result = _extract_pdf(b"corrupt data")

        assert result is None

    def test_returns_none_for_empty_result(self) -> None:
        """Empty text after stripping returns None."""
        from workers.tasks.extract_blob_text import _extract_pdf

        mock_fitz = self._mock_fitz(["  \n  "])
        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            result = _extract_pdf(b"empty pdf")

        assert result is None

    def test_closes_document(self) -> None:
        """``doc.close()`` is called after extraction."""
        from workers.tasks.extract_blob_text import _extract_pdf

        mock_fitz = self._mock_fitz(["content"])
        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            _extract_pdf(b"data")

        mock_fitz.open.return_value.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# _extract_docx
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestExtractDocx:
    """``_extract_docx`` — python-docx-based DOCX text extraction."""

    def _mock_docx(self, paragraphs: list[str]) -> MagicMock:
        """Create a mock ``docx`` module with the given paragraph texts.

        Args:
            paragraphs: List of paragraph text strings.

        Returns:
            A MagicMock that acts as the ``docx`` module.
        """
        mock_docx = MagicMock()
        mock_paragraphs = []
        for text in paragraphs:
            p = MagicMock()
            p.text = text
            mock_paragraphs.append(p)
        mock_doc = MagicMock()
        mock_doc.paragraphs = mock_paragraphs
        mock_docx.Document.return_value = mock_doc
        return mock_docx

    def test_extracts_text_from_paragraphs(self) -> None:
        """DOCX paragraphs are joined with newlines."""
        from workers.tasks.extract_blob_text import _extract_docx

        mock_docx = self._mock_docx(["First para", "Second para"])
        with patch.dict("sys.modules", {"docx": mock_docx}):
            result = _extract_docx(b"dummy docx")

        assert result == "First para\nSecond para"

    def test_skips_empty_paragraphs(self) -> None:
        """Whitespace-only paragraphs are excluded from output."""
        from workers.tasks.extract_blob_text import _extract_docx

        mock_docx = self._mock_docx(["  ", "Real content", ""])
        with patch.dict("sys.modules", {"docx": mock_docx}):
            result = _extract_docx(b"docx with blanks")

        assert result == "Real content"

    def test_returns_none_when_docx_not_installed(self) -> None:
        """Missing python-docx returns None."""
        from workers.tasks.extract_blob_text import _extract_docx

        with patch.dict("sys.modules", {"docx": None}):
            result = _extract_docx(b"data")

        assert result is None

    def test_returns_none_on_extraction_failure(self) -> None:
        """Exception during DOCX parsing returns None."""
        from workers.tasks.extract_blob_text import _extract_docx

        mock_docx = MagicMock()
        mock_docx.Document.side_effect = ValueError("Corrupt DOCX")
        with patch.dict("sys.modules", {"docx": mock_docx}):
            result = _extract_docx(b"corrupt data")

        assert result is None

    def test_returns_none_when_all_paragraphs_empty(self) -> None:
        """All-empty paragraphs result in None."""
        from workers.tasks.extract_blob_text import _extract_docx

        mock_docx = self._mock_docx(["", "  ", "\t"])
        with patch.dict("sys.modules", {"docx": mock_docx}):
            result = _extract_docx(b"empty docx")

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# _extract_text_plain
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestExtractTextPlain:
    """``_extract_text_plain`` — raw UTF-8 text decoding."""

    def test_decodes_utf8(self) -> None:
        """Valid UTF-8 bytes produce the decoded string."""
        from workers.tasks.extract_blob_text import _extract_text_plain

        result = _extract_text_plain("Hello, world!".encode("utf-8"))
        assert result == "Hello, world!"

    def test_replaces_invalid_bytes(self) -> None:
        """Invalid UTF-8 sequences are replaced, not rejected."""
        from workers.tasks.extract_blob_text import _extract_text_plain

        result = _extract_text_plain(b"Hello\xffworld")
        assert result is not None
        assert "\ufffd" in result  # U+FFFD replacement character

    def test_returns_none_for_empty_bytes(self) -> None:
        """Empty input returns None."""
        from workers.tasks.extract_blob_text import _extract_text_plain

        assert _extract_text_plain(b"") is None

    def test_returns_none_for_whitespace_only(self) -> None:
        """Whitespace-only input returns None."""
        from workers.tasks.extract_blob_text import _extract_text_plain

        assert _extract_text_plain(b"   ") is None
        assert _extract_text_plain(b"\n\t\r") is None

    def test_no_exception_for_binary_data(self) -> None:
        """Binary data is never rejected — errors='replace' covers all bytes."""
        from workers.tasks.extract_blob_text import _extract_text_plain

        # With errors='replace', even invalid UTF-8 bytes produce output
        # rather than raising. The function's except path guards against
        # non-decode exceptions (e.g. MemoryError).
        result = _extract_text_plain(b"\xff\xfe\x00\x01binary data")
        assert result is not None
        assert "\ufffd" in result  # replacement character present


# ═══════════════════════════════════════════════════════════════════════════════
# _extract_image_ocr
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestExtractImageOcr:
    """``_extract_image_ocr`` — Tesseract OCR via pytesseract."""

    @pytest.mark.asyncio
    async def test_extracts_text_via_ocr(self) -> None:
        """Image with text returns OCR result, stripped."""
        from workers.tasks.extract_blob_text import _extract_image_ocr

        mock_pyt = MagicMock()
        mock_pil = MagicMock()

        with (
            patch.dict("sys.modules", {"pytesseract": mock_pyt, "PIL": mock_pil}),
            patch("asyncio.to_thread", AsyncMock(return_value="OCR text\n")),
        ):
            result = await _extract_image_ocr(b"image bytes")

        assert result == "OCR text"

    @pytest.mark.asyncio
    async def test_returns_none_when_deps_missing(self) -> None:
        """Missing pytesseract / Pillow returns None."""
        from workers.tasks.extract_blob_text import _extract_image_ocr

        with patch.dict("sys.modules", {"pytesseract": None, "PIL": None}):
            result = await _extract_image_ocr(b"image bytes")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_ocr_failure(self) -> None:
        """Exception during OCR returns None."""
        from workers.tasks.extract_blob_text import _extract_image_ocr

        mock_pyt = MagicMock()
        mock_pil = MagicMock()

        with (
            patch.dict("sys.modules", {"pytesseract": mock_pyt, "PIL": mock_pil}),
            patch("asyncio.to_thread", AsyncMock(side_effect=RuntimeError("OCR failed"))),
        ):
            result = await _extract_image_ocr(b"image bytes")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_result(self) -> None:
        """OCR returning whitespace-only text returns None."""
        from workers.tasks.extract_blob_text import _extract_image_ocr

        mock_pyt = MagicMock()
        mock_pil = MagicMock()

        with (
            patch.dict("sys.modules", {"pytesseract": mock_pyt, "PIL": mock_pil}),
            patch("asyncio.to_thread", AsyncMock(return_value="  \n")),
        ):
            result = await _extract_image_ocr(b"image bytes")

        assert result is None

    @pytest.mark.asyncio
    async def test_image_opened_from_bytes(self) -> None:
        """PIL Image is opened from a BytesIO stream of the raw data."""
        from workers.tasks.extract_blob_text import _extract_image_ocr

        mock_pyt = MagicMock()
        mock_pil = MagicMock()
        mock_img = MagicMock()
        mock_pil.Image.open.return_value = mock_img

        with (
            patch.dict("sys.modules", {"pytesseract": mock_pyt, "PIL": mock_pil}),
            patch("asyncio.to_thread", AsyncMock(return_value="text")),
        ):
            await _extract_image_ocr(b"\x89PNG data")

        # Image.open was called with a BytesIO wrapper
        mock_pil.Image.open.assert_called_once()
        call_arg = mock_pil.Image.open.call_args[0][0]
        assert isinstance(call_arg, type(  # io.BytesIO
            __import__("io").BytesIO()
        )) or hasattr(call_arg, "read")


# ═══════════════════════════════════════════════════════════════════════════════
# _dispatch_extraction
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestDispatchExtraction:
    """``_dispatch_extraction`` — MIME-type-based extraction routing."""

    @pytest.mark.asyncio
    async def test_routes_text_slash(self) -> None:
        """``text/*`` MIME types go to ``_extract_text_plain``."""
        from workers.tasks.extract_blob_text import _dispatch_extraction

        with patch(
            "workers.tasks.extract_blob_text._extract_text_plain", return_value="plain"
        ):
            result = await _dispatch_extraction("text/plain", b"data")
        assert result == "plain"

    @pytest.mark.asyncio
    async def test_routes_text_csv(self) -> None:
        """``text/csv`` also goes to ``_extract_text_plain``."""
        from workers.tasks.extract_blob_text import _dispatch_extraction

        with patch(
            "workers.tasks.extract_blob_text._extract_text_plain", return_value="csv"
        ):
            result = await _dispatch_extraction("text/csv", b"a,b,c")
        assert result == "csv"

    @pytest.mark.asyncio
    async def test_routes_pdf(self) -> None:
        """``application/pdf`` routes to ``_extract_pdf``."""
        from workers.tasks.extract_blob_text import _dispatch_extraction

        with patch("workers.tasks.extract_blob_text._extract_pdf", return_value="pdf"):
            result = await _dispatch_extraction("application/pdf", b"%PDF")
        assert result == "pdf"

    @pytest.mark.asyncio
    async def test_routes_docx(self) -> None:
        """DOCX MIME type routes to ``_extract_docx``."""
        from workers.tasks.extract_blob_text import _dispatch_extraction

        with patch(
            "workers.tasks.extract_blob_text._extract_docx", return_value="docx"
        ):
            result = await _dispatch_extraction(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                b"PK",
            )
        assert result == "docx"

    @pytest.mark.asyncio
    async def test_routes_msword(self) -> None:
        """``application/msword`` routes to ``_extract_docx``."""
        from workers.tasks.extract_blob_text import _dispatch_extraction

        with patch(
            "workers.tasks.extract_blob_text._extract_docx", return_value="word"
        ):
            result = await _dispatch_extraction("application/msword", b"word")
        assert result == "word"

    @pytest.mark.asyncio
    async def test_routes_image_to_ocr(self) -> None:
        """``image/*`` routes to ``_extract_image_ocr``."""
        from workers.tasks.extract_blob_text import _dispatch_extraction

        with patch(
            "workers.tasks.extract_blob_text._extract_image_ocr",
            AsyncMock(return_value="ocr"),
        ):
            result = await _dispatch_extraction("image/png", b"PNG")
        assert result == "ocr"

    @pytest.mark.asyncio
    async def test_image_jpeg(self) -> None:
        """``image/jpeg`` also routes to OCR."""
        from workers.tasks.extract_blob_text import _dispatch_extraction

        with patch(
            "workers.tasks.extract_blob_text._extract_image_ocr",
            AsyncMock(return_value="jpeg ocr"),
        ):
            result = await _dispatch_extraction("image/jpeg", b"JPEG")
        assert result == "jpeg ocr"

    @pytest.mark.asyncio
    async def test_unknown_type_returns_none(self) -> None:
        """Unknown MIME type without magic returns None."""
        from workers.tasks.extract_blob_text import _dispatch_extraction

        # magic is installed in CI, so patch it to raise ImportError
        with patch.dict("sys.modules", {"magic": None}):
            result = await _dispatch_extraction("application/octet-stream", b"data")

        assert result is None

    # ── Magic fallback ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_magic_fallback_different_type(self) -> None:
        """Magic fallback re-dispatches when detected type differs."""
        from workers.tasks.extract_blob_text import _dispatch_extraction

        mock_magic = MagicMock()
        mock_magic.from_buffer.return_value = "text/plain"

        with (
            patch.dict("sys.modules", {"magic": mock_magic}),
            patch(
                "workers.tasks.extract_blob_text._extract_text_plain",
                return_value="magic text",
            ),
        ):
            result = await _dispatch_extraction("application/octet-stream", b"data")

        assert result == "magic text"
        mock_magic.from_buffer.assert_called_once_with(b"data", mime=True)

    @pytest.mark.asyncio
    async def test_magic_same_type_skips_recursion(self) -> None:
        """Magic detecting the same MIME type does not recurse."""
        from workers.tasks.extract_blob_text import _dispatch_extraction

        mock_magic = MagicMock()
        mock_magic.from_buffer.return_value = "application/octet-stream"

        with patch.dict("sys.modules", {"magic": mock_magic}):
            result = await _dispatch_extraction("application/octet-stream", b"data")

        assert result is None

    @pytest.mark.asyncio
    async def test_magic_not_installed_skipped(self) -> None:
        """Missing python-magic is silently ignored."""
        from workers.tasks.extract_blob_text import _dispatch_extraction

        with patch.dict("sys.modules", {"magic": None}):
            result = await _dispatch_extraction("application/octet-stream", b"data")

        assert result is None

    @pytest.mark.asyncio
    async def test_magic_exception_logged_and_continues(self) -> None:
        """Exception in magic.from_buffer is caught, logged, returns None."""
        from workers.tasks.extract_blob_text import _dispatch_extraction

        mock_magic = MagicMock()
        mock_magic.from_buffer.side_effect = ValueError("magic error")

        with patch.dict("sys.modules", {"magic": mock_magic}):
            result = await _dispatch_extraction("application/octet-stream", b"data")

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# _get_org_storage_config
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestGetOrgStorageConfig:
    """``_get_org_storage_config`` — org blob storage config resolution."""

    @pytest.mark.asyncio
    async def test_uses_bao_client_when_provided(self) -> None:
        """OpenBao client present → fetches config via ``get_org_config``."""
        from workers.tasks.extract_blob_text import _get_org_storage_config

        mock_bao = MagicMock()
        mock_org_cfg = MagicMock()
        mock_org_cfg.to_blob_storage_config.return_value = {
            "backend": "s3",
            "bucket_name": "custom-bucket",
        }

        with patch(
            "core.org_config.get_org_config", AsyncMock(return_value=mock_org_cfg)
        ):
            result = await _get_org_storage_config(_ORG_ID, mock_bao)

        assert result == {"backend": "s3", "bucket_name": "custom-bucket"}

    @pytest.mark.asyncio
    async def test_returns_defaults_on_config_fetch_failure(self) -> None:
        """OpenBao fetch exception → default MinIO config returned."""
        from workers.tasks.extract_blob_text import _get_org_storage_config

        mock_bao = MagicMock()

        with patch(
            "core.org_config.get_org_config",
            AsyncMock(side_effect=RuntimeError("Bao down")),
        ):
            result = await _get_org_storage_config(_ORG_ID, mock_bao)

        assert result == {
            "backend": "s3",
            "endpoint_url": "http://minio:9000",
            "region": "auto",
            "access_key_id": "",
            "secret_access_key": "",
            "bucket_name": "openzync-blobs",
            "max_blob_size_mb": 50,
        }

    @pytest.mark.asyncio
    async def test_returns_defaults_when_bao_client_none(self) -> None:
        """No OpenBao client → default config returned without any fetch."""
        from workers.tasks.extract_blob_text import _get_org_storage_config

        result = await _get_org_storage_config(_ORG_ID, None)

        assert result["backend"] == "s3"
        assert result["bucket_name"] == "openzync-blobs"
        assert result["endpoint_url"] == "http://minio:9000"


# ═══════════════════════════════════════════════════════════════════════════════
# extract_blob_text — main task
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestExtractBlobText:
    """``extract_blob_text`` — main entry point decorated with ``@with_retry``."""

    # ═══════════════════════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════════════════════

    def _make_session_factory(self, db: AsyncMock) -> MagicMock:
        """Create a mock session factory wrapping the given db."""
        factory = MagicMock()
        factory.return_value = db
        return factory

    def _make_ctx(
        self,
        db: AsyncMock,
        include_bao: bool = True,
    ) -> dict:
        """Create a minimal ARQ worker context dict.

        Args:
            db: Mock DB session.
            include_bao: Whether to include an ``openbao_client`` key.

        Returns:
            A dict suitable as ``ctx`` for ``extract_blob_text``.
        """
        ctx: dict = {
            "db_engine": MagicMock(),
            "db_session_factory": self._make_session_factory(db),
        }
        if include_bao:
            ctx["openbao_client"] = MagicMock()
        return ctx

    def _make_db(
        self,
        enrichment_status: int = 0,
    ) -> AsyncMock:
        """Create a mock DB session with a configurable episode.

        Args:
            enrichment_status: The episode's ``enrichment_status`` bitmask.

        Returns:
            An AsyncMock configured as a DB session.
        """
        mock_db = AsyncMock()
        mock_db.__aenter__.return_value = mock_db
        mock_db.__aexit__.return_value = None

        # Episode row
        mock_episode = MagicMock()
        mock_episode.enrichment_status = enrichment_status

        # SQL execution — the first call is RLS (``set_config``), which just
        # needs to not error.  Repo queries go through the mock repo, not
        # ``mock_db.execute``, so a simple MagicMock is fine.
        mock_db.execute.return_value = MagicMock()

        # `async with db:` works (same mock returned)
        return mock_db

    # ═══════════════════════════════════════════════════════════════════════
    # Idempotency
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_skips_when_blob_text_already_done(self) -> None:
        """ENRICHMENT_BLOB_TEXT bit set → early return, no extraction."""
        from workers.tasks.extract_blob_text import extract_blob_text

        db = self._make_db(enrichment_status=ENRICHMENT_BLOB_TEXT)
        ctx = self._make_ctx(db)

        with (
            patch("workers.tasks.extract_blob_text.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
        ):
            mock_blob = MagicMock()
            mock_blob.id = UUID(_BLOB_ID)
            mock_blob_repo = AsyncMock()
            mock_blob_repo.get_by_id.return_value = mock_blob
            mock_repo_cls.return_value = mock_blob_repo

            mock_ep_repo = AsyncMock()
            mock_episode = MagicMock()
            mock_episode.enrichment_status = ENRICHMENT_BLOB_TEXT
            mock_ep_repo.get_by_id.return_value = mock_episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            await extract_blob_text(
                ctx=ctx,
                blob_id=_BLOB_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                episode_id=_EPISODE_ID,
                storage_key=_STORAGE_KEY,
                mime_type=_MIME_TYPE,
                file_name=_FILE_NAME,
            )

            # No download or extract should happen
            mock_blob_repo.update_extracted_text.assert_not_called()
            db.commit.assert_not_called()

    # ═══════════════════════════════════════════════════════════════════════
    # Blob not found
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_skips_when_blob_not_found(self) -> None:
        """Blob row missing → early return without extraction."""
        from workers.tasks.extract_blob_text import extract_blob_text

        db = self._make_db()
        ctx = self._make_ctx(db)

        with (
            patch("workers.tasks.extract_blob_text.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
        ):
            mock_blob_repo = AsyncMock()
            mock_blob_repo.get_by_id.return_value = None  # blob not found
            mock_repo_cls.return_value = mock_blob_repo

            await extract_blob_text(
                ctx=ctx,
                blob_id=_BLOB_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                episode_id=_EPISODE_ID,
                storage_key=_STORAGE_KEY,
                mime_type=_MIME_TYPE,
            )

            db.commit.assert_not_called()

    # ═══════════════════════════════════════════════════════════════════════
    # Episode not found
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_skips_when_episode_not_found(self) -> None:
        """Episode row missing → early return without extraction."""
        from workers.tasks.extract_blob_text import extract_blob_text

        db = self._make_db()
        ctx = self._make_ctx(db)

        with (
            patch("workers.tasks.extract_blob_text.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
        ):
            mock_blob_repo = AsyncMock()
            mock_blob_repo.get_by_id.return_value = MagicMock(id=UUID(_BLOB_ID))
            mock_repo_cls.return_value = mock_blob_repo

            mock_ep_repo = AsyncMock()
            mock_ep_repo.get_by_id.return_value = None  # episode not found
            mock_ep_repo_cls.return_value = mock_ep_repo

            await extract_blob_text(
                ctx=ctx,
                blob_id=_BLOB_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                episode_id=_EPISODE_ID,
                storage_key=_STORAGE_KEY,
                mime_type=_MIME_TYPE,
            )

            db.commit.assert_not_called()

    # ═══════════════════════════════════════════════════════════════════════
    # Happy path
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_happy_path_full_pipeline(self) -> None:
        """Complete flow: download → extract → store → set bit → commit."""
        from workers.tasks.extract_blob_text import extract_blob_text

        db = self._make_db()
        ctx = self._make_ctx(db)

        with (
            patch("workers.tasks.extract_blob_text.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("core.blob_storage.BlobStorage") as mock_storage_cls,
            patch("core.blob_storage.BlobStorageConfig") as mock_cfg_cls,
            patch(
                "workers.tasks.extract_blob_text._get_org_storage_config",
                AsyncMock(return_value={"backend": "s3", "bucket_name": "b"}),
            ),
            patch(
                "workers.tasks.extract_blob_text._dispatch_extraction",
                AsyncMock(return_value="extracted text content"),
            ),
        ):
            # Repos
            mock_blob_repo = AsyncMock()
            mock_blob = MagicMock(id=UUID(_BLOB_ID))
            mock_blob_repo.get_by_id.return_value = mock_blob
            mock_blob_repo.update_extracted_text = AsyncMock()
            mock_repo_cls.return_value = mock_blob_repo

            mock_ep_repo = AsyncMock()
            mock_episode = MagicMock()
            mock_episode.enrichment_status = 0
            mock_ep_repo.get_by_id.return_value = mock_episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            # Storage
            mock_storage = AsyncMock()
            mock_storage.download = AsyncMock(return_value=b"pdf file bytes")
            mock_storage_cls.return_value = mock_storage

            mock_cfg_cls.from_org_config.return_value = MagicMock()

            await extract_blob_text(
                ctx=ctx,
                blob_id=_BLOB_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                episode_id=_EPISODE_ID,
                storage_key=_STORAGE_KEY,
                mime_type=_MIME_TYPE,
                file_name=_FILE_NAME,
                trace_id=_TRACE_ID,
            )

            # Assertions
            mock_storage.download.assert_awaited_once_with(_STORAGE_KEY)
            mock_blob_repo.update_extracted_text.assert_awaited_once_with(
                UUID(_BLOB_ID), "extracted text content"
            )
            assert mock_episode.enrichment_status & ENRICHMENT_BLOB_TEXT
            db.flush.assert_awaited_once()
            db.commit.assert_awaited_once()

    # ═══════════════════════════════════════════════════════════════════════
    # No text extracted
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_no_text_extracted_still_sets_bit(self) -> None:
        """When no text is extracted, the enrichment bit is still set."""
        from workers.tasks.extract_blob_text import extract_blob_text

        db = self._make_db()
        ctx = self._make_ctx(db)

        with (
            patch("workers.tasks.extract_blob_text.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("core.blob_storage.BlobStorage") as mock_storage_cls,
            patch("core.blob_storage.BlobStorageConfig"),
            patch(
                "workers.tasks.extract_blob_text._get_org_storage_config",
                AsyncMock(return_value={"backend": "s3"}),
            ),
            patch(
                "workers.tasks.extract_blob_text._dispatch_extraction",
                AsyncMock(return_value=None),  # no text extracted
            ),
        ):
            mock_blob_repo = AsyncMock()
            mock_blob_repo.get_by_id.return_value = MagicMock(id=UUID(_BLOB_ID))
            mock_repo_cls.return_value = mock_blob_repo

            mock_ep_repo = AsyncMock()
            mock_episode = MagicMock()
            mock_episode.enrichment_status = 0
            mock_ep_repo.get_by_id.return_value = mock_episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            mock_storage = AsyncMock()
            mock_storage.download = AsyncMock(return_value=b"data")
            mock_storage_cls.return_value = mock_storage

            await extract_blob_text(
                ctx=ctx,
                blob_id=_BLOB_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                episode_id=_EPISODE_ID,
                storage_key=_STORAGE_KEY,
                mime_type=_MIME_TYPE,
            )

            # No text was stored, but bit was still set
            mock_blob_repo.update_extracted_text.assert_not_called()
            assert mock_episode.enrichment_status & ENRICHMENT_BLOB_TEXT
            db.commit.assert_awaited_once()

    # ═══════════════════════════════════════════════════════════════════════
    # Download failure
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_download_failure_re_raises(self) -> None:
        """S3 download failure propagates (for ARQ retry)."""
        from workers.tasks.extract_blob_text import extract_blob_text

        db = self._make_db()
        ctx = self._make_ctx(db)

        with (
            patch("workers.tasks.extract_blob_text.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("core.blob_storage.BlobStorage") as mock_storage_cls,
            patch("core.blob_storage.BlobStorageConfig"),
            patch(
                "workers.tasks.extract_blob_text._get_org_storage_config",
                AsyncMock(return_value={"backend": "s3"}),
            ),
        ):
            mock_blob_repo = AsyncMock()
            mock_blob_repo.get_by_id.return_value = MagicMock(id=UUID(_BLOB_ID))
            mock_repo_cls.return_value = mock_blob_repo

            mock_ep_repo = AsyncMock()
            mock_episode = MagicMock()
            mock_episode.enrichment_status = 0
            mock_ep_repo.get_by_id.return_value = mock_episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            mock_storage = AsyncMock()
            mock_storage.download = AsyncMock(side_effect=ConnectionError("S3 down"))
            mock_storage_cls.return_value = mock_storage

            with pytest.raises(ConnectionError, match="S3 down"):
                await extract_blob_text(
                    ctx=ctx,
                    blob_id=_BLOB_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    episode_id=_EPISODE_ID,
                    storage_key=_STORAGE_KEY,
                    mime_type=_MIME_TYPE,
                )

            # Commit should NOT have been called (exception before commit)
            db.commit.assert_not_called()

    # ═══════════════════════════════════════════════════════════════════════
    # Trace ID
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_trace_id_bound_to_log_context(self) -> None:
        """Trace ID is propagated to structlog context vars."""
        db = self._make_db(enrichment_status=ENRICHMENT_BLOB_TEXT)  # skip early
        ctx = self._make_ctx(db)

        with (
            patch("workers.tasks.extract_blob_text.with_retry", lambda **kw: lambda f: f),
            patch("workers.tasks.extract_blob_text.structlog") as mock_structlog,
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
        ):
            from workers.tasks.extract_blob_text import extract_blob_text

            mock_blob_repo = AsyncMock()
            mock_blob_repo.get_by_id.return_value = MagicMock(id=UUID(_BLOB_ID))
            mock_repo_cls.return_value = mock_blob_repo

            mock_ep_repo = AsyncMock()
            mock_episode = MagicMock()
            mock_episode.enrichment_status = ENRICHMENT_BLOB_TEXT
            mock_ep_repo.get_by_id.return_value = mock_episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            await extract_blob_text(
                ctx=ctx,
                blob_id=_BLOB_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                episode_id=_EPISODE_ID,
                storage_key=_STORAGE_KEY,
                mime_type=_MIME_TYPE,
                trace_id="my-trace",
            )

            mock_structlog.contextvars.bind_contextvars.assert_called_with(
                trace_id="my-trace"
            )

    # ═══════════════════════════════════════════════════════════════════════
    # Missing db_engine in ctx
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_missing_db_engine_creates_own_and_disposes(self) -> None:
        """When ctx has no ``db_engine``, one is created and disposed."""
        from workers.tasks.extract_blob_text import extract_blob_text

        db = self._make_db(enrichment_status=ENRICHMENT_BLOB_TEXT)  # skip early
        session_factory = self._make_session_factory(db)

        with (
            patch("workers.tasks.extract_blob_text.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("core.db.init_db_engine") as mock_init_engine,
            patch("core.db.get_async_session") as mock_get_session,
        ):
            mock_engine = MagicMock()
            mock_engine.dispose = AsyncMock()
            mock_init_engine.return_value = mock_engine
            mock_get_session.return_value = session_factory

            mock_blob_repo = AsyncMock()
            mock_blob_repo.get_by_id.return_value = MagicMock(id=UUID(_BLOB_ID))
            mock_repo_cls.return_value = mock_blob_repo

            mock_ep_repo = AsyncMock()
            mock_episode = MagicMock()
            mock_episode.enrichment_status = ENRICHMENT_BLOB_TEXT
            mock_ep_repo.get_by_id.return_value = mock_episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            # Ctx WITHOUT db_engine or db_session_factory
            ctx: dict = {}
            result = await extract_blob_text(
                ctx=ctx,
                blob_id=_BLOB_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                episode_id=_EPISODE_ID,
                storage_key=_STORAGE_KEY,
                mime_type=_MIME_TYPE,
            )

            mock_init_engine.assert_called_once()
            mock_engine.dispose.assert_awaited_once()

    # ═══════════════════════════════════════════════════════════════════════
    # Missing db_session_factory in ctx
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_missing_session_factory_creates_from_engine(self) -> None:
        """When ctx has no ``db_session_factory``, one is created from engine."""
        from workers.tasks.extract_blob_text import extract_blob_text

        db = self._make_db(enrichment_status=ENRICHMENT_BLOB_TEXT)  # skip early

        with (
            patch("workers.tasks.extract_blob_text.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("core.db.get_async_session") as mock_get_session,
        ):
            session_factory = self._make_session_factory(db)
            mock_get_session.return_value = session_factory

            mock_blob_repo = AsyncMock()
            mock_blob_repo.get_by_id.return_value = MagicMock(id=UUID(_BLOB_ID))
            mock_repo_cls.return_value = mock_blob_repo

            mock_ep_repo = AsyncMock()
            mock_episode = MagicMock()
            mock_episode.enrichment_status = ENRICHMENT_BLOB_TEXT
            mock_ep_repo.get_by_id.return_value = mock_episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            # Ctx with db_engine but without db_session_factory
            ctx: dict = {"db_engine": MagicMock()}

            await extract_blob_text(
                ctx=ctx,
                blob_id=_BLOB_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                episode_id=_EPISODE_ID,
                storage_key=_STORAGE_KEY,
                mime_type=_MIME_TYPE,
            )

            mock_get_session.assert_called_once()

    # ═══════════════════════════════════════════════════════════════════════
    # Without bao_client
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_without_bao_client_uses_default_config(self) -> None:
        """No ``openbao_client`` in ctx → default storage config used."""
        from workers.tasks.extract_blob_text import extract_blob_text

        db = self._make_db()
        ctx = self._make_ctx(db, include_bao=False)

        with (
            patch("workers.tasks.extract_blob_text.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("core.blob_storage.BlobStorage") as mock_storage_cls,
            patch("core.blob_storage.BlobStorageConfig"),
            patch(
                "workers.tasks.extract_blob_text._dispatch_extraction",
                AsyncMock(return_value=None),
            ),
        ):
            mock_blob_repo = AsyncMock()
            mock_blob_repo.get_by_id.return_value = MagicMock(id=UUID(_BLOB_ID))
            mock_repo_cls.return_value = mock_blob_repo

            mock_ep_repo = AsyncMock()
            mock_episode = MagicMock()
            mock_episode.enrichment_status = 0
            mock_ep_repo.get_by_id.return_value = mock_episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            mock_storage = AsyncMock()
            mock_storage.download = AsyncMock(return_value=b"data")
            mock_storage_cls.return_value = mock_storage

            await extract_blob_text(
                ctx=ctx,
                blob_id=_BLOB_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                episode_id=_EPISODE_ID,
                storage_key=_STORAGE_KEY,
                mime_type=_MIME_TYPE,
            )

            mock_storage.download.assert_awaited_once()

    # ═══════════════════════════════════════════════════════════════════════
    # General exception handling
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_general_exception_re_raises(self) -> None:
        """Any exception during the pipeline propagates (for ARQ retry)."""
        from workers.tasks.extract_blob_text import extract_blob_text

        db = self._make_db()
        ctx = self._make_ctx(db)

        with (
            patch("workers.tasks.extract_blob_text.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
        ):
            mock_blob_repo = AsyncMock()
            mock_blob_repo.get_by_id.side_effect = RuntimeError("DB connection lost")
            mock_repo_cls.return_value = mock_blob_repo

            with pytest.raises(RuntimeError, match="DB connection lost"):
                await extract_blob_text(
                    ctx=ctx,
                    blob_id=_BLOB_ID,
                    org_id=_ORG_ID,
                    project_id=_PROJECT_ID,
                    episode_id=_EPISODE_ID,
                    storage_key=_STORAGE_KEY,
                    mime_type=_MIME_TYPE,
                )

    # ═══════════════════════════════════════════════════════════════════════
    # with_retry decorator presence
    # ═══════════════════════════════════════════════════════════════════════

    def test_decorated_with_with_retry(self) -> None:
        """``extract_blob_text`` has the ``@with_retry`` decorator applied."""
        from workers.tasks.extract_blob_text import extract_blob_text

        assert hasattr(extract_blob_text, "__wrapped__")
        assert callable(extract_blob_text.__wrapped__)


# ═══════════════════════════════════════════════════════════════════════════════
# extract_blob_text — ctx is not a dict
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestExtractBlobTextCtxObject:
    """Behaviour when ``ctx`` is an object instead of a dict."""

    @pytest.mark.asyncio
    async def test_ctx_as_object_creates_own_engine(self) -> None:
        """When ctx is an object (not dict), own engine is created."""
        from workers.tasks.extract_blob_text import extract_blob_text

        db = self._make_db(enrichment_status=ENRICHMENT_BLOB_TEXT)

        # ctx as a simple object — not a dict
        class FakeCtx:
            pass

        with (
            patch("workers.tasks.extract_blob_text.with_retry", lambda **kw: lambda f: f),
            patch("repositories.episode_blob_repository.EpisodeBlobRepository") as mock_repo_cls,
            patch("repositories.episode_repository.EpisodeRepository") as mock_ep_repo_cls,
            patch("core.db.init_db_engine") as mock_init_engine,
            patch("core.db.get_async_session") as mock_get_session,
        ):
            session_factory = MagicMock()
            session_factory.return_value = db
            mock_get_session.return_value = session_factory

            mock_engine = MagicMock()
            mock_engine.dispose = AsyncMock()
            mock_init_engine.return_value = mock_engine

            mock_blob_repo = AsyncMock()
            mock_blob_repo.get_by_id.return_value = MagicMock(id=UUID(_BLOB_ID))
            mock_repo_cls.return_value = mock_blob_repo

            mock_ep_repo = AsyncMock()
            mock_episode = MagicMock()
            mock_episode.enrichment_status = ENRICHMENT_BLOB_TEXT
            mock_ep_repo.get_by_id.return_value = mock_episode
            mock_ep_repo_cls.return_value = mock_ep_repo

            await extract_blob_text(
                ctx=FakeCtx(),
                blob_id=_BLOB_ID,
                org_id=_ORG_ID,
                project_id=_PROJECT_ID,
                episode_id=_EPISODE_ID,
                storage_key=_STORAGE_KEY,
                mime_type=_MIME_TYPE,
            )

            mock_init_engine.assert_called_once()
            mock_engine.dispose.assert_awaited_once()

    def _make_db(self, enrichment_status: int = 0) -> AsyncMock:
        """Create a mock DB session.  (Duplicate of class helper for reuse.)"""
        mock_db = AsyncMock()
        mock_db.__aenter__.return_value = mock_db
        mock_db.__aexit__.return_value = None
        mock_db.execute.return_value = MagicMock()
        return mock_db
