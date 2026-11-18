"""Unit tests for addon manifest parsing (core/addons/manifest.py).

No I/O beyond tmp_path fixture dirs — no DB, no Redis, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.addons import manifest as manifest_mod
from core.addons.manifest import _normalize_panel_path, parse_manifest
from core.exceptions import ValidationError


def _write_toml(tmp_path: Path, body: str) -> Path:
    addon_dir = tmp_path / "addon"
    addon_dir.mkdir(parents=True, exist_ok=True)
    (addon_dir / "addon.toml").write_text(body)
    return addon_dir


def _flat(**overrides: str) -> str:
    base = {"id": "democrm", "version": "1.0.0"}
    base.update(overrides)
    return "\n".join(f'{k} = "{v}"' for k, v in base.items()) + "\n"


class TestFlatShape:
    def test_minimal_flat_manifest_parses(self, tmp_path: Path) -> None:
        addon_dir = _write_toml(tmp_path, _flat())
        manifest = parse_manifest(addon_dir)
        assert manifest.id == "democrm"
        assert manifest.version == "1.0.0"
        assert manifest.min_core_version == "0.0.0"
        assert manifest.hooks == frozenset()
        assert manifest.permissions == frozenset()
        assert manifest.tables == ()
        assert manifest.panels == ()
        assert manifest.entrypoint == "hooks.py"

    def test_flat_hooks_permissions_tables_panels(self, tmp_path: Path) -> None:
        body = (
            _flat()
            + 'hooks = ["router", "worker"]\n'
            + 'permissions = ["project:read"]\n'
            + 'tables = ["notes"]\n'
            + 'entrypoint = "custom.py"\n'
            + '[[panels]]\nid = "board"\ntitle = "Board"\npath = "/ui/board.html"\n'
        )
        manifest = parse_manifest(_write_toml(tmp_path, body))
        assert manifest.hooks == frozenset({"router", "worker"})
        assert manifest.permissions == frozenset({"project:read"})
        assert manifest.tables == ("notes",)
        assert manifest.entrypoint == "custom.py"
        assert len(manifest.panels) == 1
        assert manifest.panels[0].path == "/ui/board.html"

    def test_missing_addon_toml(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="addon.toml not found"):
            parse_manifest(tmp_path / "empty")


class TestAddonTableShape:
    def test_section_shape_with_name_fallback(self, tmp_path: Path) -> None:
        body = (
            "[addon]\n"
            'name = "democrm"\n'
            'version = "2.3.4"\n'
            'min_core_version = "0.0.0"\n'
            'hooks = ["router"]\n'
            'permissions = ["project:write"]\n'
            'tables = ["notes"]\n'
        )
        manifest = parse_manifest(_write_toml(tmp_path, body))
        assert manifest.id == "democrm"
        assert manifest.version == "2.3.4"
        assert manifest.hooks == frozenset({"router"})

    def test_flat_keys_win_over_section(self, tmp_path: Path) -> None:
        body = _flat(version="9.9.9") + '[addon]\nversion = "1.0.0"\n'
        manifest = parse_manifest(_write_toml(tmp_path, body))
        assert manifest.version == "9.9.9"

    def test_unknown_section_ignored(self, tmp_path: Path) -> None:
        body = _flat() + '[signing]\nkey = "abc"\n'
        manifest = parse_manifest(_write_toml(tmp_path, body))
        assert manifest.id == "democrm"


class TestBadValues:
    def test_bad_id_uppercase(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="Invalid addon id"):
            parse_manifest(_write_toml(tmp_path, _flat(id="DemoCrm")))

    def test_bad_id_too_short(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="Invalid addon id"):
            parse_manifest(_write_toml(tmp_path, _flat(id="a")))

    def test_bad_version(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="Invalid version"):
            parse_manifest(_write_toml(tmp_path, _flat(version="1.0")))

    @pytest.mark.parametrize("version", ["0.1.0.dev1", "1.0.2.dev9"])
    def test_dev_version_accepted(self, tmp_path: Path, version: str) -> None:
        # Locked behavior: SEMVER_RE accepts a single X.Y.Z.devN suffix
        # (plus +local / -pre) alongside plain triples.
        manifest = parse_manifest(_write_toml(tmp_path, _flat(version=version)))
        assert manifest.version == version

    @pytest.mark.parametrize("version", ["", "abc", "1.2"])
    def test_unparseable_version_rejected(self, tmp_path: Path, version: str) -> None:
        with pytest.raises(ValidationError, match="Invalid version"):
            parse_manifest(_write_toml(tmp_path, _flat(version=version)))

    def test_build_metadata_version_accepted(self, tmp_path: Path) -> None:
        manifest = parse_manifest(_write_toml(tmp_path, _flat(version="1.0.0+build.1")))
        assert manifest.version == "1.0.0+build.1"

    def test_bad_min_core(self, tmp_path: Path) -> None:
        body = _flat() + 'min_core_version = "soon"\n'
        with pytest.raises(ValidationError, match="Invalid min_core_version"):
            parse_manifest(_write_toml(tmp_path, body))

    def test_min_core_newer_than_running_rejected(self, tmp_path: Path) -> None:
        body = _flat() + 'min_core_version = "99.0.0"\n'
        with pytest.raises(ValidationError, match="requires core"):
            parse_manifest(_write_toml(tmp_path, body))

    def test_unknown_hook(self, tmp_path: Path) -> None:
        body = _flat() + 'hooks = ["teleport"]\n'
        with pytest.raises(ValidationError, match="Unknown hooks"):
            parse_manifest(_write_toml(tmp_path, body))

    def test_unknown_permission(self, tmp_path: Path) -> None:
        body = _flat() + 'permissions = ["root:all"]\n'
        with pytest.raises(ValidationError, match="Unknown permissions"):
            parse_manifest(_write_toml(tmp_path, body))

    def test_bad_table_name(self, tmp_path: Path) -> None:
        for bad in ("Bad-Name", "has space", "9lives", "UPPER"):
            body = _flat() + f'tables = ["{bad}"]\n'
            with pytest.raises(ValidationError, match="Invalid table name"):
                parse_manifest(_write_toml(tmp_path, body))

    def test_bad_panel_id(self, tmp_path: Path) -> None:
        body = (
            _flat()
            + '[[panels]]\nid = "Bad!"\ntitle = "B"\npath = "/ui/b.html"\n'
        )
        with pytest.raises(ValidationError, match="Invalid panel id"):
            parse_manifest(_write_toml(tmp_path, body))


class TestPanelPathTraversal:
    @pytest.mark.parametrize(
        "raw",
        [
            "../secret.html",
            "ui/board.html",
            "/etc/passwd",
            "/ui/../secret",
            "/ui/../../etc/passwd",
            "/ui",
            "/ui/",
            "C:\\ui\\x.html",
        ],
    )
    def test_rejected_paths(self, raw: str) -> None:
        with pytest.raises(ValidationError):
            _normalize_panel_path(raw)

    def test_backslash_rejected(self) -> None:
        with pytest.raises(ValidationError, match="[Bb]ackslash"):
            _normalize_panel_path("/ui\\board.html")

    @pytest.mark.parametrize("raw", ["/ui/a.html", "/ui/sub/b.html", "/ui/a"])
    def test_accepted_paths(self, raw: str) -> None:
        assert _normalize_panel_path(raw) == raw

    def test_trailing_dotdot_escapes_ui(self) -> None:
        with pytest.raises(ValidationError):
            _normalize_panel_path("/ui/sub/..")

    def test_manifest_panel_traversal_rejected(self, tmp_path: Path) -> None:
        body = (
            _flat()
            + '[[panels]]\nid = "evil"\ntitle = "E"\npath = "/ui/../../x"\n'
        )
        with pytest.raises(ValidationError):
            parse_manifest(_write_toml(tmp_path, body))


class TestEntrypoint:
    def test_default_entrypoint(self, tmp_path: Path) -> None:
        assert parse_manifest(_write_toml(tmp_path, _flat())).entrypoint == "hooks.py"

    def test_custom_entrypoint(self, tmp_path: Path) -> None:
        body = _flat() + 'entrypoint = "addon/__init__.py"\n'
        manifest = parse_manifest(_write_toml(tmp_path, body))
        assert manifest.entrypoint == "addon/__init__.py"


class TestLoaderProbeOrder:
    def test_entrypoint_default_prefers_hooks_py(self, tmp_path: Path) -> None:
        from core.addons.loader import load_addon

        src = tmp_path / "src"
        src.mkdir()
        (src / "addon.toml").write_text(_flat())
        (src / "hooks.py").write_text('MARKER = "hooks"\n')
        (src / "addon.py").write_text('MARKER = "addon"\n')
        manifest = parse_manifest(src)
        assert manifest_mod.parse_manifest is parse_manifest  # sanity
        module = load_addon(manifest, src, "1.0.0")
        assert module.MARKER == "hooks"

    def test_explicit_entrypoint_wins(self, tmp_path: Path) -> None:
        from core.addons.loader import load_addon
        from core.addons.manifest import AddonManifest

        src = tmp_path / "src"
        src.mkdir()
        (src / "hooks.py").write_text('MARKER = "hooks"\n')
        (src / "custom.py").write_text('MARKER = "custom"\n')
        manifest = AddonManifest(
            id="demo",
            version="1.0.0",
            min_core_version="0.0.0",
            hooks=frozenset(),
            permissions=frozenset(),
            entrypoint="custom.py",
        )
        assert load_addon(manifest, src, "1.0.0").MARKER == "custom"

    def test_no_loadable_entrypoint(self, tmp_path: Path) -> None:
        from core.addons.loader import load_addon
        from core.addons.manifest import AddonManifest

        src = tmp_path / "src"
        src.mkdir()
        manifest = AddonManifest(
            id="demo",
            version="1.0.0",
            min_core_version="0.0.0",
            hooks=frozenset(),
            permissions=frozenset(),
        )
        with pytest.raises(ValidationError, match="no loadable entrypoint"):
            load_addon(manifest, src, "1.0.0")
