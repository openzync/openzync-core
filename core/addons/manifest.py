"""Parse and validate ``addon.toml`` manifests."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from core._version import __version__
from core.exceptions import ValidationError
from core.rbac import ALL_PERMISSIONS

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")
TABLE_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([+-][0-9A-Za-z.-]+)?$")
HOOKS_ALLOWLIST = frozenset(
    {
        "router",
        "service",
        "middleware",
        "worker",
        "graph_backend",
        "embedder",
        "llm",
        "health",
        "settings",
        "admin_ui",
    }
)


@dataclass(frozen=True, slots=True)
class AddonPanel:
    """One admin-ui panel served as an iframe."""

    id: str
    title: str
    path: str


@dataclass(frozen=True, slots=True)
class AddonManifest:
    """Validated ``addon.toml`` contents."""

    id: str
    version: str
    min_core_version: str
    hooks: frozenset[str]
    permissions: frozenset[str]
    tables: tuple[str, ...] = ()
    panels: tuple[AddonPanel, ...] = ()
    entrypoint: str = "hooks.py"


_LEADING_TRIPLE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _semver_tuple(v: str) -> tuple[int, int, int]:
    match = _LEADING_TRIPLE_RE.match(v.strip())
    if match is None:
        raise ValueError(f"no leading MAJOR.MINOR.PATCH in {v!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def parse_manifest(addon_dir: Path) -> AddonManifest:
    """Parse and validate ``addon.toml`` in ``addon_dir``.

    Args:
        addon_dir: Directory containing ``addon.toml``.

    Returns:
        The validated manifest.

    Raises:
        ValidationError: If any field violates the locked contract.
    """
    toml_path = addon_dir / "addon.toml"
    if not toml_path.is_file():
        raise ValidationError("addon.toml not found.", {"dir": str(addon_dir)})
    try:
        raw = tomllib.loads(toml_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValidationError(f"addon.toml unreadable: {exc}") from exc

    # ``[addon]`` table shape: bundles may nest every field one level down
    # (``name`` instead of ``id``). Flat keys stay canonical — the section
    # is only a fallback. Unknown sections (e.g. ``[signing]``) are ignored.
    section = raw.get("addon")
    if not isinstance(section, dict):
        section = {}

    def _pick(key: str, section_key: str | None = None) -> object:
        value = raw.get(key)
        if value is None:
            value = section.get(section_key or key)
        return value

    addon_id = str(_pick("id", "name") or "")
    version = str(_pick("version") or "")
    min_core = str(_pick("min_core_version") or "0.0.0")
    if not ID_RE.match(addon_id):
        raise ValidationError(f"Invalid addon id: {addon_id!r}.")
    if not SEMVER_RE.match(version):
        raise ValidationError(f"Invalid version: {version!r}.")
    if not SEMVER_RE.match(min_core):
        raise ValidationError(f"Invalid min_core_version: {min_core!r}.")
    try:
        if _semver_tuple(__version__) < _semver_tuple(min_core):
            raise ValidationError(
                f"Addon requires core >= {min_core}, running {__version__}."
            )
    except ValidationError:
        raise
    except ValueError as exc:
        raise ValidationError(f"Unparseable version: {exc}") from exc

    hooks = frozenset(str(h) for h in (_pick("hooks") or []))
    unknown_hooks = hooks - HOOKS_ALLOWLIST
    if unknown_hooks:
        raise ValidationError(f"Unknown hooks: {sorted(unknown_hooks)}.")
    permissions = frozenset(str(p) for p in (_pick("permissions") or []))
    unknown_perms = permissions - ALL_PERMISSIONS
    if unknown_perms:
        raise ValidationError(f"Unknown permissions: {sorted(unknown_perms)}.")

    tables = tuple(str(t) for t in (_pick("tables") or []))
    for table in tables:
        if not TABLE_RE.match(table):
            raise ValidationError(f"Invalid table name: {table!r}.")

    panels: list[AddonPanel] = []
    for panel in (_pick("panels") or []):
        pid, title, path = str(panel["id"]), str(panel["title"]), str(panel["path"])
        if not ID_RE.match(pid):
            raise ValidationError(f"Invalid panel id: {pid!r}.")
        if not path.startswith("/ui/"):
            raise ValidationError(f"Panel path must start with /ui/: {path!r}.")
        panels.append(AddonPanel(id=pid, title=title, path=path))

    return AddonManifest(
        id=addon_id,
        version=version,
        min_core_version=min_core,
        hooks=hooks,
        permissions=permissions,
        tables=tables,
        panels=tuple(panels),
        entrypoint=str(_pick("entrypoint") or "hooks.py"),
    )


__all__ = ["AddonManifest", "AddonPanel", "HOOKS_ALLOWLIST", "field", "parse_manifest"]
