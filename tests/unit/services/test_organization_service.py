"""Unit tests for OrganizationService — bootstrap flow with OpenBao seeding.

Model classes (Organization, Project) and the PromptTemplateRepository
are patched at module level. The ``_load_org_defaults`` helper is tested
directly — pure file I/O with error handling.

New contract (ADR): ``create_organization`` no longer creates an
``ApiKey`` or a default ``Project``; ``CreateOrgResponse`` carries only
the org ID and name.
"""
from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
import yaml

from schemas.organizations import CreateOrgRequest, CreateOrgResponse
from services.organization_service import OrganizationService, OrgCodeInfo


@pytest.mark.unit
class TestOrganizationService:
    """Unit tests for ``OrganizationService`` — org creation and defaults loading."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000010")

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
        org.org_code = "K7M2Q9X4"
        org.join_enabled = True
        return org

    # ── create_organization ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_create_organization_creates_org_only(
        self,
    ) -> None:
        """``create_organization`` creates an Organization record only.

        No default ``Project`` and no ``ApiKey`` are created, and the
        response carries no API key material.
        """
        service, mock_repo, mock_db = self._make_service()
        payload = self._make_payload()

        org_mock = self._make_org_mock()

        with (
            patch("services.organization_service.Organization") as mock_org_cls,
            # Project is no longer imported by the service — create=True lets
            # us assert the NEW contract: it is never constructed.
            patch("services.organization_service.Project", create=True) as mock_proj_cls,
            patch(
                "repositories.prompt_template_repository.PromptTemplateRepository",
            ) as mock_pt_repo_cls,
        ):
            mock_org_cls.return_value = org_mock

            mock_pt_repo = AsyncMock()
            mock_pt_repo.seed_default_prompts.return_value = 5
            mock_pt_repo_cls.return_value = mock_pt_repo

            result = await service.create_organization(payload)

        assert isinstance(result, CreateOrgResponse)
        assert result.organization_id == self.ORG_ID
        assert result.organization_name == "Test Org"
        # New contract: the response exposes no API key material.
        assert "api_key" not in CreateOrgResponse.model_fields
        assert result.model_dump() == {
            "organization_id": self.ORG_ID,
            "organization_name": "Test Org",
        }

        # Verify only the Organization constructor was called — no default
        # project and no API key are auto-created.  ``org_code`` is a freshly
        # generated join code (feature: org-code join flow).
        mock_org_cls.assert_called_once_with(
            name="Test Org", plan="free", org_code=ANY,
        )
        mock_proj_cls.assert_not_called()

        # Verify the generated org code conforms to the code alphabet/length.
        generated = mock_org_cls.call_args.kwargs["org_code"]
        from core.org_codes import ORG_CODE_ALPHABET, ORG_CODE_LENGTH

        assert len(generated) == ORG_CODE_LENGTH
        assert all(ch in ORG_CODE_ALPHABET for ch in generated)

        # Verify flush/refresh calls on the DB session (single entity)
        assert mock_db.add.call_count == 1
        assert mock_db.flush.await_count == 1
        assert mock_db.refresh.await_count == 1

        # Verify transaction commit
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_organization_seeds_default_prompts(self) -> None:
        """``create_organization`` seeds default prompt templates via
        ``PromptTemplateRepository``."""
        service, mock_repo, mock_db = self._make_service()
        payload = self._make_payload()

        org_mock = self._make_org_mock()

        with (
            patch("services.organization_service.Organization") as mock_org_cls,
            patch(
                "repositories.prompt_template_repository.PromptTemplateRepository",
            ) as mock_pt_repo_cls,
        ):
            mock_org_cls.return_value = org_mock

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

        with (
            patch("services.organization_service.Organization") as mock_org_cls,
            patch(
                "repositories.prompt_template_repository.PromptTemplateRepository",
            ) as mock_pt_repo_cls,
            patch.object(
                service,
                "_load_org_defaults",
                return_value={"max_sessions": 100},
            ),
        ):
            mock_org_cls.return_value = org_mock

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

        with (
            patch("services.organization_service.Organization") as mock_org_cls,
            patch(
                "repositories.prompt_template_repository.PromptTemplateRepository",
            ) as mock_pt_repo_cls,
        ):
            mock_org_cls.return_value = org_mock

            mock_pt_repo = AsyncMock()
            mock_pt_repo.seed_default_prompts.return_value = 0
            mock_pt_repo_cls.return_value = mock_pt_repo

            result = await service.create_organization(payload)

        assert isinstance(result, CreateOrgResponse)
        assert result.organization_id == self.ORG_ID
        assert "api_key" not in CreateOrgResponse.model_fields

    @pytest.mark.asyncio
    async def test_create_organization_rejects_reserved_system_name(
        self,
    ) -> None:
        """The reserved ``SYSTEM`` name (any case) → ValidationError."""
        from core.exceptions import ValidationError

        service, mock_repo, _ = self._make_service()
        for bad in ("SYSTEM", "system", "System"):
            with pytest.raises(ValidationError):
                await service.create_organization(self._make_payload(name=bad))
        mock_repo.session.add.assert_not_called()

    # ── Org join code (admin management) ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_org_code_returns_org_code_info(self) -> None:
        """``get_org_code`` returns code + ``join_enabled`` as OrgCodeInfo."""
        service, mock_repo, _ = self._make_service()
        org = self._make_org_mock()
        mock_repo.get_by_id.return_value = org

        info = await service.get_org_code(self.ORG_ID)

        assert isinstance(info, OrgCodeInfo)
        assert info.org_code == "K7M2Q9X4"
        assert info.join_enabled is True
        mock_repo.get_by_id.assert_awaited_once_with(self.ORG_ID)

    @pytest.mark.asyncio
    async def test_get_org_code_missing_org_raises_not_found(self) -> None:
        """``get_org_code`` raises NotFoundError when the org is missing."""
        from core.exceptions import NotFoundError

        service, mock_repo, _ = self._make_service()
        mock_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundError):
            await service.get_org_code(self.ORG_ID)

    @pytest.mark.asyncio
    async def test_set_join_enabled_delegates_and_returns_fresh_state(
        self,
    ) -> None:
        """``set_join_enabled`` delegates to the repo and returns OrgCodeInfo."""
        service, mock_repo, _ = self._make_service()
        org = self._make_org_mock()
        org.join_enabled = False
        mock_repo.set_join_enabled.return_value = org

        info = await service.set_join_enabled(self.ORG_ID, False)

        assert isinstance(info, OrgCodeInfo)
        assert info.org_code == "K7M2Q9X4"
        assert info.join_enabled is False
        mock_repo.set_join_enabled.assert_awaited_once_with(self.ORG_ID, False)

    @pytest.mark.asyncio
    async def test_regenerate_org_code_returns_org_code_info(self) -> None:
        """``regenerate_org_code`` rotates the code and returns OrgCodeInfo."""
        service, mock_repo, _ = self._make_service()
        org = self._make_org_mock()
        org.org_code = "ZZZ2Q9X4"
        mock_repo.set_org_code.return_value = org

        with patch(
            "services.organization_service.generate_org_code",
            return_value="ZZZ2Q9X4",
        ):
            info = await service.regenerate_org_code(self.ORG_ID)

        assert isinstance(info, OrgCodeInfo)
        assert info.org_code == "ZZZ2Q9X4"
        assert info.join_enabled is True
        mock_repo.set_org_code.assert_awaited_once_with(
            self.ORG_ID, "ZZZ2Q9X4",
        )

    @pytest.mark.asyncio
    async def test_regenerate_org_code_preserves_join_enabled_false(
        self,
    ) -> None:
        """Regenerate preserves the paused state — rotation does NOT reset
        ``join_enabled``.

        Contract #8: a paused org stays paused after code rotation; the
        response carries the new code AND the existing toggle value.
        """
        service, mock_repo, _ = self._make_service()
        org = self._make_org_mock()
        org.org_code = "ZZZ2Q9X4"
        org.join_enabled = False
        mock_repo.set_org_code.return_value = org

        with patch(
            "services.organization_service.generate_org_code",
            return_value="ZZZ2Q9X4",
        ):
            info = await service.regenerate_org_code(self.ORG_ID)

        assert isinstance(info, OrgCodeInfo)
        assert info.org_code == "ZZZ2Q9X4"
        assert info.join_enabled is False
        mock_repo.set_org_code.assert_awaited_once_with(
            self.ORG_ID, "ZZZ2Q9X4",
        )

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

    # ── Platform superadmin surface: list_all_orgs / list_org_members ───────

    @pytest.mark.asyncio
    async def test_list_all_orgs_delegates_with_filter(
        self,
    ) -> None:
        """list_all_orgs forwards the status filter + pagination to the repo."""
        service, mock_repo, _ = self._make_service()
        mock_repo.list_all.return_value = ([], 0)

        orgs, total = await service.list_all_orgs(
            status="pending", page=2, limit=20
        )

        assert orgs == []
        assert total == 0
        mock_repo.list_all.assert_awaited_once_with(
            status="pending", page=2, limit=20
        )

    @pytest.mark.asyncio
    async def test_list_org_members_returns_users_for_existing_org(
        self,
    ) -> None:
        """list_org_members verifies the org exists, then lists its users."""
        service, mock_repo, _ = self._make_service()
        mock_repo.get_by_id.return_value = self._make_org_mock()
        mock_user_repo = AsyncMock()
        mock_user_repo.list_by_org.return_value = ([MagicMock()], 1)

        with patch(
            "services.organization_service.UserRepository",
            return_value=mock_user_repo,
        ):
            users, total = await service.list_org_members(
                self.ORG_ID, page=1, limit=50
            )

        assert len(users) == 1
        assert total == 1
        mock_repo.get_by_id.assert_awaited_once_with(self.ORG_ID)
        mock_user_repo.list_by_org.assert_awaited_once_with(
            self.ORG_ID, page=1, limit=50
        )

    @pytest.mark.asyncio
    async def test_list_org_members_missing_org_raises_not_found(
        self,
    ) -> None:
        """list_org_members for a nonexistent org → NotFoundError, no query."""
        from core.exceptions import NotFoundError

        service, mock_repo, _ = self._make_service()
        mock_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundError):
            await service.list_org_members(self.ORG_ID)

        mock_repo.get_by_id.assert_awaited_once_with(self.ORG_ID)
