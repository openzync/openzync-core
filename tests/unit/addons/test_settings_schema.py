"""Unit tests for addon settings-schema parsing (core/addons/manifest.py).

No I/O beyond tmp_path fixture dirs — no DB, no Redis, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.addons.manifest import parse_manifest
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


class TestSettingsSchemaHappyPath:
    def test_number_string_boolean_defaults_parse(self, tmp_path: Path) -> None:
        body = (
            _flat()
            + '[settings_schema.threshold]\ntype = "number"\ndefault = 0.5\n'
            + '[settings_schema.greeting]\ntype = "string"\ndefault = "hi"\n'
            + '[settings_schema.verbose]\ntype = "boolean"\ndefault = true\n'
        )
        manifest = parse_manifest(_write_toml(tmp_path, body))
        by_key = {s.key: s for s in manifest.settings_schema}
        assert by_key["threshold"].type == "number"
        assert by_key["threshold"].default == 0.5
        assert by_key["greeting"].type == "string"
        assert by_key["greeting"].default == "hi"
        assert by_key["verbose"].type == "boolean"
        assert by_key["verbose"].default is True

    def test_enum_parses_to_tuple(self, tmp_path: Path) -> None:
        body = (
            _flat()
            + '[settings_schema.mode]\ntype = "string"\ndefault = "fast"\n'
            + 'enum = ["fast", "slow"]\n'
        )
        manifest = parse_manifest(_write_toml(tmp_path, body))
        assert manifest.settings_schema[0].enum == ("fast", "slow")


class TestSettingsSchemaRejects:
    def test_unknown_type_rejected(self, tmp_path: Path) -> None:
        body = _flat() + '[settings_schema.threshold]\ntype = "integer"\ndefault = 1\n'
        with pytest.raises(ValidationError, match="unknown type"):
            parse_manifest(_write_toml(tmp_path, body))

    def test_bad_default_type_rejected(self, tmp_path: Path) -> None:
        body = (
            _flat()
            + '[settings_schema.threshold]\ntype = "number"\ndefault = "high"\n'
        )
        with pytest.raises(ValidationError, match="default .* is not number"):
            parse_manifest(_write_toml(tmp_path, body))

    def test_empty_enum_rejected(self, tmp_path: Path) -> None:
        body = (
            _flat()
            + '[settings_schema.mode]\ntype = "string"\ndefault = "fast"\nenum = []\n'
        )
        with pytest.raises(ValidationError, match="enum must be a non-empty list"):
            parse_manifest(_write_toml(tmp_path, body))

    def test_default_not_in_enum_rejected(self, tmp_path: Path) -> None:
        body = (
            _flat()
            + '[settings_schema.mode]\ntype = "string"\ndefault = "warp"\n'
            + 'enum = ["fast", "slow"]\n'
        )
        with pytest.raises(ValidationError, match="not in enum"):
            parse_manifest(_write_toml(tmp_path, body))
