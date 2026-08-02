"""Unit tests for OrganizationService — bootstrap flow with OpenBao seeding.

Model classes (Organization, Project, ApiKey) and the PromptTemplateRepository
are patched at module level. The ``_load_org_defaults`` helper is tested
directly — pure file I/O with error handling.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
import yaml

from schemas.organizations import CreateOrgRequest, CreateOrgResponse
from services.organization_service import OrganizationService


@pytest.mark.unit
class TestOrganizationService:
    """Unit tests for ``OrganizationService`` — org creation and defaults loading."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000010")
    RAW_API_KEY = "oz_live_testkey123"

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_payload(
        self, name: str = "Test Org", plan: str = "free"
    ) -> CreateOrgRequest:
        """Build a ``CreateOrgRequest`` for test usage."""
        return CreateOrgRequest(name=name, plan=plan)

    def _make_service(
        self, bao_client: AsyncMock | None = None
    ) -> tuple[OrganizationService, AsyncMock, AsyncMock]:
        """Create ``OrganizationService`` with mocked repo and optional
        ``bao_client``.

        The mock repo provides a ``session`` property required by the
        service's ``__init__``.
        """
        mock_repo = AsyncMock()
        mock_db = AsyncMock()
        mock_repo.session = mock_db

        service = OrganizationService(
            repo=mock_repo, bao_client=bao_client
        )
        return service, mock_repo, mock_db

    def _make_org_mock(self) -> MagicMock:
        """Build a MagicMock mimicking an Organization ORM instance."""
        org = MagicMock()
        org.id = self.ORG_ID
        org.name = "Test Org"
        org.plan = "free"
        return org

    def _make_project_mock(self) -> MagicMock:
        """Build a MagicMock mimicking a Project ORM instance."""
        proj = MagicMock()
        proj.id = self.PROJECT_ID
        proj.organization_id = self.ORG_ID
        proj.name = "Test Org - Default"
        return proj

    # ── create_organization ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_create_organization_creates_org_project_api_key(
        self,
    ) -> None:
        """``create_organization`` creates Organization, Project, and ApiKey
        records and returns a ``CreateOrgResponse``."""
        service, mock_repo, mock_db = self._make_service()
        payload = self._make_payload()

        org_mock = self._make_org_mock()
        project_mock = self._make_project_mock()

        with (
            patch("services.organization_service.Organization") as mock_org_cls,
            patch("services.organization_service.Project") as mock_proj_cls,
            patch("services.organization_service.ApiKey") as mock_key_cls,
            patch(
                "repositories.prompt_template_repository.PromptTemplateRepository",
            ) as mock_pt_repo_cls,
            patch(
                "services.organization_service.generate_api_key",
                return_value=self.RAW_API_KEY,
            ),
            patch(
                "services.organization_service.hash_api_key",
                return_value=("hashed_key", "salted_salt"),
            ),
            patch(
                "services.organization_service.compute_lookup_hash",
                return_value="lookup_abc",
            ),
        ):
            mock_org_cls.return_value = org_mock
            mock_proj_cls.return_value = project_mock
            mock_key_cls.return_value = MagicMock()

            mock_pt_repo = AsyncMock()
            mock_pt_repo.seed_default_prompts.return_value = 5
            mock_pt_repo_cls.return_value = mock_pt_repo

            result = await service.create_organization(payload)

        assert isinstance(result, CreateOrgResponse)
        assert result.organization_id == self.ORG_ID
        assert result.organization_name == "Test Org"
        assert result.api_key == self.RAW_API_KEY
        assert result.api_key_name == "default"

        # Verify model constructors were called
        mock_org_cls.assert_called_once_with(
            name="Test Org", plan="free"
        )
        mock_proj_cls.assert_called_once_with(
            organization_id=self.ORG_ID,
            name="Test Org - Default",
        )
        mock_key_cls.assert_called_once_with(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            key_hash="hashed_key",
            lookup_hash="lookup_abc",
            salt="salted_salt",
            prefix="oz_live_",
            name="default",
            scopes=["read", "write", "admin"],
            is_revoked=False,
        )

        # Verify flush/refresh calls on the DB session
        assert mock_db.add.call_count == 3
        assert mock_db.flush.await_count == 3
        assert mock_db.refresh.await_count == 2

        # Verify transaction commit
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_organization_seeds_default_prompts(self) -> None:
        """``create_organization`` seeds default prompt templates via
        ``PromptTemplateRepository``."""
        service, mock_repo, mock_db = self._make_service()
        payload = self._make_payload()

        org_mock = self._make_org_mock()
        project_mock = self._make_project_mock()

        with (
            patch("services.organization_service.Organization") as mock_org_cls,
            patch("services.organization_service.Project") as mock_proj_cls,
            patch("services.organization_service.ApiKey"),
            patch(
                "repositories.prompt_template_repository.PromptTemplateRepository",
            ) as mock_pt_repo_cls,
            patch("services.organization_service.generate_api_key",
                  return_value=self.RAW_API_KEY),
            patch("services.organization_service.hash_api_key",
                  return_value=("hashed_key", "salted_salt")),
            patch("services.organization_service.compute_lookup_hash",
                  return_value="lookup_abc"),
        ):
            mock_org_cls.return_value = org_mock
            mock_proj_cls.return_value = project_mock

            mock_pt_repo = AsyncMock()
            mock_pt_repo.seed_default_prompts.return_value = 5
            mock_pt_repo_cls.return_value = mock_pt_repo

            result = await service.create_organization(payload)

        assert result.organization_id == self.ORG_ID
        mock_pt_repo_cls.assert_called_once_with(mock_db)
        mock_pt_repo.seed_default_prompts.assert_awaited_once_with(
            self.ORG_ID
        )

    @pytest.mark.asyncio
    async def test_create_organization_with_bao_client_bootstraps_namespace(
        self,
    ) -> None:
        """``create_organization`` bootstraps the OpenBao namespace and writes
        org defaults when ``bao_client`` is provided."""
        mock_bao = AsyncMock()
        service, mock_repo, mock_db = self._make_service(bao_client=mock_bao)
        payload = self._make_payload()

        org_mock = self._make_org_mock()
        project_mock = self._make_project_mock()

        with (
            patch("services.organization_service.Organization") as mock_org_cls,
            patch("services.organization_service.Project") as mock_proj_cls,
            patch("services.organization_service.ApiKey"),
            patch(
                "repositories.prompt_template_repository.PromptTemplateRepository",
            ) as mock_pt_repo_cls,
            patch("services.organization_service.generate_api_key",
                  return_value=self.RAW_API_KEY),
            patch("services.organization_service.hash_api_key",
                  return_value=("hashed_key", "salted_salt")),
            patch("services.organization_service.compute_lookup_hash",
                  return_value="lookup_abc"),
            patch.object(
                service,
                "_load_org_defaults",
                return_value={"max_sessions": 100},
            ),
        ):
            mock_org_cls.return_value = org_mock
            mock_proj_cls.return_value = project_mock

            mock_pt_repo = AsyncMock()
            mock_pt_repo.seed_default_prompts.return_value = 0
            mock_pt_repo_cls.return_value = mock_pt_repo

            result = await service.create_organization(payload)

        assert result.organization_id == self.ORG_ID
        mock_bao.create_org_namespace.assert_awaited_once_with(self.ORG_ID)
        mock_bao.write_org_config.assert_awaited_once_with(
            self.ORG_ID, {"max_sessions": 100}
        )

    @pytest.mark.asyncio
    async def test_create_organization_bao_failure_is_non_fatal(
        self,
    ) -> None:
        """``create_organization`` logs the OpenBao bootstrap failure but
        still returns a successful ``CreateOrgResponse``."""
        mock_bao = AsyncMock()
        mock_bao.create_org_namespace.side_effect = RuntimeError(
            "OpenBao unreachable"
        )
        service, mock_repo, mock_db = self._make_service(bao_client=mock_bao)
        payload = self._make_payload()

        org_mock = self._make_org_mock()
        project_mock = self._make_project_mock()

        with (
            patch("services.organization_service.Organization") as mock_org_cls,
            patch("services.organization_service.Project") as mock_proj_cls,
            patch("services.organization_service.ApiKey"),
            patch(
                "repositories.prompt_template_repository.PromptTemplateRepository",
            ) as mock_pt_repo_cls,
            patch("services.organization_service.generate_api_key",
                  return_value=self.RAW_API_KEY),
            patch("services.organization_service.hash_api_key",
                  return_value=("hashed_key", "salted_salt")),
            patch("services.organization_service.compute_lookup_hash",
                  return_value="lookup_abc"),
        ):
            mock_org_cls.return_value = org_mock
            mock_proj_cls.return_value = project_mock

            mock_pt_repo = AsyncMock()
            mock_pt_repo.seed_default_prompts.return_value = 0
            mock_pt_repo_cls.return_value = mock_pt_repo

            result = await service.create_organization(payload)

        assert isinstance(result, CreateOrgResponse)
        assert result.organization_id == self.ORG_ID
        assert result.api_key == self.RAW_API_KEY

    # ── _load_org_defaults ───────────────────────────────────────────────────

    def test_load_org_defaults_returns_yaml_content(self) -> None:
        """``_load_org_defaults`` reads and parses the YAML file,
        returning its contents as a dict."""
        service, _, _ = self._make_service()

        with (
            patch("builtins.open", new=MagicMock()) as mock_open,
            patch(
                "services.organization_service.yaml.safe_load",
                return_value={"max_sessions": 100, "llm_model": "gpt-4"},
            ),
        ):
            result = service._load_org_defaults()

        assert isinstance(result, dict)
        assert result == {"max_sessions": 100, "llm_model": "gpt-4"}

    def test_load_org_defaults_file_not_found_returns_empty(
        self,
    ) -> None:
        """``_load_org_defaults`` returns ``{}`` when the YAML file does
        not exist."""
        service, _, _ = self._make_service()

        with patch("builtins.open", side_effect=FileNotFoundError):
            result = service._load_org_defaults()

        assert result == {}

    def test_load_org_defaults_yaml_error_returns_empty(self) -> None:
        """``_load_org_defaults`` returns ``{}`` when the YAML file is
        malformed."""
        service, _, _ = self._make_service()

        with (
            patch("builtins.open", new=MagicMock()),
            patch(
                "services.organization_service.yaml.safe_load",
                side_effect=yaml.YAMLError("bad yaml"),
            ),
        ):
            result = service._load_org_defaults()

        assert result == {}
