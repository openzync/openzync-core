"""PostgreSQL row-level-security context helpers.

The tenant tables in this schema are protected by ``org_isolation_*``
policies (migration 0059).  Those policies read three GUCs:

``app.org_id``
    The tenant (organization) the current statement may touch.  Set for
    every authenticated request by :func:`dependencies.db.get_db` from the
    middleware-provided ``request.state.org_id``, and by
    :meth:`repositories.organization_repository.OrganizationRepository.set_tenant_context`
    for the public flows that only learn their tenant mid-request.

``app.bypass_rls``
    ``'true'`` disables the policies.  Reserved for the platform
    super-admin session (:func:`dependencies.db.get_db_superadmin`) and for
    the narrowly scoped pre-authentication lookups (see
    :func:`rls_bypass`).

``app.lookup_code``
    The join code presented by an anonymous joiner.  It grants a
    single-row read of ``organizations`` — the code *is* the credential —
    and nothing else.

**Scope: transaction-local.**  Every binding below uses
``set_config(..., is_local => true)`` — i.e. ``SET LOCAL``.  PostgreSQL
discards a transaction-local GUC when the transaction ends, so the tenant
context can never outlive its transaction and can never be carried onto a
pooled connection that a later request will use.  A statement that runs
without a bound context matches **no rows**: the policy expression
``NULLIF(current_setting('app.org_id', true), '')::UUID`` is NULL, and
``organization_id = NULL`` is never true.  A missing context is therefore
fail-closed *by construction* — an empty result, never another tenant's
rows.

Transaction-local scope has one cost: a mid-request ``COMMIT`` discards
the context, so every statement after that commit would run un-tenanted.
:class:`RlsAsyncSession` pays that cost automatically — it remembers the
context on the session and re-applies it to the session's *next*
transaction whenever the current one ends (commit or rollback).  The
re-bind statement is the first statement of the new transaction, so no
statement of that transaction can run against a foreign tenant.

**The invariant.**  A session's tenant context is always the context the
session was initialised with, on *every* transaction it runs — including
the transaction that follows a mid-request commit, and including the
transaction on whatever connection the pool hands out next.  The
session-creation paths that establish it:

* ``dependencies.db.get_db`` — :func:`init_context` with the request's
  ``request.state.org_id`` (empty for public endpoints).
* ``dependencies.db.get_db_superadmin`` — :func:`init_context` on the
  verification session; the bypass session then binds the platform org
  and ``app.bypass_rls='true'`` explicitly.
* ``services.worker.worker.rls_scoped_session_factory`` —
  :func:`init_context` with ``None`` (fail-closed) for every worker
  session; the task binds its own tenant afterwards.
* Any other session opened in this repository that touches an
  RLS-protected table — initialise it with :func:`init_context` (or bind
  a tenant with :func:`set_tenant_context`, or open a transaction-scoped
  :func:`rls_bypass`) as its first statement.

**How it breaks.**  A missing or stale context can only ever *narrow* a
result set (RLS is an additional filter), so the failure mode of a lost
context is a silent empty result — not a cross-tenant read.  The one way
to read another tenant's rows is to bind that tenant explicitly, which is
a caller bug, not a pool artefact.  :func:`init_context` pins the bypass
to ``'false'`` for the same reason.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)


_APPLY_CONTEXT_SQL = text(
    "SELECT set_config('app.org_id', :org_id, true), "
    "set_config('app.bypass_rls', :bypass_rls, true), "
    "set_config('app.lookup_code', :lookup_code, true)"
)
"""Bind the whole isolation context to the session's current transaction.

