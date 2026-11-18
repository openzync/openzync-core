"""Unit tests for the addon installer (core/addons/installer.py).

Filesystem tests use tmp_path only — no DB, no Redis, no network.
``install_source`` itself is covered by the integration lifecycle test;
here we pin the confinement, symlink, atomicity, and version helpers.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.addons import installer
from core.addons.installer import (
    _atomic_replace,
    _reject_symlinked_tree,
    _resolve_local_source,
    local_source_version,
)
from core.exceptions import ValidationError


@pytest.fixture
def allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setattr(installer, "allowed_addon_src", lambda: root)
    return root


class TestAllowlistConfine:
    def test_inside_allowed(self, allowed: Path) -> None:
        target = allowed / "myaddon"
        target.mkdir()
        assert _resolve_local_source(str(target)) == target.resolve()

    def test_nested_inside_allowed(self, allowed: Path) -> None:
        target = allowed / "a" / "b"
        target.mkdir(parents=True)
        assert _resolve_local_source(str(target)) == target.resolve()

    def test_outside_rejected(self, allowed: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(ValidationError, match="must be under"):
            _resolve_local_source(str(outside))

    def test_traversal_rejected(self, allowed: Path, tmp_path: Path) -> None:
        (tmp_path / "evil").mkdir()
        with pytest.raises(ValidationError, match="must be under"):
            _resolve_local_source(str(allowed / ".." / "evil"))

    def test_absolute_outside_rejected(self, allowed: Path) -> None:
        with pytest.raises(ValidationError, match="must be under"):
            _resolve_local_source("/etc")

    def test_allowed_root_itself_rejected(self, allowed: Path) -> None:
        with pytest.raises(ValidationError, match="must be under"):
            _resolve_local_source(str(allowed))

    def test_missing_dir_rejected(self, allowed: Path) -> None:
        with pytest.raises(ValidationError, match="not found"):
            _resolve_local_source(str(allowed / "nosuchdir"))

    def test_unknown_source_type(self) -> None:
        import asyncio

        with pytest.raises(ValidationError, match="Unknown source_type"):
            asyncio.run(installer.install_source("ftp", "x", None))


class TestSymlinks:
    def test_symlinked_source_rejected(self, allowed: Path, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = allowed / "linked"
        link.symlink_to(real, target_is_directory=True)
        with pytest.raises(ValidationError, match="[Ss]ymlink"):
            _resolve_local_source(str(link))

    def test_symlinked_file_in_tree_rejected(self, allowed: Path, tmp_path: Path) -> None:
        src = allowed / "addon"
        src.mkdir()
        (src / "hooks.py").write_text("x = 1\n")
        (tmp_path / "secret.txt").write_text("s3cret")
        (src / "leak.txt").symlink_to(tmp_path / "secret.txt")
        with pytest.raises(ValidationError, match="[Ss]ymlink"):
            _reject_symlinked_tree(src)

    def test_symlinked_subdir_in_tree_rejected(self, allowed: Path, tmp_path: Path) -> None:
        src = allowed / "addon"
        src.mkdir()
        outside = tmp_path / "outside-tree"
        outside.mkdir()
        (src / "sub").symlink_to(outside, target_is_directory=True)
        with pytest.raises(ValidationError, match="[Ss]ymlink"):
            _reject_symlinked_tree(src)

    def test_clean_tree_ok(self, allowed: Path) -> None:
        src = allowed / "addon"
        (src / "sub").mkdir(parents=True)
        (src / "hooks.py").write_text("x = 1\n")
        _reject_symlinked_tree(src)

    def test_symlinked_ancestor_above_root_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A symlinked cwd (/tmp on some systems) must not fail closed:
        # only components at/below the allowed root are probed.
        real = tmp_path / "real"
        addon = real / "allowed" / "addon"
        addon.mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        monkeypatch.setattr(installer, "allowed_addon_src", lambda: (real / "allowed").resolve())
        resolved = _resolve_local_source(str(link / "allowed" / "addon"))
        assert resolved == addon.resolve()


class TestAtomicReplace:
    def test_swap_replaces_dest(self, tmp_path: Path) -> None:
        dest = tmp_path / "live"
        dest.mkdir()
        (dest / "v.txt").write_text("old")
        staged = tmp_path / "staged"
        staged.mkdir()
        (staged / "v.txt").write_text("new")
        _atomic_replace(staged, dest)
        assert (dest / "v.txt").read_text() == "new"
        assert not staged.exists()
        assert list(tmp_path.glob("live.bak.*")) == []

    def test_swap_onto_missing_dest(self, tmp_path: Path) -> None:
        dest = tmp_path / "live"
        staged = tmp_path / "staged"
        staged.mkdir()
        (staged / "v.txt").write_text("new")
        _atomic_replace(staged, dest)
        assert (dest / "v.txt").read_text() == "new"

    def test_failure_restores_backup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        dest = tmp_path / "live"
        dest.mkdir()
        (dest / "v.txt").write_text("old")
        staged = tmp_path / "staged"
        staged.mkdir()
        (staged / "v.txt").write_text("new")

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError, match="disk full"):
            _atomic_replace(staged, dest)
        assert (dest / "v.txt").read_text() == "old"


class TestLocalSourceVersion:
    def test_reads_pyproject_version(self, tmp_path: Path) -> None:
        src = tmp_path / "addon"
        src.mkdir()
        (src / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n')
        assert local_source_version(src) == "1.2.3"

    def test_missing_pyproject_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="pyproject.toml"):
            local_source_version(tmp_path)

    def test_pyproject_without_version_rejected(self, tmp_path: Path) -> None:
        src = tmp_path / "addon"
        src.mkdir()
        (src / "pyproject.toml").write_text('[project]\nname = "x"\n')
        with pytest.raises(ValidationError, match="no version"):
            local_source_version(src)
