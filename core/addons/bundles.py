"""Addon bundle intake — upload validation, safe extraction, staging.

Pure infrastructure: validates the ``.zip`` / ``.tar.gz`` suffix, size,
and magic bytes, rejects absolute paths and ``..`` escapes, and stages
the bundle into a temp dir. Returns the manifest bytes plus the staging
directory; the caller promotes it after signature verification.
"""

from __future__ import annotations

import io
import logging
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path
from zipfile import ZipInfo

from starlette.datastructures import UploadFile

from core.addons.settings_schema import parse_settings_schema
from core.exceptions import ValidationError

logger = logging.getLogger(__name__)

MAX_BUNDLE_BYTES = 10 * 1024 * 1024
"""Largest accepted bundle (10 MiB) — fail-closed above."""

ALLOWED_SUFFIXES: tuple[str, ...] = (".zip", ".tar.gz", ".tgz")
"""Accepted bundle filename suffixes."""


async def extract_bundle(bundle: UploadFile) -> tuple[bytes, Path]:
    """Validate and stage a bundle, returning manifest bytes + dir.

    Args:
        bundle: The uploaded file.

    Returns:
        The ``(addon.toml bytes, staging dir)`` pair. The caller owns the
        staging dir (promote on success, ``shutil.rmtree`` on failure).

    Raises:
        ValidationError: On bad suffix, oversize, bad MIME, or unsafe /
            missing members.
    """
    filename = bundle.filename or ""
    if not filename.endswith(ALLOWED_SUFFIXES):
        raise ValidationError(
            f"Bundle must end in {ALLOWED_SUFFIXES}, got {filename!r}."
        )
    content = await bundle.read()
    if len(content) > MAX_BUNDLE_BYTES:
        raise ValidationError(
            f"Bundle too large ({len(content)} bytes > {MAX_BUNDLE_BYTES})."
        )
    _check_mime(content, filename)
    stage = Path(tempfile.mkdtemp(prefix="addon_"))
    try:
        _unpack(content, filename, stage)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    raw = stage / "addon.toml"
    if not raw.is_file():
        shutil.rmtree(stage, ignore_errors=True)
        raise ValidationError("Bundle has no top-level addon.toml.")
    if not (stage / "hooks.py").is_file():
        shutil.rmtree(stage, ignore_errors=True)
        raise ValidationError("Bundle has no top-level hooks.py.")
    return raw.read_bytes(), stage


def _check_mime(content: bytes, filename: str) -> None:
    """Reject bundles whose magic bytes are not zip/gzip.

    Falls back to structural parsing when libmagic returns a generic
    ``application/octet-stream`` (small stored-entry zips misdetect this
    way on some platforms).

    Args:
        content: The bundle bytes.
        filename: The client-supplied filename (suffix already checked).

    Raises:
        ValidationError: When the content is not an archive.
    """
    try:
        import magic

        mime = magic.from_buffer(content[:2048], mime=True)
    except ImportError:
        mime = "application/zip" if filename.endswith(".zip") else "application/gzip"
    if mime in {
        "application/zip",
        "application/gzip",
        "application/x-gzip",
        "application/x-tar",
    }:
        return
    if mime not in {"application/octet-stream", "", None}:
        raise ValidationError(f"Bundle MIME {mime!r} is not an archive.")
    if filename.endswith(".zip"):
        if zipfile.is_zipfile(io.BytesIO(content)):
            return
    else:
        try:
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as a:
                a.getmembers()
            return
        except (tarfile.TarError, EOFError, OSError, ValueError):
            pass
    raise ValidationError(f"Bundle MIME {mime!r} is not an archive.")


def _unpack(content: bytes, filename: str, stage: Path) -> None:
    """Unpack a bundle, rejecting absolute paths and ``..`` escapes.

    Args:
        content: The bundle bytes.
        filename: The client-supplied filename (drives zip vs tar).
        stage: The staging directory.

    Raises:
        ValidationError: On unsafe member paths.
    """
    if filename.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for info in archive.infolist():
                _check_member(info.filename)
                if _is_zip_symlink(info):
                    raise ValidationError(
                        f"Unsafe bundle member (symlink): {info.filename!r}."
                    )
            # ⚠️ Members pre-validated (no abs/.. paths) into a temp dir.
            archive.extractall(stage)  # noqa: S202
    else:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
            for member in archive.getmembers():
                _check_member(member.name)
                if member.issym() or member.islnk():
                    raise ValidationError(
                        f"Unsafe bundle member (link): {member.name!r}."
                    )
            archive.extractall(stage, filter="data")


def _is_zip_symlink(info: ZipInfo) -> bool:
    """Whether a zip member is a symlink (never extracted).

    Args:
        info: The zip member metadata.

    Returns:
        ``True`` when the member's unix mode marks a symlink.
    """
    is_symlink = getattr(info, "is_symlink", None)
    if callable(is_symlink):
        return bool(is_symlink())
    return stat.S_ISLNK(info.external_attr >> 16)


def read_settings_schema(stage: Path) -> dict | None:
    """Read and validate the staged ``settings.schema.json``, if present.

    Addons without settings simply omit the file — this returns ``None``
    and their settings endpoints 404 with a clear message.

    Args:
        stage: The staged bundle directory.

    Returns:
        The parsed schema, or ``None`` when the bundle ships no file.

    Raises:
        ValidationError: When the file exists but is invalid.
    """
    path = stage / "settings.schema.json"
    if not path.is_file():
        return None
    return parse_settings_schema(path.read_bytes())


def _check_member(name: str) -> None:
    """Reject one archive member path (absolute / traversal / empty).

    Args:
        name: The member path.

    Raises:
        ValidationError: When the path is unsafe.
    """
    if not name or name.startswith(("/", "\\")) or ".." in Path(name).parts:
        raise ValidationError(f"Unsafe bundle member path: {name!r}.")
