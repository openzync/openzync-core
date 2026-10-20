"""Force row-level security and make the org-isolation policies fail closed.

Every tenant table that carried an ``org_isolation_*`` policy had it
**inert**: PostgreSQL exempts the table *owner* from row-level security,
and the application role owns every table.  This revision closes that hole
with ``ALTER TABLE ... FORCE ROW LEVEL SECURITY``, which applies the
policies to the owner as well — so the change needs no role split, no
``DATABASE_URL`` change and no operational re-provisioning.

Five tenant tables carried an ``organization_id`` but had **no** policy and
no RLS at all; they are brought under the same org-isolation policy here
(see the table count at the end of this docstring).

It also fixes two policy defects that only became visible once the policies
actually fired:

1. ``current_setting('app.org_id')`` was called **without** ``missing_ok``.
   An unset context therefore raised ``invalid input syntax for type uuid:
   ""`` instead of matching no rows.  The expression is now
   ``NULLIF(current_setting('app.org_id', true), '')::uuid`` — an unset or
   empty context yields ``NULL``, i.e. *no rows* and *no successful
   org-scoped write*.
2. The policies are recreated so that the ``FOR ALL`` shape and the policy
   names stay exactly as they were — only the ``app.org_id`` expression
   changes.

Two extra policies are added on ``organizations`` for the two public
flows that cannot know their tenant up front:

``org_join_code_lookup`` (``FOR SELECT`` only)
    The join code presented by an anonymous joiner *is* the credential, so
    the submitted code is bound to ``app.lookup_code`` and the policy lets
    exactly the row with that ``org_code`` be read.  It is a separate
    ``FOR SELECT`` policy, so it ORs with the ``FOR ALL`` policy for
    SELECT but does **not** widen INSERT/UPDATE/DELETE, which stay
    org-scoped.  A wrong code matches no row, so it cannot be used to
    enumerate organizations.

    **The RETURNING clause of an INSERT is filtered by this policy too.**
    PostgreSQL applies a table's SELECT policies to the ``RETURNING``
    clause of an ``INSERT``: a row the session may not SELECT is not
    returned, and an INSERT whose ``RETURNING`` yields no row is rejected
    with *"new row violates row-level security policy"* — even when the
    INSERT's own ``WITH CHECK`` passed.  A session that inserts an
    organization row without being bound to that organization therefore
    fails at the flush, not at the CHECK.  This is why the organization id
    is minted in Python and bound to the session **before** the flush (see
    ``AuthRepository.create_organization`` and
    ``OrganizationService.create_organization``): the id must be known and
    bound before the INSERT runs, because the database cannot hand it back.

``org_anon_insert`` (``FOR INSERT`` only)
    The public signup path generates the organization id itself and creates
    the org row *before* the request knows the tenant context — the row is
    brand new and contains only that new org's data.  The policy is
    INSERT-only: SELECT/UPDATE/DELETE on ``organizations`` remain
    org-scoped.  Its ``CHECK`` is the same expression the SELECT policy
    uses — ``id = NULLIF(current_setting('app.org_id', true), '')::UUID`` —
    so a session may only insert the organization it is bound to.  The
    pre-tightening form (``WITH CHECK (true)``) was *wider* than the
    ``FOR ALL`` policy: permissive policies are OR'ed, so the unconditional
    CHECK overrode the ``FOR ALL`` policy's ``id = <app.org_id>`` and any
    tenant-bound session could insert an arbitrary organization row.
    (No equivalent policy is added to ``users``: the user insert happens
    after the tenant context is known and is therefore org-scoped like
    every other write.)

The ``audit_logs`` policy is tightened as well: it no longer admits the
organization-less rows (the platform-level audit trail of unauthenticated
requests) to a tenant-bound session, and the application layer filters the
organization explicitly (``AuditLogRepository.list``).  The write path for
those rows — the ARQ audit task — runs under an explicit RLS bypass,
because a row that belongs to no organization cannot be admitted by a
tenant-scoped policy.

Table count: 24 tables carry a policy at this revision — the 19 below plus
the five added here.  The five (``custom_instructions``,
``episode_blobs``, ``fact_invalidation_events``, ``prompt_templates`` and
``webhook_endpoints``) carry an ``organization_id`` but had **no** row-level
security at all before this revision: no policy and ``relrowsecurity =
false``.  The other 19 were created with ``ENABLE ROW LEVEL SECURITY`` and
only ever missed the ``FORCE`` flag (the owner bypassed their inert
policies), so ``FORCE`` alone is enough for them.  The five therefore need
``ENABLE`` as well, and the downgrade has to ``DISABLE`` (and drop the
policy) to restore the pre-revision catalog state exactly.

Revision ID: 0059
Revises: 0058
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ── Policy shapes ───────────────────────────────────────────────────────────
#
# Each tenant table uses one of four predicates.  The only difference
# between the pre-0059 and the post-0059 form is the ``app.org_id``
# expression: ``current_setting('app.org_id')::UUID`` (raises on an unset
# context) becomes ``NULLIF(current_setting('app.org_id', true), '')::UUID``
# (evaluates to NULL, i.e. matches nothing).

_ADDED_ORG_SCOPED_TABLES: tuple[str, ...] = (
    "custom_instructions",
    "episode_blobs",
    "fact_invalidation_events",
    "prompt_templates",
    "webhook_endpoints",
)
"""Tables this revision adds to the org-scoped set.

