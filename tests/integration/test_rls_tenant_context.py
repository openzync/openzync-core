"""Integration test for the RLS tenant context — a real PostgreSQL, real policies.

The unit tests pin the *shape* of the tenant context (one statement, all
three GUCs, transaction-local).  This file pins the *behaviour* against a
real database with the migration-0059 policies installed:

* the tenant survives a COMMIT and a ROLLBACK,
* the re-bind is the FIRST statement of the new transaction,
* a session with no context reads ZERO rows (fail-closed),
* a hand-bound (``set_config``) tenant is lost at the next commit while a
  ``core.rls``-bound tenant is not,
* the ``org_anon_insert`` policy refuses an organization row that does not
  belong to the session's tenant.

The tests connect as a **non-superuser** role on purpose: a superuser
bypasses row-level security entirely, so every assertion here would pass
vacuously against the container's default role.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from core.db import _register_pgvector_codec, get_async_session
from core.rls import set_tenant_context

pytestmark = pytest.mark.integration

RLS_ROLE = "oz_rls_test"
RLS_PASSWORD = "oz_rls_test"  # noqa: S105 — throwaway role in a testcontainer

_CREATED_ORG_IDS: list[UUID] = []
"""Every organization this module created — removed again at session end."""


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _cleanup_created_orgs(engine: AsyncEngine) -> AsyncIterator[None]:
    """Delete the organizations this module created.

    The rows are seeded straight into the database (the tenant sessions
    are not allowed to create them), so they have to be removed again —
    otherwise a later test that counts organizations would see them.
    """
    yield
    if not _CREATED_ORG_IDS:
        return
    async with engine.begin() as conn:
        for org_id in _CREATED_ORG_IDS:
            await conn.execute(
                text("DELETE FROM organizations WHERE id = :id"),
                {"id": org_id},
            )


@pytest_asyncio.fixture(scope="session")
async def rls_engine(engine: AsyncEngine) -> AsyncEngine:
    """An engine that connects as a plain (non-superuser) role.

    The testcontainers PostgreSQL hands out a superuser, and a superuser
    bypasses row-level security — the policies would be inert and the
    assertions below would prove nothing.  This role owns nothing and has
    no ``BYPASSRLS``, so the forced policies apply to it exactly as they
    apply to the application role in production.
    """
    async with engine.connect() as conn:
        await conn.execute(
            text(
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_roles WHERE rolname = '{RLS_ROLE}'
                    ) THEN
                        EXECUTE 'CREATE ROLE {RLS_ROLE} LOGIN '
                            'PASSWORD ''{RLS_PASSWORD}'' '
                            'NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE';
                    END IF;
                END
                $$
                """  # noqa: S608 — the role name is a module constant, never input
            )
        )
        await conn.execute(
            text(
                f"GRANT CONNECT ON DATABASE {engine.url.database} TO {RLS_ROLE}"
                # noqa: S608 — the role/database names are test constants
            )
        )
        await conn.execute(
            text(f"GRANT USAGE ON SCHEMA public TO {RLS_ROLE}")  # noqa: S608
        )
        await conn.execute(
            text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {RLS_ROLE}"
                # noqa: S608 — the role name is a module constant, never input
            )
        )
        await conn.execute(
            text(
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RLS_ROLE}"
                # noqa: S608 — the role name is a module constant, never input
            )
        )
        await conn.commit()

    url = engine.url.set(username=RLS_ROLE, password=RLS_PASSWORD)
    scoped = create_async_engine(url, poolclass=NullPool, pool_pre_ping=True)
    _register_pgvector_codec(scoped)
    try:
        yield scoped
    finally:
        await scoped.dispose()


@pytest_asyncio.fixture(scope="session")
async def pooled_rls_engine(engine: AsyncEngine) -> AsyncEngine:
    """The same non-superuser role, but on a **pooled** engine.

    ``NullPool`` hands every session a brand-new connection, which hides
    the "the GUC stood on the pooled connection" failure mode.  A pool
    with a single connection reproduces it: the same connection is
    returned to the pool and handed to the next session.
    """
    url = engine.url.set(username=RLS_ROLE, password=RLS_PASSWORD)
    scoped = create_async_engine(
        url,
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
    )
    try:
        yield scoped
    finally:
        await scoped.dispose()


@pytest_asyncio.fixture(scope="session")
async def tenants(engine: AsyncEngine) -> dict[str, tuple[UUID, UUID]]:
    """Two tenants, each with one project.

    Seeded through the superuser connection — the RLS policies are bypassed
    there, so the fixtures can create the rows the tenant sessions then
    have to *not* see.
    """
    seeded: dict[str, tuple[UUID, UUID]] = {}
    async with engine.begin() as conn:
        for label in ("a", "b"):
            org_id = uuid4()
            project_id = uuid4()
            _CREATED_ORG_IDS.append(org_id)
            await conn.execute(
                text(
                    "INSERT INTO organizations (id, name, plan, org_code) "
                    "VALUES (:id, :name, 'free', :code)"
                ),
                {
                    "id": org_id,
                    "name": f"RLS Tenant {label.upper()}",
                    "code": f"rls{label}{uuid4().hex[:8]}",
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO projects (id, organization_id, name) "
                    "VALUES (:id, :org_id, :name)"
                ),
                {
                    "id": project_id,
                    "org_id": org_id,
                    "name": f"RLS Project {label.upper()}",
                },
            )
            seeded[label] = (org_id, project_id)
    return seeded


