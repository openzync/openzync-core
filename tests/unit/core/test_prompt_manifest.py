"""Unit tests for prompt template manifest loader (``core.prompt_manifest``).

Covers:
- ``load_manifest``: loads YAML, returns ``PromptManifest``
- ``PromptManifest``: lookup helpers (by_name, by_type, defaults)
- ``get_template_text``: reads template files from disk
- Missing manifest → ``FileNotFoundError``
- Malformed YAML → ``yaml.YAMLError``
- Missing template file → ``FileNotFoundError``
- Cache invalidation
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from core.prompt_manifest import (
    MANIFEST_FILENAME,
    PROMPTS_DIR,
    PromptManifest,
    invalidate_manifest_cache,
    load_manifest,
)


# Path.parent is a readonly property — can't patch.object it.
# Instead we patch PROMPTS_DIR itself (the whole Path), which is mutable
# at the module attribute level.


SAMPLE_MANIFEST_YAML = """
version: 2
templates:
  - name: extract_facts_v4
    type: fact_extraction
    file: extract_facts_v4.jinja2
    description: Extract structured facts from conversation text.
    is_default_for_type: true

  - name: extract_facts_v3
    type: fact_extraction
    file: extract_facts_v3.jinja2
    description: Legacy fact extraction (v3).

  - name: summarize_conversation
    type: conversation_summary
    file: summarize.jinja2
    description: Summarise a conversation into a concise paragraph.
    is_default_for_type: true

  - name: classify_intent
    type: intent_classification
    file: classify_intent.jinja2
    description: Classify user intent.
"""


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Clear the module-level cache before and after each test."""
    invalidate_manifest_cache()
    yield
    invalidate_manifest_cache()


@pytest.fixture
def manifest_path(tmp_path: Path) -> Path:
    """Create a temporary ``PROMPTS_DIR`` with manifest and template files.

    Returns the ``prompts`` directory path.  Tests should patch
    ``core.prompt_manifest.PROMPTS_DIR`` with this value.
    """
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    manifest = prompts_dir / MANIFEST_FILENAME
    manifest.write_text(SAMPLE_MANIFEST_YAML)

    (prompts_dir / "extract_facts_v4.jinja2").write_text(
        "Extract facts from: {{ text }}",
    )
    (prompts_dir / "extract_facts_v3.jinja2").write_text(
        "Legacy extraction: {{ text }}",
    )
    (prompts_dir / "summarize.jinja2").write_text(
        "Summarize: {{ conversation }}",
    )
    (prompts_dir / "classify_intent.jinja2").write_text(
        "Classify: {{ input }}",
    )

    return prompts_dir


@pytest.mark.unit
class TestLoadManifest:
    """Loading the manifest from disk."""

    def test_loads_manifest_successfully(self, manifest_path: Path) -> None:
        """A valid manifest file is parsed and returned as a ``PromptManifest``."""
        with patch("core.prompt_manifest.PROMPTS_DIR", manifest_path):
            manifest = load_manifest()
            assert isinstance(manifest, PromptManifest)
            assert manifest.version == 2
            assert len(manifest.templates) == 4

    def test_reload_flag_bypasses_cache(self, manifest_path: Path) -> None:
        """Passing ``reload=True`` forces a re-read from disk."""
        with patch("core.prompt_manifest.PROMPTS_DIR", manifest_path):
            m1 = load_manifest()
            m2 = load_manifest(reload=True)
            assert m1.version == 2
            assert m2.version == 2

    def test_manifest_not_found_raises(self) -> None:
        """If ``manifest.yaml`` does not exist, ``FileNotFoundError`` is raised."""
        with patch("core.prompt_manifest.PROMPTS_DIR", Path("/nonexistent/path")):
            with pytest.raises(FileNotFoundError, match="Prompt manifest not found"):
                load_manifest()

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        """Malformed YAML content raises ``yaml.YAMLError``."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        manifest = prompts_dir / MANIFEST_FILENAME
        manifest.write_text("{invalid: yaml: broken [[[")

        with patch("core.prompt_manifest.PROMPTS_DIR", prompts_dir):
            with pytest.raises(yaml.YAMLError):
                load_manifest()

    def test_empty_manifest_creates_empty_prompt_manifest(self, tmp_path: Path) -> None:
        """An empty manifest (``{}``) creates a ``PromptManifest`` with no templates."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        manifest = prompts_dir / MANIFEST_FILENAME
        manifest.write_text("{}")

        with patch("core.prompt_manifest.PROMPTS_DIR", prompts_dir):
            manifest = load_manifest()
            assert manifest.version == 1  # default
            assert manifest.templates == []

    def test_caching_returns_same_object(self, manifest_path: Path) -> None:
        """Multiple calls return the same cached object (no ``reload``)."""
        with patch("core.prompt_manifest.PROMPTS_DIR", manifest_path):
            m1 = load_manifest()
            m2 = load_manifest()
            assert m1 is m2  # same cached instance


