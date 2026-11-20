"""Addon installer — copy local dirs or shallow-clone github tags."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import shutil
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

import structlog

from core.exceptions import ExternalServiceError, ValidationError

logger = structlog.get_logger(__name__)

ADDON_ROOT = Path("var/addons")
"""On-disk store for installed addon sources (no env-var config)."""

_LS_TIMEOUT_S = 15.0
_LS_CONCURRENCY = 5


def allowed_addon_src() -> Path:
    """Return the resolved dir local addon sources must live under.

    Env-managed ``ALLOWED_ADDON_SRC``; defaults to ``<repo>/addons``.

    Returns:
        The resolved allowed source directory.
    """
    try:
        from core.config import get_settings

        raw = getattr(get_settings(), "ALLOWED_ADDON_SRC", None) or "addons"
    except RuntimeError:
        raw = "addons"
    return Path(raw).resolve()


@dataclass(frozen=True, slots=True)
class InstalledSource:
    """Result of materialising an addon source on disk."""

    addon_id: str
    version: str
    installed_sha: str
    path: Path


def local_source_version(addon_dir: Path) -> str:
    """Read the ``pyproject.toml`` version of a local addon source.

    Args:
        addon_dir: Directory containing ``pyproject.toml``.

    Returns:
        The ``project.version`` string.

    Raises:
        ValidationError: If the file is missing or has no version.
    """
    pyproject = addon_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            raw = tomllib.loads(pyproject.read_text())
            return str(raw["project"]["version"])
        except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
            raise ValidationError(f"pyproject.toml has no version: {exc}") from exc
    raise ValidationError("Local addon source must contain pyproject.toml.")


def _resolve_local_source(source_ref: str) -> Path:
    """Resolve a local source, confining it under ``allowed_addon_src``.

    Args:
        source_ref: Caller-supplied local path.

    Returns:
        The resolved source directory.

    Raises:
        ValidationError: When the path escapes the allowed dir (422), is
            a symlink, or has a symlinked parent up to the allowed root.
    """
    allowed = allowed_addon_src()
    candidate = Path(source_ref)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    # Reject symlinks: the path itself plus every parent up to the root.
    # Resolved components below the allowed dir are checked by name so a
    # symlinked ancestor cannot smuggle an outside tree inside.
    abs_path = candidate.absolute()
    parts = abs_path.parts
    # Probe only components at/below the allowed root — ancestors above
    # it (symlinked cwd, /tmp) are outside our threat model and must
    # not fail closed.
    for i in range(len(allowed.parts), len(parts) + 1):
        probe = Path(*parts[:i])
        if probe != allowed and allowed not in probe.parents:
            continue
        if probe.is_symlink():
            raise ValidationError(f"Symlinks not allowed: {probe}.")
    try:
        resolved = abs_path.resolve()
    except OSError as exc:
        raise ValidationError(f"Local addon dir not found: {source_ref}.") from exc
    if resolved == allowed or allowed not in resolved.parents:
        raise ValidationError(
            f"Local addon source must be under {allowed}: {source_ref}."
        )
    if not resolved.is_dir():
        raise ValidationError(f"Local addon dir not found: {source_ref}.")
    return resolved


def _atomic_replace(src_tmp: Path, dest: Path) -> None:
    """Atomically swap a staged temp dir over ``dest``.

    ``os.replace`` cannot overwrite a non-empty dir, so the live dir is
    first renamed aside; the staged dir then takes its name atomically
    and the backup is removed. Failure before the swap leaves ``dest``
    untouched; failure after restores the backup.

    Args:
        src_tmp: Fully materialised staging dir (same filesystem as dest).
        dest: Live install dir.
    """
    backup: Path | None = None
    if dest.exists() or dest.is_symlink():
        # Unique suffix: concurrent installs must not share one .bak.
        backup = dest.with_name(
            f"{dest.name}.bak.{os.getpid()}.{secrets.token_hex(4)}"
        )
        os.rename(dest, backup)
    try:
        os.replace(src_tmp, dest)
    except BaseException:
        if backup is not None and not dest.exists():
            os.rename(backup, dest)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


_TAG_RE = re.compile(r"^[\w][\w.\-/]*$")
"""Allowed git-tag charset (leading dash rejected: option injection)."""


def _reject_symlinked_tree(src: Path) -> None:
    """Reject any symlink inside a local addon source dir.

    Args:
        src: The resolved source directory to walk.

    Raises:
        ValidationError: When ``src`` or any entry in it is a symlink
            (copytree would otherwise materialise outside targets).
    """
    if src.is_symlink():
        raise ValidationError(f"Symlinks not allowed: {src}.")
    for entry in src.rglob("*"):
        if entry.is_symlink():
            raise ValidationError(f"Symlinks not allowed: {entry}.")


async def install_source(
    source_type: str, source_ref: str, tag: str | None = None
) -> InstalledSource:
    """Materialise an addon source into ``ADDON_ROOT``.

    Args:
        source_type: ``"local"`` (copy dir) or ``"github"`` (clone tag).
        source_ref: Local path or ``owner/repo`` (https or ssh accepted as
            ``github_url`` and normalised here).
        tag: Git tag to clone; ``None`` resolves to the latest tag via
            ``git ls-remote --tags``.

    Returns:
        The installed source record.

    Raises:
        ValidationError: On bad source type or missing local dir.
        ExternalServiceError: When git clone/ls-remote fails.
    """
    if source_type not in ("local", "github"):
        raise ValidationError(f"Unknown source_type: {source_type!r}.")
    ADDON_ROOT.mkdir(parents=True, exist_ok=True)

    if source_type == "local":
        src = _resolve_local_source(source_ref)
        _reject_symlinked_tree(src)
        from core.addons.manifest import parse_manifest

        manifest = parse_manifest(src)
        dest = ADDON_ROOT / manifest.id
        tmp = Path(tempfile.mkdtemp(prefix=f"{manifest.id}.", dir=str(ADDON_ROOT)))
        shutil.rmtree(tmp)
        try:
            shutil.copytree(src, tmp)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        _atomic_replace(tmp, dest)
        digest = hashlib.sha256(str(dest.stat().st_mtime).encode()).hexdigest()[:12]
        return InstalledSource(
            addon_id=manifest.id,
            version=local_source_version(dest),
            installed_sha=f"local-{digest}",
            path=dest,
        )

    repo = source_ref.removeprefix("https://github.com/").removesuffix(".git")
    if repo.startswith("git@github.com:"):
        repo = repo.removeprefix("git@github.com:").removesuffix(".git")
    url = f"https://github.com/{repo}.git"
    resolved_tag = tag or await latest_github_tag(url)
    if not _TAG_RE.match(resolved_tag):
        raise ValidationError(f"Invalid git tag: {resolved_tag!r}.")
    slug = repo.replace("/", "__")
    dest = ADDON_ROOT / slug
    tmp = Path(tempfile.mkdtemp(prefix=f"{slug}.", dir=str(ADDON_ROOT)))
    shutil.rmtree(tmp)
    proc = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        f"--branch={resolved_tag}",
        "--depth",
        "1",
        url,
        str(tmp),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ExternalServiceError(f"git clone failed: {err.decode()[:500]}")
    from core.addons.manifest import parse_manifest

    manifest = parse_manifest(tmp)
    _atomic_replace(tmp, dest)
    return InstalledSource(
        addon_id=manifest.id,
        version=manifest.version,
        installed_sha=resolved_tag,
        path=dest,
    )


async def latest_github_tag(repo_url: str) -> str:
    """Resolve the newest semver tag via ``git ls-remote --tags``.

    Args:
        repo_url: Clone URL of the addon repo.

    Returns:
        The latest tag name.

    Raises:
        ExternalServiceError: When ls-remote fails or no tags exist.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        "ls-remote",
        "--tags",
        repo_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise ExternalServiceError(f"git ls-remote failed: {err.decode()[:500]}")
    tags = [
        line.split("refs/tags/")[-1].removesuffix("^{}")
        for line in out.decode().splitlines()
        if "refs/tags/" in line and "^{}" not in line
    ]
    if not tags:
        raise ExternalServiceError(f"No tags found for {repo_url}.")
    return max(tags, key=_tag_sort_key)


_TAG_NUM_RE = re.compile(r"\d+")


def _tag_sort_key(tag: str) -> tuple[int, tuple[int, ...], str]:
    """Sort key ordering semver tags numerically (``v10`` > ``v9``).

    Args:
        tag: Raw tag name (leading ``v`` stripped for parsing).

    Returns:
        ``(is_semver, numeric_tuple, raw)`` — semver-like tags sort
        above non-semver ones; unparseable tags fall back to raw order.
    """
    stripped = tag.strip().removeprefix("v").removeprefix("V")
    nums = _TAG_NUM_RE.findall(stripped.split("-")[0].split("+")[0])
    if not nums:
        return (0, (), tag)
    try:
        return (1, tuple(int(n) for n in nums[:4]), tag)
    except ValueError:
        return (0, (), tag)


__all__ = [
    "ADDON_ROOT",
    "InstalledSource",
    "allowed_addon_src",
    "install_source",
    "latest_github_tag",
    "local_source_version",
]