async def _visible_project_ids(session) -> set[UUID]:
    """The project ids the session can read (RLS decides, not the query)."""
    result = await session.execute(text("SELECT id FROM projects"))
    return {row[0] for row in result.all()}


class TestTenantSurvivesTransactionBoundaries:
    """The tenant context is re-bound to every transaction the session runs."""

    @pytest.mark.asyncio
    async def test_the_tenant_survives_a_commit(
        self,
        rls_engine: AsyncEngine,
        tenants: dict[str, tuple[UUID, UUID]],
    ) -> None:
        """A commit does not un-tenant the session.

        The context is bound transaction-locally, so the COMMIT discards
        it.  ``RlsAsyncSession`` re-applies it to the next transaction —
        without that, the session would run un-tenanted and could pick up
        another request's connection (the confirmed cross-tenant leak).
        """
        org_a, project_a = tenants["a"]
        _org_b, project_b = tenants["b"]
        factory = get_async_session(rls_engine)

        async with factory() as session:
            await set_tenant_context(session, org_a)
            assert await _visible_project_ids(session) == {project_a}

            await session.commit()

            # Same tenant after the commit — and never the other one.
            assert await _visible_project_ids(session) == {project_a}
            assert project_b not in await _visible_project_ids(session)

    @pytest.mark.asyncio
    async def test_the_tenant_survives_a_rollback(
        self,
        rls_engine: AsyncEngine,
        tenants: dict[str, tuple[UUID, UUID]],
    ) -> None:
        """A rollback does not un-tenant the session either.

        A mid-request rollback used to discard the binding while the
        request kept querying (``services/memory_service.py``) — the
        session then ran with no tenant at all.
        """
        org_a, project_a = tenants["a"]
        factory = get_async_session(rls_engine)

        async with factory() as session:
            await set_tenant_context(session, org_a)
            assert await _visible_project_ids(session) == {project_a}

            await session.rollback()

            assert await _visible_project_ids(session) == {project_a}

    @pytest.mark.asyncio
    async def test_the_rebind_is_the_first_statement_of_the_new_transaction(
        self,
        rls_engine: AsyncEngine,
        tenants: dict[str, tuple[UUID, UUID]],
    ) -> None:
        """No statement of the new transaction can observe a foreign tenant.

        The re-bind opens the next transaction, so it is that transaction's
        first statement — asserted by recording every statement the engine
        executed and the position of the COMMIT boundary.
        """
        org_a, _project_a = tenants["a"]
        factory = get_async_session(rls_engine)

        statements: list[str] = []
        commits: list[int] = []

        @event.listens_for(rls_engine.sync_engine, "before_cursor_execute")
        def _record(conn, cursor, statement, parameters, context, executemany) -> None:
            statements.append(statement)

        @event.listens_for(rls_engine.sync_engine, "commit")
        def _on_commit(conn) -> None:
            commits.append(len(statements))

        try:
            async with factory() as session:
                await set_tenant_context(session, org_a)
                await session.commit()
                await session.execute(text("SELECT 1"))
        finally:
            event.remove(rls_engine.sync_engine, "before_cursor_execute", _record)
            event.remove(rls_engine.sync_engine, "commit", _on_commit)

        assert commits, "the commit boundary must have been observed"
        first_after_commit = statements[commits[0]]
        assert "set_config('app.org_id'" in first_after_commit
        # The caller's own statement runs *after* the re-bind.
        assert "SELECT 1" in statements[commits[0] + 1]