``is_local => true`` is ``SET LOCAL``: the value is discarded when the
transaction ends, so it cannot survive onto a pooled connection.
"""


@dataclass(frozen=True, slots=True)
class RlsContext:
    """The isolation context a session is bound to for the current transaction.

    Attributes:
        org_id: The tenant (organization) UUID, or ``''`` for no tenant.
        bypass_rls: Whether the row-level-security policies are disabled.
        lookup_code: The organization join code presented by an anonymous
            joiner.
    """

    org_id: str = ""
    bypass_rls: bool = False
    lookup_code: str = ""

    @property
    def is_empty(self) -> bool:
        """Whether the context binds no tenant, no bypass and no lookup code."""
        return not (self.org_id or self.bypass_rls or self.lookup_code)

    def as_params(self) -> dict[str, str]:
        """Render the context as ``set_config`` bind parameters.

        Returns:
            The bind parameters for :data:`_APPLY_CONTEXT_SQL`.
        """
        return {
            "org_id": self.org_id,
            "bypass_rls": "true" if self.bypass_rls else "false",
            "lookup_code": self.lookup_code,
        }


class RlsAsyncSession(AsyncSession):
    """An :class:`AsyncSession` that re-binds its RLS context after a commit.

    The tenant context is bound transaction-locally (see the module
    docstring), so a ``COMMIT`` — or a ``ROLLBACK`` — discards it.  This
    class remembers the context on the session and re-applies it to the
    session's next transaction whenever the current one ends, so a session
    that commits mid-request keeps its own tenant on whichever connection
    the pool hands out next.

    The re-bind is the session's first statement in the new transaction,
    so the new transaction can never run a statement against the previous
    tenant.  If the re-bind fails the exception propagates: a request
    whose context could not be restored must fail loudly rather than
    continue against an unknown tenant.
    """

    rls_context: RlsContext = RlsContext()
    """The context this session is bound to; re-applied after every commit."""

    async def commit(self) -> None:
        """Commit the transaction and re-bind the RLS context.

        The commit ends the transaction, which discards the
        transaction-local context.  The re-bind opens the next transaction
        and re-applies the context to it, so the statements that follow
        run under the same tenant.
        """
        await super().commit()
        await self._rebind_rls_context()

    async def rollback(self) -> None:
        """Roll the transaction back and re-bind the RLS context.

        A rollback also discards the transaction-local context, and the
        session stays usable afterwards — so the context is restored
        exactly as after a commit.
        """
        had_transaction = self.get_transaction() is not None
        await super().rollback()
        if had_transaction:
            await self._rebind_rls_context()

    async def _rebind_rls_context(self) -> None:
        """Re-apply the remembered context to the session's next transaction.

        The statement opens the session's next transaction, so it is the
        first statement of that transaction and no other statement of it
        can observe a foreign tenant.

        Raises:
            SQLAlchemyError: If the context could not be re-applied.  The
                failure is logged loudly and propagates — a session that
                lost its tenant must not continue against an unknown one.
        """
        ctx = self.rls_context
        if ctx.is_empty:
            return
        try:
            await self.execute(_APPLY_CONTEXT_SQL, ctx.as_params())
        except SQLAlchemyError:
            # Fail loud: a session that lost its tenant must never
            # continue against an unknown one.
            logger.error("rls.context_rebind_failed", exc_info=True)
            raise


def _remembered_context(db: AsyncSession) -> RlsContext:
    """Return the context remembered on ``db``.

    Args:
        db: The session to read.

    Returns:
        The session's remembered context, or an empty context when the
        session was never bound (e.g. a plain :class:`AsyncSession` that
        is not an :class:`RlsAsyncSession`).
    """
    ctx = getattr(db, "rls_context", None)
    # The isinstance check is not paranoia: a session double auto-creates
    # any attribute, and ``dataclasses.replace`` rejects anything that is
    # not a dataclass instance.
    return ctx if isinstance(ctx, RlsContext) else RlsContext()


async def _apply_context(db: AsyncSession, ctx: RlsContext) -> None:
    """Bind ``ctx`` to the session's current transaction and remember it.

    The context is remembered *after* the statement succeeds, so a failed
    binding leaves the session with the context it actually has.

    Args:
        db: The session to bind.
        ctx: The context to apply.
    """
    await db.execute(_APPLY_CONTEXT_SQL, ctx.as_params())
    db.rls_context = ctx  # type: ignore[attr-defined]


async def set_tenant_context(db: AsyncSession, org_id: UUID | str) -> None:
    """Bind the current session to one tenant (organization).

    Transaction-local, so the tenant is bound for the session's current
    transaction only.  :class:`RlsAsyncSession` re-applies it to the
    session's next transaction, so a mid-request commit does not leave
    the session un-tenanted.

    Args:
        db: The session to bind.
        org_id: The organization UUID the session may act on.
    """
    ctx = _remembered_context(db)
    await _apply_context(db, replace(ctx, org_id=str(org_id)))


async def set_bypass_rls(db: AsyncSession, enabled: bool = True) -> None:
    """Enable or disable the row-level-security bypass for the transaction.

    Transaction-local, so the bypass cannot outlive the transaction — not
    even when the statement that set it is rolled back.

    Args:
        db: The session to bind.
        enabled: Whether the policies are disabled for the transaction.
    """
    ctx = _remembered_context(db)
    await _apply_context(db, replace(ctx, bypass_rls=enabled))


async def set_lookup_code(db: AsyncSession, code: str) -> None:
    """Allow a single-row read of the organization owning ``code``.

    Used by the public org-code join: the submitted code is the
    credential, and the ``org_join_code_lookup`` policy matches exactly
    that one row.

    Args:
        db: The session to bind.
        code: The normalized organization code.
    """
    ctx = _remembered_context(db)
    await _apply_context(db, replace(ctx, lookup_code=code))


async def init_context(db: AsyncSession, org_id: str | None) -> None:
    """Initialise the RLS context for a request.

    Binds the tenant (empty when the request is unauthenticated — an empty
    context yields no rows and no successful org-scoped write), pins the
    bypass to ``'false'`` and clears any lookup code.

    This must be the **first statement of the session**.  See the module
    docstring.

    Args:
        db: The session to initialise.
        org_id: The authenticated organization UUID, or ``None``.
    """
    await _apply_context(db, RlsContext(org_id=(org_id or "").strip()))


async def release_tenant_context(db: AsyncSession) -> None:
    """Drop the session's RLS context and discard its transaction.

    The tenant context is transaction-local, so the end of the transaction
    already discards it — there is nothing to reset on the pooled
    connection, and no window in which a tenant can stand on a connection
    the pool hands to the next request.

    What this does is drop the *remembered* context (so the post-commit
    re-bind cannot resurrect it) and roll back whatever the caller left
    uncommitted.

    Args:
        db: The session whose context is about to be released.
    """
    try:
        db.rls_context = RlsContext()  # type: ignore[attr-defined]
        await db.rollback()
    except SQLAlchemyError:
        # The connection is unusable — SQLAlchemy invalidates it and the
        # pool discards it instead of handing it to the next task, so the
        # tenant cannot leak.  Log loudly rather than mask the error that
        # is already on its way out.
        logger.error("rls.tenant_context_release_failed", exc_info=True)


@asynccontextmanager
async def rls_bypass(db: AsyncSession) -> AsyncIterator[None]:
    """Run the enclosed statements with the RLS policies disabled.

    Used by the pre-authentication lookups (login by email, refresh-token
    rotation, invite claim) where the row must be found before the tenant
    is known.  The bypass is **transaction-scoped** (``SET LOCAL``), so
    PostgreSQL discards it at the next COMMIT or ROLLBACK — it cannot
    outlive the transaction, not even when the reset statement below
    fails.

    Args:
        db: The session to run the statements on.

    Yields:
        ``None`` — the enclosed block runs with the policies disabled.
    """
    await db.execute(text("SELECT set_config('app.bypass_rls', 'true', true)"))
    try:
        yield
    except Exception:
        # Log before re-raising — the failure must be visible even when the
        # caller swallows it into a generic response.
        logger.exception("rls.bypass_block_failed")
        raise
    finally:
        try:
            await db.execute(text("SELECT set_config('app.bypass_rls', 'false', true)"))
        except SQLAlchemyError:
            # The reset failed — the transaction is almost certainly
            # aborted (or the connection died).  Roll back so the
            # transaction-local bypass dies with the transaction instead
            # of lingering on a still-open one.  A dead connection is
            # discarded by the pool, so the bypass cannot leak that way
            # either — log loudly either way, never mask the original.
            logger.error("rls.bypass_reset_failed", exc_info=True)
            try:
                await db.rollback()
            except SQLAlchemyError:
                logger.error("rls.bypass_reset_rollback_failed", exc_info=True)
            # Re-assert 'false' on whatever transaction is now current:
            # after a successful rollback the bypass is gone with the old
            # transaction, and this pins the fresh one to 'false' so a
            # pooled-session reuse can never observe a stuck bypass.
            try:
                await db.execute(
                    text("SELECT set_config('app.bypass_rls', 'false', true)")
                )
            except SQLAlchemyError:
                logger.error("rls.bypass_reset_reassert_failed", exc_info=True)