@pytest.mark.unit
class TestPromptManifestClass:
    """``PromptManifest`` data class — lookup helpers."""

    @pytest.fixture
    def manifest(self, manifest_path: Path) -> PromptManifest:
        with patch("core.prompt_manifest.PROMPTS_DIR", manifest_path):
            return load_manifest()

    def test_get_by_name_found(self, manifest: PromptManifest) -> None:
        """``get_by_name`` returns the entry for an existing template name."""
        entry = manifest.get_by_name("extract_facts_v4")
        assert entry is not None
        assert entry["name"] == "extract_facts_v4"
        assert entry["type"] == "fact_extraction"

    def test_get_by_name_not_found(self, manifest: PromptManifest) -> None:
        """``get_by_name`` returns ``None`` for a non-existent name."""
        assert manifest.get_by_name("nonexistent") is None

    def test_get_default_for_type_found(self, manifest: PromptManifest) -> None:
        """``get_default_for_type`` returns the entry marked as default."""
        entry = manifest.get_default_for_type("fact_extraction")
        assert entry is not None
        assert entry["name"] == "extract_facts_v4"

    def test_get_default_for_type_not_found(self, manifest: PromptManifest) -> None:
        """``get_default_for_type`` returns ``None`` when no default exists."""
        entry = manifest.get_default_for_type("nonexistent_type")
        assert entry is None

    def test_get_default_for_type_no_default_marked(self) -> None:
        """When no entry has ``is_default_for_type``, ``None`` is returned."""
        data = {
            "version": 1,
            "templates": [
                {"name": "t1", "type": "test", "file": "t1.jinja2"},
                {"name": "t2", "type": "test", "file": "t2.jinja2"},
            ],
        }
        pm = PromptManifest(data)
        entry = pm.get_default_for_type("test")
        assert entry is None

    def test_get_default_names(self, manifest: PromptManifest) -> None:
        """``get_default_names`` returns names of all type-default entries."""
        names = manifest.get_default_names()
        assert "extract_facts_v4" in names
        assert "summarize_conversation" in names
        assert "classify_intent" not in names

    def test_get_default_names_empty(self) -> None:
        """``get_default_names`` returns empty list when no defaults exist."""
        pm = PromptManifest({"version": 1, "templates": []})
        assert pm.get_default_names() == []

    def test_by_name_lookup_is_case_sensitive(self) -> None:
        """Template name lookup is case-sensitive."""
        data = {
            "version": 1,
            "templates": [{"name": "MyTemplate", "type": "test", "file": "t.jinja2"}],
        }
        pm = PromptManifest(data)
        assert pm.get_by_name("MyTemplate") is not None
        assert pm.get_by_name("mytemplate") is None

    def test_by_type_groups_templates(self, manifest: PromptManifest) -> None:
        """Templates of the same type are grouped via ``by_type``."""
        assert len(manifest.by_type["fact_extraction"]) == 2
        assert len(manifest.by_type["conversation_summary"]) == 1
        assert len(manifest.by_type["intent_classification"]) == 1


