"""Integration tests for under-covered repositories — direct DB coverage.

Exercises four repositories that previously had only partial coverage:
``AuditLogRepository``, ``CustomInstructionRepository``,
``EpisodeBlobRepository``, and ``PromptTemplateRepository``.

Every test uses the ``db_session`` fixture (truncates all tables on
teardown) so no state leaks between tests.  Only pure-DB behaviour is
tested — nothing that requires external services (S3 uploads, LLM calls).
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.audit_log_repository import AuditLogRepository
from repositories.custom_instruction_repository import CustomInstructionRepository
from repositories.episode_blob_repository import EpisodeBlobRepository
from repositories.episode_repository import EpisodeRepository
from repositories.prompt_template_repository import PromptTemplateRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository


pytestmark = pytest.mark.integration


ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")


class TestAuditLogRepository:
    """AuditLogRepository — append-only create + filtered/paginated list."""

    async def _seed_entry(
        self,
        repo: AuditLogRepository,
        action: str = "session.create",
        actor_type: str = "user",
        details: dict | None = None,
    ) -> None:
        await repo.create(
            organization_id=ORG_ID,
            actor_id="user_1",
            actor_type=actor_type,
            action=action,
            resource_type="session",
            resource_id="session_1",
            details=details,
            ip_address="127.0.0.1",
        )

    async def test_create(self, engine, db_session) -> None:
        """Creating an entry returns it with generated fields populated."""
        async with AsyncSession(engine) as db:
            repo = AuditLogRepository(db)
            await self._seed_entry(repo, action="session.create", details={"status_code": 200})

            entries, total = await repo.list(ORG_ID)
            assert total == 1
            assert len(entries) == 1
            entry = entries[0]
            assert entry.id is not None
            assert entry.action == "session.create"
            assert entry.resource_type == "session"
            assert entry.details == {"status_code": 200}
            assert entry.created_at is not None

    async def test_list_empty(self, engine, db_session) -> None:
        """List returns ([]) for an org with no entries."""
        async with AsyncSession(engine) as db:
            repo = AuditLogRepository(db)
            entries, total = await repo.list(ORG_ID)
            assert entries == []
            assert total == 0

    async def test_list_filter_by_action_and_actor(self, engine, db_session) -> None:
        """List filters by action and actor_type exactly."""
        async with AsyncSession(engine) as db:
            repo = AuditLogRepository(db)
            await self._seed_entry(repo, action="session.create", actor_type="user")
            await self._seed_entry(repo, action="user.login", actor_type="api_key")

            sessions, total = await repo.list(ORG_ID, action="session.create")
            assert total == 1
            assert sessions[0].action == "session.create"

            api_keys, total = await repo.list(ORG_ID, actor_type="api_key")
            assert total == 1
            assert api_keys[0].actor_type == "api_key"

    async def test_list_exclude_prefix(self, engine, db_session) -> None:
        """List excludes actions matching comma-separated prefixes."""
        async with AsyncSession(engine) as db:
            repo = AuditLogRepository(db)
            await self._seed_entry(repo, action="session.create")
            await self._seed_entry(repo, action="session.delete")
            await self._seed_entry(repo, action="user.login")

            entries, total = await repo.list(ORG_ID, exclude_prefix="session")
            assert total == 1
            assert entries[0].action == "user.login"

    async def test_list_filter_by_status_code(self, engine, db_session) -> None:
        """List filters on details->>status_code."""
        async with AsyncSession(engine) as db:
            repo = AuditLogRepository(db)
            await self._seed_entry(repo, details={"status_code": 200})
            await self._seed_entry(repo, details={"status_code": 500})

            errors, total = await repo.list(ORG_ID, status_code=500)
            assert total == 1
            assert errors[0].details["status_code"] == 500

    async def test_list_filter_by_created_after(self, engine, db_session) -> None:
        """List with a future created_after returns nothing."""
        async with AsyncSession(engine) as db:
            repo = AuditLogRepository(db)
            await self._seed_entry(repo)

            entries, total = await repo.list(
                ORG_ID, created_after="2099-01-01T00:00:00Z"
            )
            assert entries == []
            assert total == 0

            entries, total = await repo.list(
                ORG_ID, created_after="2020-01-01T00:00:00Z"
            )
            assert total == 1

    async def test_list_pagination(self, engine, db_session) -> None:
        """List paginates with limit/offset and returns the total count."""
        async with AsyncSession(engine) as db:
            repo = AuditLogRepository(db)
            for i in range(5):
                await repo.create(
                    organization_id=ORG_ID,
                    actor_id=f"user_{i}",
                    actor_type="user",
                    action=f"action.{i}",
                    resource_type="session",
                    resource_id=None,
                    details=None,
                    ip_address=None,
                )

            page1, total = await repo.list(ORG_ID, limit=2, offset=0)
            assert len(page1) == 2
            assert total == 5

            page3, total = await repo.list(ORG_ID, limit=2, offset=4)
            assert len(page3) == 1
            assert total == 5


class TestCustomInstructionRepository:
    """CustomInstructionRepository — scope-scoped get/set/delete."""

    async def test_set_and_get_by_scope(self, engine, db_session) -> None:
        """set_by_scope inserts rows; get_by_scope returns them ordered by name."""
        async with AsyncSession(engine) as db:
            repo = CustomInstructionRepository(db)
            created = await repo.set_by_scope(
                ORG_ID,
                scope="extraction",
                target_id=None,
                instructions=[
                    {"name": "beta", "text": "Instruction B"},
                    {"name": "alpha", "text": "Instruction A"},
                ],
            )
            assert len(created) == 2

            found = await repo.get_by_scope(ORG_ID, scope="extraction")
            assert [i.name for i in found] == ["alpha", "beta"]

    async def test_get_by_scope_target_isolation(self, engine, db_session) -> None:
        """Org-level and target-level instructions do not leak into each other."""
        async with AsyncSession(engine) as db:
            repo = CustomInstructionRepository(db)
            target = UUID("12345678-1234-5678-1234-567812345678")
            await repo.set_by_scope(
                ORG_ID, scope="user_summary", target_id=None,
                instructions=[{"name": "org_level", "text": "Org-wide"}],
            )
            await repo.set_by_scope(
                ORG_ID, scope="user_summary", target_id=target,
                instructions=[{"name": "target_level", "text": "Per-user"}],
            )

            org_level = await repo.get_by_scope(ORG_ID, scope="user_summary")
            assert [i.name for i in org_level] == ["org_level"]

            target_level = await repo.get_by_scope(
                ORG_ID, scope="user_summary", target_id=target
            )
            assert [i.name for i in target_level] == ["target_level"]

    async def test_set_by_scope_replaces_existing(self, engine, db_session) -> None:
        """set_by_scope atomically replaces rows for the same scope + target."""
        async with AsyncSession(engine) as db:
            repo = CustomInstructionRepository(db)
            await repo.set_by_scope(
                ORG_ID, scope="extraction", target_id=None,
                instructions=[{"name": "old", "text": "Old text"}],
            )
            await repo.set_by_scope(
                ORG_ID, scope="extraction", target_id=None,
                instructions=[{"name": "new", "text": "New text"}],
            )

            found = await repo.get_by_scope(ORG_ID, scope="extraction")
            assert [i.name for i in found] == ["new"]

    async def test_delete_by_scope(self, engine, db_session) -> None:
        """delete_by_scope removes all rows for the scope; other scopes survive."""
        async with AsyncSession(engine) as db:
            repo = CustomInstructionRepository(db)
            await repo.set_by_scope(
                ORG_ID, scope="extraction", target_id=None,
                instructions=[{"name": "gone", "text": "Bye"}],
            )
            await repo.set_by_scope(
                ORG_ID, scope="user_summary", target_id=None,
                instructions=[{"name": "kept", "text": "Hello"}],
            )

            await repo.delete_by_scope(ORG_ID, scope="extraction")

            assert await repo.get_by_scope(ORG_ID, scope="extraction") == []
            kept = await repo.get_by_scope(ORG_ID, scope="user_summary")
            assert [i.name for i in kept] == ["kept"]


class TestEpisodeBlobRepository:
    """EpisodeBlobRepository — batch create, reads, update, delete.

    Blob rows need a user → session → episode chain first (all FKs are
    NOT NULL).  Only metadata is inserted — no S3 upload happens.
    """

    async def _seed_chain(self, db: AsyncSession) -> dict[str, UUID]:
        user_repo = UserRepository(db)
        session_repo = SessionRepository(db)
        episode_repo = EpisodeRepository(db)
        user = await user_repo.create(
            organization_id=ORG_ID, external_id="blob_test_user",
        )
        session = await session_repo.create(
            organization_id=ORG_ID,
            project_id=PROJECT_ID,
            created_by=user.id,
            external_id="blob_test_session",
        )
        episodes = await episode_repo.batch_create(
            organization_id=ORG_ID,
            project_id=PROJECT_ID,
            session_id=session.id,
            user_id=user.id,
            messages=[{"role": "user", "content": "With attachment"}],
        )
        return {
            "user_id": user.id,
            "session_id": session.id,
            "episode_id": episodes[0].id,
        }

    def _blob_dicts(self, episode_id: UUID) -> list[dict]:
        return [
            {
                "storage_backend": "s3",
                "storage_key": f"episodes/{episode_id}/0.pdf",
                "file_name": "doc.pdf",
                "mime_type": "application/pdf",
                "file_size": 1024,
                "content_hash": "abc123",
                "blob_index": 0,
            },
            {
                "storage_backend": "s3",
                "storage_key": f"episodes/{episode_id}/1.png",
                "file_name": "image.png",
                "mime_type": "image/png",
                "file_size": 2048,
                "content_hash": "def456",
                "blob_index": 1,
            },
        ]

    async def test_batch_create(self, engine, db_session) -> None:
        """batch_create inserts rows and populates defaults."""
        async with AsyncSession(engine) as db:
            chain = await self._seed_chain(db)
            repo = EpisodeBlobRepository(db)

            blobs = await repo.batch_create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                session_id=chain["session_id"],
                episode_id=chain["episode_id"],
                created_by=chain["user_id"],
                blobs=self._blob_dicts(chain["episode_id"]),
            )
            assert len(blobs) == 2
            assert blobs[0].file_name == "doc.pdf"
            assert blobs[0].storage_backend == "s3"
            assert blobs[0].blob_index == 0
            assert blobs[0].created_at is not None

    async def test_batch_create_empty(self, engine, db_session) -> None:
        """batch_create with an empty list returns []."""
        async with AsyncSession(engine) as db:
            repo = EpisodeBlobRepository(db)
            result = await repo.batch_create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                session_id=UUID("00000000-0000-0000-0000-000000000099"),
                episode_id=UUID("00000000-0000-0000-0000-000000000099"),
                created_by=UUID("00000000-0000-0000-0000-000000000099"),
                blobs=[],
            )
            assert result == []

    async def test_get_by_episode_and_session(self, engine, db_session) -> None:
        """get_by_episode orders by blob_index; get_by_session returns all."""
        async with AsyncSession(engine) as db:
            chain = await self._seed_chain(db)
            repo = EpisodeBlobRepository(db)
            await repo.batch_create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                session_id=chain["session_id"],
                episode_id=chain["episode_id"],
                created_by=chain["user_id"],
                blobs=self._blob_dicts(chain["episode_id"]),
            )

            by_episode = await repo.get_by_episode(chain["episode_id"])
            assert [b.blob_index for b in by_episode] == [0, 1]

            by_session = await repo.get_by_session(chain["session_id"])
            assert len(by_session) == 2

    async def test_get_by_id(self, engine, db_session) -> None:
        """get_by_id returns the blob; unknown id returns None."""
        async with AsyncSession(engine) as db:
            chain = await self._seed_chain(db)
            repo = EpisodeBlobRepository(db)
            blobs = await repo.batch_create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                session_id=chain["session_id"],
                episode_id=chain["episode_id"],
                created_by=chain["user_id"],
                blobs=self._blob_dicts(chain["episode_id"]),
            )

            found = await repo.get_by_id(blobs[0].id)
            assert found is not None
            assert found.id == blobs[0].id

            missing = await repo.get_by_id(
                UUID("00000000-0000-0000-0000-000000000099")
            )
            assert missing is None

    async def test_get_by_content_hash(self, engine, db_session) -> None:
        """get_by_content_hash finds matching blobs scoped to the org."""
        async with AsyncSession(engine) as db:
            chain = await self._seed_chain(db)
            repo = EpisodeBlobRepository(db)
            await repo.batch_create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                session_id=chain["session_id"],
                episode_id=chain["episode_id"],
                created_by=chain["user_id"],
                blobs=self._blob_dicts(chain["episode_id"]),
            )

            matches = await repo.get_by_content_hash(ORG_ID, "abc123")
            assert len(matches) == 1
            assert matches[0].file_name == "doc.pdf"

    async def test_update_extracted_text(self, engine, db_session) -> None:
        """update_extracted_text sets the text; missing id returns None."""
        async with AsyncSession(engine) as db:
            chain = await self._seed_chain(db)
            repo = EpisodeBlobRepository(db)
            blobs = await repo.batch_create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                session_id=chain["session_id"],
                episode_id=chain["episode_id"],
                created_by=chain["user_id"],
                blobs=self._blob_dicts(chain["episode_id"]),
            )

            updated = await repo.update_extracted_text(
                blobs[0].id, "extracted PDF text"
            )
            assert updated is not None
            assert updated.extracted_text == "extracted PDF text"

            missing = await repo.update_extracted_text(
                UUID("00000000-0000-0000-0000-000000000099"), "text"
            )
            assert missing is None

    async def test_count_by_episode(self, engine, db_session) -> None:
        """count_by_episode returns the number of blobs."""
        async with AsyncSession(engine) as db:
            chain = await self._seed_chain(db)
            repo = EpisodeBlobRepository(db)
            await repo.batch_create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                session_id=chain["session_id"],
                episode_id=chain["episode_id"],
                created_by=chain["user_id"],
                blobs=self._blob_dicts(chain["episode_id"]),
            )

            assert await repo.count_by_episode(chain["episode_id"]) == 2
            assert await repo.count_by_episode(
                UUID("00000000-0000-0000-0000-000000000099")
            ) == 0

    async def test_delete_by_episode(self, engine, db_session) -> None:
        """delete_by_episode removes the rows and returns them."""
        async with AsyncSession(engine) as db:
            chain = await self._seed_chain(db)
            repo = EpisodeBlobRepository(db)
            await repo.batch_create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                session_id=chain["session_id"],
                episode_id=chain["episode_id"],
                created_by=chain["user_id"],
                blobs=self._blob_dicts(chain["episode_id"]),
            )

            deleted = await repo.delete_by_episode(chain["episode_id"])
            assert len(deleted) == 2
            assert await repo.count_by_episode(chain["episode_id"]) == 0

    async def test_delete_by_ids(self, engine, db_session) -> None:
        """delete_by_ids removes only the requested blobs."""
        async with AsyncSession(engine) as db:
            chain = await self._seed_chain(db)
            repo = EpisodeBlobRepository(db)
            blobs = await repo.batch_create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                session_id=chain["session_id"],
                episode_id=chain["episode_id"],
                created_by=chain["user_id"],
                blobs=self._blob_dicts(chain["episode_id"]),
            )

            deleted = await repo.delete_by_ids([blobs[0].id])
            assert len(deleted) == 1
            assert await repo.count_by_episode(chain["episode_id"]) == 1

    async def test_get_orphaned_blobs(self, engine, db_session) -> None:
        """get_orphaned_blobs finds blobs of soft-deleted episodes."""
        async with AsyncSession(engine) as db:
            chain = await self._seed_chain(db)
            repo = EpisodeBlobRepository(db)
            await repo.batch_create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                session_id=chain["session_id"],
                episode_id=chain["episode_id"],
                created_by=chain["user_id"],
                blobs=self._blob_dicts(chain["episode_id"]),
            )

            assert await repo.get_orphaned_blobs(ORG_ID) == []

            episode_repo = EpisodeRepository(db)
            await episode_repo.soft_delete_by_project(PROJECT_ID)

            orphans = await repo.get_orphaned_blobs(ORG_ID)
            assert len(orphans) == 2


class TestPromptTemplateRepository:
    """PromptTemplateRepository — versioned org-scoped templates."""

    async def test_set_for_org_creates_v1(self, engine, db_session) -> None:
        """set_for_org creates the first active version."""
        async with AsyncSession(engine) as db:
            repo = PromptTemplateRepository(db)
            template = await repo.set_for_org(
                ORG_ID, name="memory_summary", text="Summarise {input}"
            )
            assert template.version == 1
            assert template.is_active is True
            assert template.is_default_for_type is False

    async def test_set_for_org_bumps_version_and_deactivates_old(
        self, engine, db_session
    ) -> None:
        """set_for_org creates v2 and deactivates v1."""
        async with AsyncSession(engine) as db:
            repo = PromptTemplateRepository(db)
            await repo.set_for_org(ORG_ID, name="memory_summary", text="v1 text")
            v2 = await repo.set_for_org(ORG_ID, name="memory_summary", text="v2 text")

            assert v2.version == 2

            active = await repo.get_active(ORG_ID, "memory_summary")
            assert active is not None
            assert active.version == 2
            assert active.template_text == "v2 text"

    async def test_get_active_not_found(self, engine, db_session) -> None:
        """get_active returns None for an unknown template."""
        async with AsyncSession(engine) as db:
            repo = PromptTemplateRepository(db)
            assert await repo.get_active(ORG_ID, "missing") is None

    async def test_get_version(self, engine, db_session) -> None:
        """get_version returns the exact version; unknown version returns None."""
        async with AsyncSession(engine) as db:
            repo = PromptTemplateRepository(db)
            await repo.set_for_org(ORG_ID, name="memory_summary", text="v1 text")
            await repo.set_for_org(ORG_ID, name="memory_summary", text="v2 text")

            v1 = await repo.get_version(ORG_ID, "memory_summary", 1)
            assert v1 is not None
            assert v1.template_text == "v1 text"

            assert await repo.get_version(ORG_ID, "memory_summary", 99) is None

    async def test_set_as_type_default(self, engine, db_session) -> None:
        """set_as_type_default promotes one template and demotes the other."""
        async with AsyncSession(engine) as db:
            repo = PromptTemplateRepository(db)
            await repo.set_for_org(
                ORG_ID, name="extract_facts", text="facts",
                template_type="fact_extraction",
            )
            await repo.set_for_org(
                ORG_ID, name="extract_facts_alt", text="facts alt",
                template_type="fact_extraction",
            )

            # No default yet.
            assert await repo.get_active_by_type(ORG_ID, "fact_extraction") is None

            promoted = await repo.set_as_type_default(ORG_ID, "extract_facts")
            assert promoted.is_default_for_type is True

            default = await repo.get_active_by_type(ORG_ID, "fact_extraction")
            assert default is not None
            assert default.template_name == "extract_facts"

    async def test_set_as_type_default_missing_template(
        self, engine, db_session
    ) -> None:
        """set_as_type_default raises ValueError for an unknown template."""
        async with AsyncSession(engine) as db:
            repo = PromptTemplateRepository(db)
            with pytest.raises(ValueError):
                await repo.set_as_type_default(ORG_ID, "missing")

    async def test_set_for_org_carries_type_default_flag(
        self, engine, db_session
    ) -> None:
        """A new version inherits the type-default flag from its predecessor."""
        async with AsyncSession(engine) as db:
            repo = PromptTemplateRepository(db)
            await repo.set_for_org(
                ORG_ID, name="extract_facts", text="v1",
                template_type="fact_extraction",
            )
            await repo.set_as_type_default(ORG_ID, "extract_facts")

            v2 = await repo.set_for_org(
                ORG_ID, name="extract_facts", text="v2",
                template_type="fact_extraction",
            )
            assert v2.version == 2
            assert v2.is_default_for_type is True
            assert v2.is_active is True

            default = await repo.get_active_by_type(ORG_ID, "fact_extraction")
            assert default is not None
            assert default.version == 2

    async def test_rollback(self, engine, db_session) -> None:
        """rollback creates a new version with the target version's text."""
        async with AsyncSession(engine) as db:
            repo = PromptTemplateRepository(db)
            await repo.set_for_org(ORG_ID, name="memory_summary", text="v1 text")
            await repo.set_for_org(ORG_ID, name="memory_summary", text="v2 text")

            rolled_back = await repo.rollback(ORG_ID, "memory_summary", version=1)
            assert rolled_back.version == 3
            assert rolled_back.template_text == "v1 text"
            assert rolled_back.is_active is True

    async def test_rollback_missing_version(self, engine, db_session) -> None:
        """rollback raises ValueError when the target version is missing."""
        async with AsyncSession(engine) as db:
            repo = PromptTemplateRepository(db)
            await repo.set_for_org(ORG_ID, name="memory_summary", text="v1 text")

            with pytest.raises(ValueError):
                await repo.rollback(ORG_ID, "memory_summary", version=99)

    async def test_list_names_and_versions(self, engine, db_session) -> None:
        """list_names collapses versions; list_versions returns all newest-first."""
        async with AsyncSession(engine) as db:
            repo = PromptTemplateRepository(db)
            await repo.set_for_org(ORG_ID, name="memory_summary", text="v1")
            await repo.set_for_org(ORG_ID, name="memory_summary", text="v2")
            await repo.set_for_org(ORG_ID, name="other_template", text="v1")

            names = await repo.list_names(ORG_ID)
            assert {n["name"] for n in names} == {"memory_summary", "other_template"}
            summary = next(n for n in names if n["name"] == "memory_summary")
            assert summary["version"] == 2

            versions = await repo.list_versions(ORG_ID, "memory_summary")
            assert [v.version for v in versions] == [2, 1]

    async def test_delete_for_org(self, engine, db_session) -> None:
        """delete_for_org removes every version of the template."""
        async with AsyncSession(engine) as db:
            repo = PromptTemplateRepository(db)
            await repo.set_for_org(ORG_ID, name="memory_summary", text="v1")
            await repo.set_for_org(ORG_ID, name="memory_summary", text="v2")

            await repo.delete_for_org(ORG_ID, "memory_summary")

            assert await repo.get_active(ORG_ID, "memory_summary") is None
            assert await repo.list_versions(ORG_ID, "memory_summary") == []