class TestFailClosed:
    """A session without a tenant context sees nothing."""

    @pytest.mark.asyncio
    async def test_a_session_with_no_context_reads_zero_rows(
        self,
        rls_engine: AsyncEngine,
        tenants: dict[str, tuple[UUID, UUID]],
    ) -> None:
        """No context → no rows, and never another tenant's rows.

        This is why the policy expression is
        ``NULLIF(current_setting('app.org_id', true), '')::UUID``: an unset
        context is NULL, not an error.
        """
        _org_a, project_a = tenants["a"]
        _org_b, project_b = tenants["b"]
        factory = get_async_session(rls_engine)

        async with factory() as session:
            visible = await _visible_project_ids(session)
            assert visible == set()
            assert project_a not in visible
            assert project_b not in visible

    @pytest.mark.asyncio
    async def test_the_binding_does_not_survive_on_the_pooled_connection(
        self,
        pooled_rls_engine: AsyncEngine,
        tenants: dict[str, tuple[UUID, UUID]],
    ) -> None:
        """The tenant does not stand on the connection the pool hands out.

        A session-scoped ``set_config`` survives the COMMIT, so the tenant
        would stay on the pooled connection and the next session would
        inherit it.  The binding is transaction-local, so PostgreSQL
        discards it with the transaction and the pooled connection is
        handed back clean.
        """
        org_a, _project_a = tenants["a"]
        factory = get_async_session(pooled_rls_engine)

        async with factory() as session:
            await set_tenant_context(session, org_a)
            current = (
                await session.execute(
                    text("SELECT current_setting('app.org_id', true)")
                )
            ).scalar()
            assert current == str(org_a)
            # A COMMIT is the boundary that discards a transaction-local
            # GUC — a session-scoped one would survive it.
            await session.commit()

        # The connection went back to the pool and is handed to the next
        # session — with no tenant left on it.
        async with factory() as session:
            current = (
                await session.execute(
                    text("SELECT current_setting('app.org_id', true)")
                )
            ).scalar()
            assert current in (None, "")


class TestHandBoundContext:
    """A raw ``set_config`` is not remembered — the tenant is lost."""

    @pytest.mark.asyncio
    async def test_a_hand_bound_tenant_is_lost_after_a_commit(
        self,
        rls_engine: AsyncEngine,
        tenants: dict[str, tuple[UUID, UUID]],
    ) -> None:
        """Binding by hand does not survive the commit.

        Only ``core.rls.set_tenant_context`` records the context on the
        session.  A hand-rolled ``set_config`` binds the tenant for the
        current transaction and is then gone — which is why the worker task
        files were converted to the ``core.rls`` helpers.
        """
        org_a, project_a = tenants["a"]
        factory = get_async_session(rls_engine)

        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.org_id', :org_id, true)"),
                {"org_id": str(org_a)},
            )
            assert await _visible_project_ids(session) == {project_a}

            await session.commit()

            # The tenant is gone — the session now sees nothing at all.
            assert await _visible_project_ids(session) == set()

    @pytest.mark.asyncio
    async def test_a_core_rls_tenant_is_kept_after_a_commit(
        self,
        rls_engine: AsyncEngine,
        tenants: dict[str, tuple[UUID, UUID]],
    ) -> None:
        """The counterpart: ``core.rls`` remembers and re-binds the tenant."""
        org_a, project_a = tenants["a"]
        factory = get_async_session(rls_engine)

        async with factory() as session:
            await set_tenant_context(session, org_a)
            await session.commit()

            assert await _visible_project_ids(session) == {project_a}


class TestOrgAnonInsertPolicy:
    """``org_anon_insert`` only admits the session's own organization row."""

    @pytest.mark.asyncio
    async def test_a_tenant_cannot_insert_another_organizations_row(
        self,
        rls_engine: AsyncEngine,
        tenants: dict[str, tuple[UUID, UUID]],
    ) -> None:
        """The INSERT policy is not ``WITH CHECK (true)``.

        Permissive policies are OR'ed, so an unconditional CHECK would
        override the ``FOR ALL`` policy's ``id = <app.org_id>`` and let a
        tenant-bound session create an organization row for any id.
        """
        org_a, _project_a = tenants["a"]
        factory = get_async_session(rls_engine)
        foreign_org_id = uuid4()

        async with factory() as session:
            await set_tenant_context(session, org_a)

            with pytest.raises(DBAPIError) as exc_info:
                await session.execute(
                    text(
                        "INSERT INTO organizations (id, name, plan, org_code) "
                        "VALUES (:id, :name, 'free', :code)"
                    ),
                    {
                        "id": foreign_org_id,
                        "name": "Foreign Org",
                        "code": f"foreign{uuid4().hex[:8]}",
                    },
                )

            assert "row-level security" in str(exc_info.value).lower()
            await session.rollback()

    @pytest.mark.asyncio
    async def test_a_session_bound_to_the_new_organization_can_insert_it(
        self,
        rls_engine: AsyncEngine,
    ) -> None:
        """The signup path still works: bind the new id, then insert it."""
        factory = get_async_session(rls_engine)
        new_org_id = uuid4()
        _CREATED_ORG_IDS.append(new_org_id)

        async with factory() as session:
            await set_tenant_context(session, new_org_id)

            await session.execute(
                text(
                    "INSERT INTO organizations (id, name, plan, org_code) "
                    "VALUES (:id, :name, 'free', :code)"
                ),
                {
                    "id": new_org_id,
                    "name": "Brand New Org",
                    "code": f"neworg{uuid4().hex[:8]}",
                },
            )
            await session.commit()

            # The row is readable — the session is bound to it.
            result = await session.execute(
                text("SELECT id FROM organizations WHERE id = :id"),
                {"id": new_org_id},
            )
            assert {row[0] for row in result.all()} == {new_org_id}
