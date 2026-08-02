"""Unit tests for PromptTemplateRepository — org-scoped template versioning."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.prompt_template_repository import PromptTemplateRepository


pytestmark = pytest.mark.unit


class TestPromptTemplateRepository:
    """PromptTemplateRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def repo(self, mock_db: AsyncMock) -> PromptTemplateRepository:
        return PromptTemplateRepository(db=mock_db)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _mock_template(self, **overrides: object) -> MagicMock:
        t = MagicMock()
        t.id = overrides.get("id", uuid4())
        t.organization_id = overrides.get("organization_id", self.ORG_ID)
        t.template_name = overrides.get("template_name", "test_template")
        t.template_text = overrides.get(
            "template_text", "Extract facts from: {{text}}"
        )
        t.version = overrides.get("version", 1)
        t.description = overrides.get("description", "A test template")
        t.type = overrides.get("type", "fact_extraction")
        t.is_active = overrides.get("is_active", True)
        t.is_default_for_type = overrides.get("is_default_for_type", False)
        t.updated_at = overrides.get("updated_at", None)
        return t

    # ── get_active ─────────────────────────────────────────────────────────────

    async def test_get_active_found(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """get_active returns active template when found."""
        tmpl = self._mock_template()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = tmpl
        mock_db.execute.return_value = mock_result

        result = await repo.get_active(
            org_id=self.ORG_ID, template_name="test_template"
        )

        assert result == tmpl

    async def test_get_active_not_found(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """get_active returns None when no active template."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_active(
            org_id=self.ORG_ID, template_name="nonexistent"
        )

        assert result is None

    # ── get_active_by_type ─────────────────────────────────────────────────────

    async def test_get_active_by_type_found(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """get_active_by_type returns the type default when found."""
        tmpl = self._mock_template(is_default_for_type=True)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = tmpl
        mock_db.execute.return_value = mock_result

        result = await repo.get_active_by_type(
            org_id=self.ORG_ID, type="fact_extraction"
        )

        assert result == tmpl

    async def test_get_active_by_type_not_found(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """get_active_by_type returns None when no default."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_active_by_type(
            org_id=self.ORG_ID, type="unknown_type"
        )

        assert result is None

    # ── get_version ────────────────────────────────────────────────────────────

    async def test_get_version_found(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """get_version returns exact version when found."""
        tmpl = self._mock_template(version=3)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = tmpl
        mock_db.execute.return_value = mock_result

        result = await repo.get_version(
            org_id=self.ORG_ID, name="test_template", version=3
        )

        assert result == tmpl

    async def test_get_version_not_found(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """get_version returns None when version does not exist."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_version(
            org_id=self.ORG_ID, name="test_template", version=99
        )

        assert result is None

    # ── set_as_type_default ────────────────────────────────────────────────────

    async def test_set_as_type_default(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """set_as_type_default promotes template and demotes others."""
        tmpl = self._mock_template(is_default_for_type=False, type="fact_extraction")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = tmpl
        mock_db.execute.return_value = mock_result
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.set_as_type_default(
            org_id=self.ORG_ID, name="test_template"
        )

        assert result is not None
        assert result.is_default_for_type is True
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_set_as_type_default_template_not_found(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """set_as_type_default raises ValueError when template missing."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with pytest.raises(ValueError, match="Template .* not found"):
            await repo.set_as_type_default(
                org_id=self.ORG_ID, name="nonexistent"
            )

    async def test_set_as_type_default_no_type(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """set_as_type_default raises ValueError when template has no type."""
        tmpl = self._mock_template(type=None)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = tmpl
        mock_db.execute.return_value = mock_result

        with pytest.raises(ValueError, match="no type assigned"):
            await repo.set_as_type_default(
                org_id=self.ORG_ID, name="test_template"
            )

    # ── set_for_org (create new version) ───────────────────────────────────────

    async def test_set_for_org_creates_new_version(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """set_for_org creates a new version and deactivates prior ones."""
        # First query: old active template exists
        old_tmpl = self._mock_template(
            version=2, is_default_for_type=True, type="fact_extraction"
        )
        result_active = MagicMock()
        result_active.scalar_one_or_none.return_value = old_tmpl
        # Second query: max version = 2
        result_max = MagicMock()
        result_max.scalar.return_value = 2

        mock_db.execute.side_effect = [result_active, result_max, result_max]
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.set_for_org(
            org_id=self.ORG_ID,
            name="test_template",
            text="New template text",
            desc="Updated",
            template_type="fact_extraction",
        )

        assert result is not None
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()
        # New version carries forward is_default_for_type
        assert result.is_default_for_type is True

    async def test_set_for_org_no_prior_version(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """set_for_org creates version 1 when no prior versions exist."""
        result_none = MagicMock()
        result_none.scalar_one_or_none.return_value = None
        result_zero = MagicMock()
        result_zero.scalar.return_value = 0

        mock_db.execute.side_effect = [result_none, result_zero, result_zero]
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.set_for_org(
            org_id=self.ORG_ID,
            name="new_template",
            text="Brand new",
        )

        assert result is not None

    # ── rollback ───────────────────────────────────────────────────────────────

    async def test_rollback_creates_new_version_from_old(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """rollback creates a new version whose text matches a prior version."""
        target = self._mock_template(
            version=1, template_text="Original text", type="fact_extraction"
        )
        old_active = self._mock_template(
            version=3, is_default_for_type=True, type="fact_extraction"
        )

        result_target = MagicMock()
        result_target.scalar_one_or_none.return_value = target
        result_active = MagicMock()
        result_active.scalar_one_or_none.return_value = old_active
        result_max = MagicMock()
        result_max.scalar.return_value = 3

        mock_db.execute.side_effect = [
            result_target,
            result_active,
            MagicMock(),  # update statement (deactivate old versions)
            result_max,
        ]
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.rollback(
            org_id=self.ORG_ID, name="test_template", version=1
        )

        assert result is not None
        assert result.is_default_for_type is True

    async def test_rollback_target_not_found(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """rollback raises ValueError when target version does not exist."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with pytest.raises(ValueError, match="Version 99"):
            await repo.rollback(
                org_id=self.ORG_ID, name="test_template", version=99
            )

    # ── delete_for_org ─────────────────────────────────────────────────────────

    async def test_delete_for_org(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """delete_for_org removes all versions of a template."""
        mock_db.execute.return_value = MagicMock()
        mock_db.flush.return_value = None

        await repo.delete_for_org(
            org_id=self.ORG_ID, name="test_template"
        )

        mock_db.execute.assert_awaited_once()
        mock_db.flush.assert_awaited_once()

    # ── seed_default_prompts ───────────────────────────────────────────────────

    async def test_seed_default_prompts(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """seed_default_prompts inserts manifest defaults into org."""
        # Mock manifest entries
        manifest_templates = [
            {
                "name": "extract_facts",
                "file": "extract_facts.jinja2",
                "type": "fact_extraction",
                "is_default_for_type": True,
                "description": "Extract facts",
            },
            {
                "name": "classify_dialog",
                "file": "classify.jinja2",
                "type": "dialog_classification",
                "is_default_for_type": True,
                "description": "Classify dialog",
            },
            {
                "name": "legacy_extrakt",
                "file": "legacy.jinja2",
                "type": "fact_extraction",
                "is_default_for_type": False,
                "description": "Legacy (skipped)",
            },
        ]
        # No existing templates for this org
        result_none = MagicMock()
        result_none.scalar_one_or_none.return_value = None

        mock_db.execute.return_value = result_none
        mock_db.add.return_value = None
        mock_db.flush.return_value = None

        with patch(
            "repositories.prompt_template_repository.load_manifest"
        ) as mock_load:
            manifest = MagicMock()
            manifest.templates = manifest_templates
            manifest.get_template_text.return_value = "template body"
            mock_load.return_value = manifest

            count = await repo.seed_default_prompts(org_id=self.ORG_ID)

        assert count == 2  # Only is_default_for_type entries
        assert mock_db.add.call_count == 2
        mock_db.flush.assert_awaited_once()

    async def test_seed_default_prompts_skips_existing(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """seed_default_prompts skips templates the org already has."""
        manifest_templates = [
            {
                "name": "extract_facts",
                "file": "extract_facts.jinja2",
                "type": "fact_extraction",
                "is_default_for_type": True,
            },
        ]
        # Already has this template
        existing = MagicMock()
        existing.scalar_one_or_none.return_value = self._mock_template()

        mock_db.execute.return_value = existing

        with patch(
            "repositories.prompt_template_repository.load_manifest"
        ) as mock_load:
            manifest = MagicMock()
            manifest.templates = manifest_templates
            mock_load.return_value = manifest

            count = await repo.seed_default_prompts(org_id=self.ORG_ID)

        assert count == 0
        mock_db.add.assert_not_called()

    # ── import_system_template ─────────────────────────────────────────────────

    async def test_import_system_template(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """import_system_template imports a manifest template into org scope."""
        result_none = MagicMock()
        result_none.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = result_none
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        with patch(
            "repositories.prompt_template_repository.load_manifest"
        ) as mock_load:
            manifest = MagicMock()
            manifest.get_by_name.return_value = {
                "name": "extract_facts_v4",
                "file": "extract_facts_v4.jinja2",
                "type": "fact_extraction",
                "is_default_for_type": False,
                "description": "Version 4",
            }
            manifest.get_template_text.return_value = "v4 template body"
            manifest.by_name = {"extract_facts_v4": {}}
            mock_load.return_value = manifest

            result = await repo.import_system_template(
                org_id=self.ORG_ID, template_name="extract_facts_v4"
            )

        assert result is not None
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_import_system_template_already_exists(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """import_system_template raises ValueError when org already has it."""
        existing = MagicMock()
        existing.scalar_one_or_none.return_value = self._mock_template()
        mock_db.execute.return_value = existing

        with patch(
            "repositories.prompt_template_repository.load_manifest"
        ) as mock_load:
            manifest = MagicMock()
            manifest.get_by_name.return_value = {"name": "extract_facts_v4"}
            manifest.by_name = {"extract_facts_v4": {}}
            mock_load.return_value = manifest

            with pytest.raises(ValueError, match="already imported"):
                await repo.import_system_template(
                    org_id=self.ORG_ID, template_name="extract_facts_v4"
                )

    async def test_import_system_template_not_in_manifest(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """import_system_template raises ValueError when not in manifest."""
        with patch(
            "repositories.prompt_template_repository.load_manifest"
        ) as mock_load:
            manifest = MagicMock()
            manifest.get_by_name.return_value = None
            manifest.by_name = {}
            mock_load.return_value = manifest

            with pytest.raises(ValueError, match="No manifest entry"):
                await repo.import_system_template(
                    org_id=self.ORG_ID, template_name="nonexistent"
                )

    # ── list_system_grouped ────────────────────────────────────────────────────

    async def test_list_system_grouped(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """list_system_grouped returns manifest templates grouped by type."""
        manifest_templates = [
            {
                "name": "extract_facts",
                "file": "extract_facts.jinja2",
                "type": "fact_extraction",
                "is_default_for_type": True,
                "description": "Extract facts",
            },
            {
                "name": "classify_dialog",
                "file": "classify.jinja2",
                "type": "dialog_classification",
                "is_default_for_type": True,
                "description": "Classify dialog",
            },
        ]
        mock_db.execute.return_value = MagicMock()
        mock_db.execute.return_value.fetchall.return_value = [
            ("extract_facts",),
        ]

        with patch(
            "repositories.prompt_template_repository.load_manifest"
        ) as mock_load:
            manifest = MagicMock()
            manifest.templates = manifest_templates
            mock_load.return_value = manifest

            groups = await repo.list_system_grouped(org_id=self.ORG_ID)

        assert len(groups) == 2
        assert groups[0]["type"] == "dialog_classification"
        assert groups[1]["type"] == "fact_extraction"
        assert groups[1]["imported"] == ["extract_facts"]

    # ── list_names ─────────────────────────────────────────────────────────────

    async def test_list_names(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """list_names returns distinct template names with latest version."""
        row_1 = MagicMock()
        row_1.template_name = "tmpl_a"
        row_1.version = 3
        row_1.description = "Desc A"
        row_1.type = "fact_extraction"
        row_1.is_default_for_type = True
        row_1.updated_at = None

        row_2 = MagicMock()
        row_2.template_name = "tmpl_b"
        row_2.version = 1
        row_2.description = "Desc B"
        row_2.type = "dialog_classification"
        row_2.is_default_for_type = True
        row_2.updated_at = None

        mock_db.execute.return_value = MagicMock()
        mock_db.execute.return_value.all.return_value = [row_1, row_2]

        names = await repo.list_names(org_id=self.ORG_ID)

        assert len(names) == 2
        assert names[0]["name"] == "tmpl_a"
        assert names[1]["name"] == "tmpl_b"
        assert names[0]["is_customised"] is True

    async def test_list_names_empty(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """list_names returns empty list when org has no templates."""
        mock_db.execute.return_value = MagicMock()
        mock_db.execute.return_value.all.return_value = []

        names = await repo.list_names(org_id=self.ORG_ID)

        assert names == []

    # ── list_versions ──────────────────────────────────────────────────────────

    async def test_list_versions(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """list_versions returns all versions ordered by version desc."""
        versions = [
            self._mock_template(version=3),
            self._mock_template(version=2),
            self._mock_template(version=1),
        ]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = versions
        mock_db.execute.return_value = mock_result

        result = await repo.list_versions(
            org_id=self.ORG_ID, name="test_template"
        )

        assert result == versions

    async def test_list_versions_empty(
        self, repo: PromptTemplateRepository, mock_db: AsyncMock
    ) -> None:
        """list_versions returns empty list when no versions."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.list_versions(
            org_id=self.ORG_ID, name="nonexistent"
        )

        assert result == []
