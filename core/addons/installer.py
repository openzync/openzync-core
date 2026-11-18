"""Addon installer — copy local dirs or shallow-clone github tags."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from core.exceptions import ExternalServiceError, ValidationError

ADDON_ROOT = Path("var/addons")
"""On-disk store for installed addon sources (no env-var config)."""


@dataclass(frozen=True, slots=True)
class InstalledSource:
    """Result of materialising an addon source on disk."""

    addon_id: str
    version: str
    installed_sha: str
    path: Path


def _local_version(addon_dir: Path) -> str:
    pyproject = addon_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            raw = tomllib.loads(pyproject.read_text())
            return str(raw["project"]["version"])
        except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
            raise ValidationError(f"pyproject.toml has no version: {exc}") from exc
    raise ValidationError("Local addon source must contain pyproject.toml.")


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
        src = Path(source_ref)
        if not src.is_dir():
            raise ValidationError(f"Local addon dir not found: {source_ref}.")
        from core.addons.manifest import parse_manifest

        manifest = parse_manifest(src)
        dest = ADDON_ROOT / manifest.id
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest)
        digest = hashlib.sha256(str(dest.stat().st_mtime).encode()).hexdigest()[:12]
        return InstalledSource(
            addon_id=manifest.id,
            version=_local_version(dest),
            installed_sha=f"local-{digest}",
            path=dest,
        )

    repo = source_ref.removeprefix("https://github.com/").removesuffix(".git")
    if repo.startswith("git@github.com:"):
        repo = repo.removeprefix("git@github.com:").removesuffix(".git")
    url = f"https://github.com/{repo}.git"
    resolved_tag = tag or await latest_github_tag(url)
    slug = repo.replace("/", "__")
    dest = ADDON_ROOT / slug
    shutil.rmtree(dest, ignore_errors=True)
    proc = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        "--branch",
        resolved_tag,
        "--depth",
        "1",
        url,
        str(dest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise ExternalServiceError(f"git clone failed: {err.decode()[:500]}")
    from core.addons.manifest import parse_manifest

    manifest = parse_manifest(dest)
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
    return sorted(tags)[-1]


__all__ = ["ADDON_ROOT", "InstalledSource", "install_source", "latest_github_tag"]
