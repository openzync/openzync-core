"""Integration tests for remaining repositories — CRUD patterns with PostgreSQL.

Covers: ApiKeyRepository, AuthRepository, FactRepository,
ExtractionSchemaRepository, StructuredExtractionRepository,
DialogClassificationRepository.

All require testcontainers PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.episode import Episode
from models.fact import Fact
from repositories.api_key_repository import ApiKeyRepository
from repositories.auth_repository import AuthRepository
from repositories.dialog_classification_repository import (
    DialogClassificationRepository,
)
from repositories.extraction_schema_repository import ExtractionSchemaRepository
from repositories.fact_repository import FactRepository
from repositories.structured_extraction_repository import (
    StructuredExtractionRepository,
)
from repositories.user_repository import UserRepository
from repositories.session_repository import SessionRepository
from repositories.episode_repository import EpisodeRepository


pytestmark = pytest.mark.integration
ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
ALT_ORG_ID = UUID("00000000-0000-0000-0000-000000000099")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")


# ── Helpers ────────────────────────────────────────────────────────────────


async def _seed_user_and_session(engine) -> tuple[UUID, UUID]:
    async with AsyncSession(engine) as db:
        user_repo = UserRepository(db)
        session_repo = SessionRepository(db)
        user = await user_repo.create(
            organization_id=ORG_ID, external_id="repo_test_user",
        )
        session = await session_repo.create(
            organization_id=ORG_ID, project_id=PROJECT_ID, created_by=user.id,
            external_id="repo_test_session",
        )
        return user.id, session.id


async def _seed_episode(engine, user_id: UUID, session_id: UUID) -> UUID:
    async with AsyncSession(engine) as db:
        ep_repo = EpisodeRepository(db)
        episodes = await ep_repo.batch_create(
            organization_id=ORG_ID,
            project_id=PROJECT_ID,
            session_id=session_id,
            user_id=user_id,
            messages=[{"role": "user", "content": "test episode", "metadata": {}}],
        )
        return episodes[0].id


# ═══════════════════════════════════════════════════════════════════════════
# ApiKeyRepository
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestApiKeyRepository:
    async def test_create_and_list(self, engine) -> None:
        async with AsyncSession(engine) as db:
            repo = ApiKeyRepository(db)
            key = await repo.create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                key_hash="hash123", salt="salty", prefix="mg_test_",
                lookup_hash="lookup123", name="Test Key",
            )
            assert key.id is not None
            assert key.prefix == "mg_test_"
            assert key.project_id == PROJECT_ID

            # List by org without project_id returns all for the org
            keys = await repo.list_by_org(ORG_ID)
            assert len(keys) >= 1

            # List by org + project returns only project-scoped keys
            project_keys = await repo.list_by_org(ORG_ID, project_id=PROJECT_ID)
            assert len(project_keys) >= 1

            # List by a different project returns empty
            other_keys = await repo.list_by_org(
                ORG_ID,
                project_id=UUID("00000000-0000-0000-0000-000000000099"),
            )
            assert len(other_keys) == 0

    async def test_get_by_id_found(self, engine) -> None:
        async with AsyncSession(engine) as db:
            repo = ApiKeyRepository(db)
            key = await repo.create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                key_hash="h2", salt="s2",
                prefix="mg_test_", lookup_hash="l2", name="Key 2",
            )
            found = await repo.get_by_id(ORG_ID, key.id)
            assert found is not None
            assert found.id == key.id

            # Found with project_id filter
            found = await repo.get_by_id(ORG_ID, key.id, project_id=PROJECT_ID)
            assert found is not None

            # Not found with wrong project_id
            not_found = await repo.get_by_id(
                ORG_ID, key.id,
                project_id=UUID("00000000-0000-0000-0000-000000000099"),
            )
            assert not_found is None

    async def test_get_by_id_not_found(self, engine) -> None:
        async with AsyncSession(engine) as db:
            repo = ApiKeyRepository(db)
            found = await repo.get_by_id(ORG_ID, uuid4())
            assert found is None

    async def test_revoke(self, engine) -> None:
        async with AsyncSession(engine) as db:
            repo = ApiKeyRepository(db)
            key = await repo.create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                key_hash="h3", salt="s3",
                prefix="mg_test_", lookup_hash="l3", name="Key 3",
            )
            revoked = await repo.revoke(ORG_ID, key.id)
            assert revoked is not None
            assert revoked.is_revoked is True

            # Cannot revoke with wrong project_id
            key2 = await repo.create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                key_hash="h4", salt="s4",
                prefix="mg_test_", lookup_hash="l4", name="Key 4",
            )
            not_revoked = await repo.revoke(
                ORG_ID, key2.id,
                project_id=UUID("00000000-0000-0000-0000-000000000099"),
            )
            assert not_revoked is None
            # Key should still be active
            still_active = await repo.get_by_id(ORG_ID, key2.id)
            assert still_active is not None
            assert still_active.is_revoked is False

    async def test_revoke_with_project_id(self, engine) -> None:
        """Revoke should succeed when project_id matches."""
        async with AsyncSession(engine) as db:
            repo = ApiKeyRepository(db)
            key = await repo.create(
                organization_id=ORG_ID,
                project_id=PROJECT_ID,
                key_hash="h5", salt="s5",
                prefix="mg_test_", lookup_hash="l5", name="Key 5",
            )
            revoked = await repo.revoke(
                ORG_ID, key.id, project_id=PROJECT_ID,
            )
            assert revoked is not None
            assert revoked.is_revoked is True


# ═══════════════════════════════════════════════════════════════════════════
# AuthRepository
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestAuthRepository:
    async def test_create_dashboard_user(self, engine) -> None:
        async with AsyncSession(engine) as db:
            repo = AuthRepository(db)
            user = await repo.create_dashboard_user(
                email="test@openzep.dev", password_hash="hash",
                organization_id=ORG_ID,
            )
            assert user.id is not None
            assert user.email == "test@openzep.dev"

    async def test_find_user_by_email_found(self, engine) -> None:
        async with AsyncSession(engine) as db:
            repo = AuthRepository(db)
            await repo.create_dashboard_user(
                email="findme@openzep.dev", password_hash="hash",
                organization_id=ORG_ID,
            )
            user = await repo.find_user_by_email("findme@openzep.dev")
            assert user is not None
            assert user.email == "findme@openzep.dev"

    async def test_create_refresh_token(self, engine) -> None:
        async with AsyncSession(engine) as db:
            repo = AuthRepository(db)
            token = await repo.create_refresh_token(
                user_id=str(uuid4()), organization_id=ORG_ID,
                token_hash="tok_hash", expires_at=datetime.now(),
            )
            assert token.id is not None

    async def test_revoke_refresh_token(self, engine) -> None:
        async with AsyncSession(engine) as db:
            repo = AuthRepository(db)
            token = await repo.create_refresh_token(
                user_id=str(uuid4()), organization_id=ORG_ID,
                token_hash="revoke_hash", expires_at=datetime.now(),
            )
            assert token.is_revoked is False

            await repo.revoke_refresh_token(token.id)
            # Manually check the token was revoked in DB
            from sqlalchemy import select
            from models.refresh_token import RefreshToken
            result = await db.execute(
                select(RefreshToken).where(RefreshToken.id == token.id)
            )
            found = result.scalar_one()
            assert found.is_revoked is True


# ═══════════════════════════════════════════════════════════════════════════
# FactRepository
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestFactRepository:
    async def test_create(self, engine) -> None:
        user_id, _ = await _seed_user_and_session(engine)
        async with AsyncSession(engine) as db:
            repo = FactRepository(db)
            fact = await repo.create(
                user_id=user_id, organization_id=ORG_ID,
                project_id=PROJECT_ID,
                content="Python is great", subject="Python",
                predicate="is", obj="great", confidence=0.95,
            )
            assert fact.id is not None
            assert fact.content == "Python is great"

    async def test_soft_delete_by_user(self, engine) -> None:
        user_id, _ = await _seed_user_and_session(engine)
        async with AsyncSession(engine) as db:
            repo = FactRepository(db)
            await repo.create(
                user_id=user_id, organization_id=ORG_ID,
                project_id=PROJECT_ID,
                content="Delete me", subject="X", predicate="is", obj="Y",
            )
            deleted = await repo.soft_delete_by_user(user_id)
            assert deleted >= 1

    async def test_list_by_session(self, engine) -> None:
        user_id, session_id = await _seed_user_and_session(engine)
        async with AsyncSession(engine) as db:
            repo = FactRepository(db)
            facts, cursor = await repo.list_by_session(
                ORG_ID, session_id, limit=10,
            )
            assert isinstance(facts, list)


# ═══════════════════════════════════════════════════════════════════════════
# ExtractionSchemaRepository
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestExtractionSchemaRepository:
    async def test_create_and_get(self, engine) -> None:
        async with AsyncSession(engine) as db:
            repo = ExtractionSchemaRepository(db)
            schema = await repo.create(
                org_id=ORG_ID, name="test-schema",
                json_schema={"type": "object", "properties": {"name": {"type": "string"}}},
            )
            assert schema.id is not None

            found = await repo.get_by_id(ORG_ID, schema.id)
            assert found is not None
            assert found.name == "test-schema"

    async def test_get_all(self, engine) -> None:
        async with AsyncSession(engine) as db:
            repo = ExtractionSchemaRepository(db)
            schemas = await repo.get_all(ORG_ID)
            assert isinstance(schemas, list)

    async def test_update(self, engine) -> None:
        async with AsyncSession(engine) as db:
            repo = ExtractionSchemaRepository(db)
            schema = await repo.create(
                org_id=ORG_ID, name="update-schema",
                json_schema={"type": "object"},
            )
            updated = await repo.update(
                schema, prompt_template="new template",
            )
            assert updated is not None
            assert updated.prompt_template == "new template"


# ═══════════════════════════════════════════════════════════════════════════
# StructuredExtractionRepository
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestStructuredExtractionRepository:
    async def test_get_by_session(self, engine) -> None:
        async with AsyncSession(engine) as db:
            repo = StructuredExtractionRepository(db)
            results = await repo.get_by_session(
                ORG_ID, uuid4(),
            )
            assert isinstance(results, list)

    async def test_get_by_episode(self, engine) -> None:
        async with AsyncSession(engine) as db:
            repo = StructuredExtractionRepository(db)
            result = await repo.get_by_episode(ORG_ID, episode_id=uuid4())
            assert result is None  # no seeded data


# ═══════════════════════════════════════════════════════════════════════════
# DialogClassificationRepository
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestDialogClassificationRepository:
    async def test_get_by_session(self, engine) -> None:
        async with AsyncSession(engine) as db:
            repo = DialogClassificationRepository(db)
            results = await repo.get_by_session(
                ORG_ID, session_id=uuid4(),
            )
            assert isinstance(results, list)

    async def test_get_by_episode_not_found(self, engine) -> None:
        async with AsyncSession(engine) as db:
            repo = DialogClassificationRepository(db)
            result = await repo.get_by_episode(ORG_ID, episode_id=uuid4())
            assert result is None

    async def test_count_for_session(self, engine) -> None:
        async with AsyncSession(engine) as db:
            repo = DialogClassificationRepository(db)
            count = await repo.count_for_session(ORG_ID, session_id=uuid4())
            assert count >= 0