@pytest.mark.unit
class TestGetTemplateText:
    """Reading template file contents from disk."""

    def test_reads_template_text(self, manifest_path: Path) -> None:
        """``get_template_text`` reads the template file from the prompts directory."""
        with patch("core.prompt_manifest.PROMPTS_DIR", manifest_path):
            manifest = load_manifest()
            text = manifest.get_template_text("extract_facts_v4.jinja2")
            assert "Extract facts from:" in text

    def test_missing_template_file_raises(self, manifest_path: Path) -> None:
        """If a referenced template file does not exist, ``FileNotFoundError`` is raised."""
        with patch("core.prompt_manifest.PROMPTS_DIR", manifest_path):
            manifest = load_manifest()
            with pytest.raises(FileNotFoundError, match="not found"):
                manifest.get_template_text("nonexistent.jinja2")

    def test_path_traversal_raises(self, manifest_path: Path) -> None:
        """A path with ``..`` still raises when the file doesn't exist (no traversal leak)."""
        with patch("core.prompt_manifest.PROMPTS_DIR", manifest_path):
            manifest = load_manifest()
            with pytest.raises(FileNotFoundError):
                manifest.get_template_text("../../../etc/passwd")

    def test_reads_different_template(self, manifest_path: Path) -> None:
        """Reading a different template returns its correct content."""
        with patch("core.prompt_manifest.PROMPTS_DIR", manifest_path):
            manifest = load_manifest()
            text = manifest.get_template_text("summarize.jinja2")
            assert "Summarize:" in text


@pytest.mark.unit
class TestInvalidateCache:
    """Cache invalidation behavior."""

    def test_invalidate_clears_cache(self, manifest_path: Path) -> None:
        """After invalidation, the next load re-reads from disk."""
        with patch("core.prompt_manifest.PROMPTS_DIR", manifest_path):
            m1 = load_manifest()
            invalidate_manifest_cache()
            m2 = load_manifest()
            assert m1 is not m2  # different object

    def test_invalidate_before_load(self) -> None:
        """Calling ``invalidate_manifest_cache`` before any load is safe."""
        invalidate_manifest_cache()

    def test_invalidate_twice_is_safe(self) -> None:
        """Calling ``invalidate_manifest_cache`` twice is a no-op."""
        invalidate_manifest_cache()
        invalidate_manifest_cache()


@pytest.mark.unit
class TestPromptManifestDefaults:
    """Default behavior when YAML is partially complete."""

    def test_version_defaults_to_1(self) -> None:
        """When version is missing, it defaults to 1."""
        pm = PromptManifest({"templates": []})
        assert pm.version == 1

    def test_templates_defaults_to_empty_list(self) -> None:
        """When templates key is missing, it defaults to empty list."""
        pm = PromptManifest({"version": 2})
        assert pm.templates == []
        assert pm.by_name == {}
        assert pm.by_type == {}

    def test_skips_template_without_name(self) -> None:
        """A template entry without a ``name`` key is skipped in ``by_name``."""
        data = {
            "version": 1,
            "templates": [
                {"type": "test", "file": "no_name.jinja2"},
                {"name": "valid", "type": "test", "file": "valid.jinja2"},
            ],
        }
        pm = PromptManifest(data)
        assert "valid" in pm.by_name
        assert len(pm.by_name) == 1

    def test_skips_template_without_type_in_by_type(self) -> None:
        """A template entry without a ``type`` key is skipped in ``by_type``."""
        data = {
            "version": 1,
            "templates": [
                {"name": "no_type", "file": "t.jinja2"},
                {"name": "typed", "type": "test", "file": "t.jinja2"},
            ],
        }
        pm = PromptManifest(data)
        assert "test" in pm.by_type
        assert len(pm.by_type["test"]) == 1
