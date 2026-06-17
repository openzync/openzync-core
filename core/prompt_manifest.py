"""Prompt template manifest — load metadata and template text from disk.

``manifest.yaml`` and ``.jinja2`` files in :data:`PROMPTS_DIR` are the
canonical source of truth for system-default prompt templates.  This module
provides a caching loader and lookup helpers so the rest of the system never
needs to know about the file layout.

Usage::

    from core.prompt_manifest import load_manifest

    manifest = load_manifest()
    entry = manifest.get_default_for_type("fact_extraction")
    if entry:
        text = manifest.get_template_text(entry["file"])
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

#: Absolute path to the directory containing ``manifest.yaml`` and ``.jinja2`` files.
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "services" / "worker" / "prompts"

#: Name of the YAML manifest file inside :data:`PROMPTS_DIR`.
MANIFEST_FILENAME = "manifest.yaml"

#: Sentinel used by :func:`load_manifest` to detect cache-miss.
_UNSET = object()

#: Module-level cache — reloaded on each call to :func:`load_manifest`.
_manifest_cache: object = _UNSET


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════


def load_manifest(*, reload: bool = False) -> PromptManifest:
    """Load and return the parsed prompt manifest.

    Results are cached module-globally across calls.  Pass ``reload=True``
    to force a re-read from disk (useful in tests or after deploys).

    Args:
        reload: If ``True``, bypass the cache and re-read from disk.

    Returns:
        A :class:`PromptManifest` with lookup helpers.

    Raises:
        FileNotFoundError: If ``manifest.yaml`` does not exist.
        yaml.YAMLError: If the manifest file is malformed.
    """
    global _manifest_cache  # noqa: PLW0603 — acceptable module-level cache

    if reload or _manifest_cache is _UNSET:
        path = PROMPTS_DIR / MANIFEST_FILENAME
        if not path.exists():
            raise FileNotFoundError(
                f"Prompt manifest not found: {path}. "
                f"Ensure services/worker/prompts/manifest.yaml exists."
            )
        with open(path) as f:
            data = yaml.safe_load(f)
        _manifest_cache = PromptManifest(data or {})
        logger.info("Loaded prompt manifest from %s", path)

    return _manifest_cache  # type: ignore[return-value]


def invalidate_manifest_cache() -> None:
    """Clear the module-level manifest cache.

    The next call to :func:`load_manifest` will re-read from disk.
    """
    global _manifest_cache  # noqa: PLW0603
    _manifest_cache = _UNSET


# ═══════════════════════════════════════════════════════════════════════════════
# Data class
# ═══════════════════════════════════════════════════════════════════════════════


class PromptManifest:
    """Parsed manifest data with efficient lookup helpers.

    Attributes:
        version: Manifest schema version (from the YAML).
        templates: Raw list of template dicts from the manifest.
        by_name: Mapping of ``template_name → entry``.
        by_type: Mapping of ``type → [entries]``.
    """

    def __init__(self, data: dict) -> None:
        self.version: int = data.get("version", 1)
        self.templates: list[dict] = data.get("templates", [])
        self.by_name: dict[str, dict] = {
            t["name"]: t for t in self.templates if "name" in t
        }
        self.by_type: dict[str, list[dict]] = {}
        for t in self.templates:
            ttype = t.get("type")
            if ttype:
                self.by_type.setdefault(ttype, []).append(t)

    # ── Lookup helpers ──────────────────────────────────────────────────────

    def get_by_name(self, name: str) -> dict | None:
        """Return the manifest entry for a template name, or ``None``."""
        return self.by_name.get(name)

    def get_default_for_type(self, type: str) -> dict | None:
        """Return the manifest entry marked as type default, or ``None``.

        Only one entry per type should have ``is_default_for_type: true``.
        If multiple are accidentally marked, the first match wins.
        """
        for t in self.by_type.get(type, []):
            if t.get("is_default_for_type"):
                return t
        return None

    def get_default_names(self) -> list[str]:
        """Return the template names of all type-default entries."""
        return [
            t["name"] for t in self.templates if t.get("is_default_for_type")
        ]

    # ── File I/O ────────────────────────────────────────────────────────────

    def get_template_text(self, file_name: str) -> str:
        """Read the actual prompt template text from disk.

        Args:
            file_name: Relative filename from the manifest (e.g. ``"extract_facts_v4.jinja2"``).

        Returns:
            The full file contents as a string.

        Raises:
            FileNotFoundError: If the file does not exist inside :data:`PROMPTS_DIR`.
        """
        path = PROMPTS_DIR / file_name
        if not path.exists():
            raise FileNotFoundError(
                f"Prompt file referenced in manifest not found: {path}"
            )
        return path.read_text()