They carry an ``organization_id`` like every other tenant table, but they
were created **without** ``ENABLE ROW LEVEL SECURITY`` — so unlike the 19
tables below, they need ``ENABLE`` as well as ``FORCE`` here, and the
downgrade has to ``DISABLE`` and drop the policy to restore the
pre-revision catalog state exactly.
"""

_ORG_SCOPED_TABLES: tuple[str, ...] = (
    "api_keys",
    "dialog_classifications",
    "episodes",
    "extraction_schemas",
    "facts",
    "graph_entities",
    "graph_observations",
    "graph_relationships",
    "llm_usage",
    "project_pins",
    "projects",
    "refresh_tokens",
    "sessions",
    "structured_extractions",
    "users",
    *_ADDED_ORG_SCOPED_TABLES,
)
"""Tables whose tenant column is ``organization_id``."""

_PROJECT_SCOPED_TABLES: tuple[str, ...] = (
    "graph_episode_entities",
    "project_members",
)
"""Tables without ``organization_id`` — tenant scope via the parent project."""

_SELF_SCOPED_TABLE: str = "organizations"
"""The org table itself — the tenant column is ``id``."""

_NULLABLE_ORG_TABLE: str = "audit_logs"
"""Table whose ``organization_id`` may be NULL (platform-level rows)."""


def _org_id_expr(missing_ok: bool) -> str:
    """Render the ``app.org_id`` comparison expression.

    Args:
        missing_ok: ``True`` for the fail-closed form used after this
            revision (``NULLIF(current_setting('app.org_id', true), '')::UUID``);
            ``False`` restores the original form
            (``current_setting('app.org_id')::UUID``) on downgrade.

    Returns:
        The SQL expression as a string.
    """
    if missing_ok:
        return "NULLIF(current_setting('app.org_id', true), '')::UUID"
    return "current_setting('app.org_id')::UUID"


def _policy_body(table: str, missing_ok: bool, *, admit_null_org: bool) -> str:
    """Build the ``USING`` expression for a tenant policy.

    Args:
        table: The table the policy is created on.
        missing_ok: See :func:`_org_id_expr`.
        admit_null_org: Whether rows **without** an organization are visible
            to a tenant.  Only the pre-0059 ``audit_logs`` policy admitted
            them; the tightened policy does not, so a tenant can no longer
            read the platform-level audit rows (which carry another
            tenant's client IPs and request ids).

    Returns:
        The policy body as a SQL string.
    """
    org_id = _org_id_expr(missing_ok)
    bypass = "current_setting('app.bypass_rls', true) = 'true'"

    if table == _SELF_SCOPED_TABLE:
        return f"{bypass}\n                OR id = {org_id}"
    if table == _NULLABLE_ORG_TABLE:
        null_branch = (
            "                OR organization_id IS NULL\n" if admit_null_org else ""
        )
        return f"{bypass}\n{null_branch}                OR organization_id = {org_id}"
    if table in _PROJECT_SCOPED_TABLES:
        return (
            f"{bypass}\n                OR project_id IN (\n"  # noqa: S608
            "                    SELECT id FROM projects\n"
            f"                    WHERE organization_id = {org_id}\n"
            "                )"
        )
    return f"{bypass}\n                OR organization_id = {org_id}"


def _all_policy_tables() -> tuple[str, ...]:
    """Return every table that carries a tenant policy.

    Returns:
        The table names, in a stable order.
    """
    return (
        _SELF_SCOPED_TABLE,
        _NULLABLE_ORG_TABLE,
        *_ORG_SCOPED_TABLES,
        *_PROJECT_SCOPED_TABLES,
    )


def _tables_with_prior_policies() -> tuple[str, ...]:
    """Return the tables that carried a policy **before** this revision.

    The five tables this revision adds had no policy at all — their
    downgrade path drops the policy instead of restoring the pre-0059
    expression.

    Returns:
        The table names, in a stable order.
    """
    return tuple(t for t in _all_policy_tables() if t not in _ADDED_ORG_SCOPED_TABLES)


def _recreate_policies(
    missing_ok: bool,
    tables: Sequence[str] | None = None,
    *,
    admit_null_org: bool = False,
) -> None:
    """Drop and recreate every tenant policy.

    Args:
        missing_ok: See :func:`_org_id_expr`.
        tables: The tables to (re)create policies for.  ``None`` means
            every policy table.
        admit_null_org: See :func:`_policy_body`.  The downgrade sets this
            to ``True`` so the pre-0059 ``audit_logs`` policy — which
            admitted the organization-less rows — is restored exactly.
    """
    for table in tables if tables is not None else _all_policy_tables():
        op.execute(f"DROP POLICY IF EXISTS org_isolation_{table} ON {table}")
        op.execute(
            f"""
            CREATE POLICY org_isolation_{table} ON {table}
            FOR ALL
            USING (
                {_policy_body(table, missing_ok, admit_null_org=admit_null_org)}
            )
            """
        )


def upgrade() -> None:
    """Force RLS on every tenant table and install the fail-closed policies."""
    _recreate_policies(missing_ok=True)

    # The five tables added by this revision never had RLS enabled — and
    # ``FORCE`` is a no-op without ``ENABLE``, so enable them first.
    for table in _ADDED_ORG_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

    for table in _all_policy_tables():
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # ── Public join path: read exactly one org by its join code ───────────
    # SELECT-only, so it does not widen INSERT/UPDATE/DELETE.
    op.execute("""
        CREATE POLICY org_join_code_lookup ON organizations
        FOR SELECT
        USING (
            NULLIF(current_setting('app.lookup_code', true), '') IS NOT NULL
            AND org_code = current_setting('app.lookup_code', true)
        )
    """)

    # ── Public signup path: create a brand-new org row ─────────────────────
    # INSERT-only.  The signup path mints the organization id itself and
    # binds the session to that id **before** the insert, so the row can be
    # created by a session that is already scoped to the tenant it is
    # creating.  The CHECK is therefore the same expression the SELECT
    # policy uses — an insert whose row id is not the bound tenant is
    # refused, so a session bound to tenant A can no longer create an
    # organization row for tenant B (or for an arbitrary id).
    #
    # NOTE: a session that inserts an org row WITHOUT binding itself to that
    # org cannot read the row back — PostgreSQL filters the ``RETURNING``
    # clause of the INSERT with the table's SELECT policy, and an empty
    # RETURNING raises "new row violates row-level security policy" even
    # though the INSERT's own CHECK passed.  That is why the organization id
    # is minted in Python and bound to the session before the flush (see
    # ``AuthRepository.create_organization``): the RETURNING clause is
    # filtered by the SELECT policy, so the session must already be bound
    # to the new organization when the INSERT runs.
    op.execute("""
        CREATE POLICY org_anon_insert ON organizations
        FOR INSERT
        WITH CHECK (
            id = NULLIF(current_setting('app.org_id', true), '')::UUID
        )
    """)


def downgrade() -> None:
    """Restore the inert policies and release the FORCE flag.

    Rollback note: run ``alembic downgrade -1`` with the same
    ``OZ_DATABASE_URL`` used for the upgrade.  The downgrade restores the
    exact pre-0059 policy expressions and drops the FORCE flag, so the
    policies go back to being advisory for the table owner — no data is
    changed by the rollback itself.  The five tables this revision added
    (``custom_instructions``, ``episode_blobs``,
    ``fact_invalidation_events``, ``prompt_templates``,
    ``webhook_endpoints``) had neither a policy nor RLS before it, so
    their policy is dropped and RLS is disabled outright.
    """
    op.execute("DROP POLICY IF EXISTS org_join_code_lookup ON organizations")
    op.execute("DROP POLICY IF EXISTS org_anon_insert ON organizations")

    # ``admit_null_org=True`` restores the pre-0059 ``audit_logs`` policy
    # verbatim — it admitted the organization-less rows, which the tightened
    # policy no longer does.
    _recreate_policies(
        missing_ok=False,
        tables=_tables_with_prior_policies(),
        admit_null_org=True,
    )

    for table in _all_policy_tables():
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

    # Restore "no RLS at all" for the five tables this revision added.
    for table in _ADDED_ORG_SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS org_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
