# Database Migration Runbook

## Overview

OpenZep uses **Alembic** for PostgreSQL schema migrations. All schema changes go through a versioned migration — no `create_all()` in production. This runbook covers the workflow, zero-downtime patterns, and rollback procedures.

---

## Alembic Setup

### Installation

```bash
pip install alembic psycopg2-binary
```

### Initialization

```bash
# From the project root
cd services/api
alembic init alembic
```

### `alembic.ini`

```ini
[alembic]
script_location = services/api/alembic
sqlalchemy.url = postgresql://OpenZep:OpenZep@localhost:5432/OpenZep
# ^ Sync driver (psycopg2) for migration commands.
# The application uses async driver (asyncpg) at runtime.

# Logging
loggers = sqlalchemy
[loggers_sqlalchemy]
level = WARN
```

### `alembic/env.py`

```python
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.db import Base  # Import all models to register metadata
from app.models import *       # noqa: F401, F403 — ensures all models loaded

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_sync_migrations(connection: Connection):
    """Run migrations in sync mode (default for alembic)."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,    # Detect column type changes
        compare_server_default=True,  # Detect default value changes
    )
    with context.begin_transaction():
        context.run_migrations()


def run_async_migrations():
    """Run migrations asynchronously (for async engine)."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        run_sync_migrations(connection)

    connectable.dispose()


if context.is_offline_mode():
    # Generate SQL script (offline)
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    run_async_migrations()
```

---

## Migration Naming Convention

```
{YYYYMMDD}_{short_description}.py
```

**Examples:**

| File | Description |
|---|---|
| `20260605_initial_schema.py` | Baseline migration |
| `20260610_add_structured_extractions.py` | New table |
| `20260615_add_confidence_to_facts.py` | Add column |
| `20260620_add_user_search_index.py` | Add index |
| `20260701_make_email_unique.py` | Add constraint |

---

## Migration Workflow

### Step 1: Create Migration

```bash
# Autogenerate from model changes
cd services/api
alembic revision --autogenerate -m "add confidence column to facts"
```

This creates a file like `alembic/versions/20260615_add_confidence_to_facts.py`.

### Step 2: Review Auto-Generated Migration

Alembic can miss:
- Index changes (especially custom index types like pgvector `ivfflat`)
- Constraint name changes
- Trigger creation
- Materialized views

**Example of what Alembic gets right and wrong:**

```python
# Auto-generated (usually correct for simple additions)
def upgrade():
    op.add_column("facts", sa.Column("confidence", sa.Float4(), nullable=True))

# === REVIEW AND FIX ===

def upgrade():
    op.add_column("facts", sa.Column("confidence", sa.Float4(), nullable=True))

    # Alembic MISSED this index — added manually
    op.create_index(
        "ix_facts_confidence",
        "facts",
        ["confidence"],
        postgresql_using="btree",
    )


def downgrade():
    op.drop_index("ix_facts_confidence")
    op.drop_column("facts", "confidence")
```

### Step 3: Add Manual Index/Constraint Changes

Always check:
1. **pgvector indexes**: Alembic does not detect `IVFFlat` or `HNSW` index changes.

```python
# Manual pgvector index (Alembic will not autogenerate this)
def upgrade():
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_facts_embedding ON facts "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )

def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_facts_embedding")
```

2. **GIN indexes for full-text search**:

```python
def upgrade():
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_episodes_content_gin "
        "ON episodes USING GIN (to_tsvector('english', content))"
    )

def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_episodes_content_gin")
```

3. **Unique constraints across columns**:

```python
def upgrade():
    op.create_unique_constraint(
        "uq_users_org_external",
        "users",
        ["organization_id", "external_id"],
    )

def downgrade():
    op.drop_constraint("uq_users_org_external", "users", type_="unique")
```

### Step 4: Always Implement `downgrade()`

Every migration must be reversible:

```python
"""add confidence column to facts

Revision ID: abc123
Revises: def456
Create Date: 2026-06-15 10:30:00.000000
"""


def upgrade():
    op.add_column("facts", sa.Column("confidence", sa.Float4(), nullable=True))
    op.create_index("ix_facts_confidence", "facts", ["confidence"])


def downgrade():
    op.drop_index("ix_facts_confidence")
    op.drop_column("facts", "confidence")
```

### Step 5: Test

```bash
# Upgrade
alembic upgrade head

# Verify schema
psql -U OpenZep -d OpenZep -c "\d facts"

# Downgrade one step
alembic downgrade -1

# Verify rollback worked
psql -U OpenZep -d OpenZep -c "\d facts"

# Re-upgrade
alembic upgrade head
```

---

## Zero-Downtime Migration Patterns

### Pattern 1: Adding a Column (Safe)

```sql
ALTER TABLE facts ADD COLUMN confidence FLOAT4 DEFAULT 1.0;
```

**Safety**: Existing rows get `DEFAULT` (or NULL if no default). No locking issue.

```python
def upgrade():
    op.add_column(
        "facts",
        sa.Column("confidence", sa.Float4(), server_default="1.0", nullable=False),
    )

def downgrade():
    op.drop_column("facts", "confidence")
```

### Pattern 2: Adding a NOT NULL Column (Multi-Step)

Adding a NOT NULL column to a table with existing rows requires multiple steps:

**Step 1**: Add column as nullable
```python
def upgrade():
    op.add_column("facts", sa.Column("confidence", sa.Float4(), nullable=True))
```

**Step 2**: Deploy code that writes to the new column.

**Step 3**: Backfill existing rows
```sql
UPDATE facts SET confidence = 1.0 WHERE confidence IS NULL;
```

**Step 4**: Add NOT NULL constraint
```python
def upgrade():
    op.alter_column("facts", "confidence", nullable=False)
```

**Combined migration**:

```python
def upgrade():
    # 1. Add as nullable
    op.add_column("facts", sa.Column("confidence", sa.Float4(), nullable=True))
    # 2. Backfill (safest approach — run UPDATE)
    op.execute("UPDATE facts SET confidence = 1.0 WHERE confidence IS NULL")
    # 3. Set NOT NULL
    op.alter_column("facts", "confidence", nullable=False, server_default="1.0")


def downgrade():
    op.drop_column("facts", "confidence")
```

### Pattern 3: Renaming a Column (Multi-Step Deploy)

**Do NOT rename in one step** — it breaks running code that references the old name.

**Deploy 1**: Add new column + dual-write
```python
def upgrade():
    op.add_column("users", sa.Column("display_name", sa.String(255), nullable=True))
    # Copy existing values
    op.execute("UPDATE users SET display_name = name WHERE display_name IS NULL")
```

- Deploy code that writes to both `name` and `display_name`.
- Reads still use `name`.

**Deploy 2**: Migrate reads
- Deploy code that reads from `display_name` instead of `name`.
- Old `name` column is now unused.

**Deploy 3**: Drop old column
```python
def upgrade():
    op.drop_column("users", "name")
```

### Pattern 4: Dropping a Column (Two-Step)

**Deploy 1**: Remove all code references to the column. Deploy and verify.

**Deploy 2**: Drop the column
```python
def upgrade():
    op.drop_column("users", "legacy_field")
```

**Never** drop a column in the same deploy that removes code references.

### Pattern 5: Dropping a Table (Two-Step)

**Deploy 1**: Remove all code references to the table.

**Deploy 2**: Drop the table
```python
def upgrade():
    op.drop_table("obsolete_table")
```

---

## Rollback Procedure

### Standard Rollback

```bash
# Rollback one migration
alembic downgrade -1

# Rollback to specific revision
alembic downgrade <revision_id>

# Rollback to base (full rollback — all migrations reversed)
alembic downgrade base
```

### Rollback with Data Preservation

If a migration drops a column or table, the downgrade must restore the data:

```python
def upgrade():
    # Danger: data loss on upgrade
    op.drop_column("users", "temporary_field")

def downgrade():
    # Restore column (data is lost — warn in the migration)
    op.add_column("users", sa.Column("temporary_field", sa.Text(), nullable=True))
```

**Always test rollback before production deploy:**

```bash
# In staging:
alembic upgrade head
# Verify everything works
alembic downgrade -1
# Verify rollback is clean
alembic upgrade head
```

---

## Concurrent Migration Prevention

Multiple API instances must never run migrations simultaneously. Use a database advisory lock:

### `alembic/env.py`

```python
def run_migrations_online():
    """Run migrations with an advisory lock to prevent concurrent runs."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # Acquire advisory lock (lock ID = migration script)
        connection.execute(text("SELECT pg_advisory_lock(20260605)"))
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=True,
            )
            with context.begin_transaction():
                context.run_migrations()
        finally:
            # Release advisory lock
            connection.execute(text("SELECT pg_advisory_unlock(20260605)"))
```

### In Kubernetes (Helm Pre-Upgrade Hook)

```yaml
# templates/job-migrate.yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "OpenZep.fullname" . }}-migrate
  annotations:
    "helm.sh/hook": pre-upgrade,pre-install
    "helm.sh/hook-weight": "-5"
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  template:
    spec:
      restartPolicy: OnFailure
      containers:
        - name: migrate
          image: "{{ .Values.global.imageRegistry }}/OpenZep-api:{{ .Values.global.imageTag }}"
          command: ["alembic", "upgrade", "head"]
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: OpenZep-secrets
                  key: database_url
```

---

## Migration Status Commands

```bash
# Show current revision
alembic current

# Show migration history
alembic history

# Show pending migrations
alembic check

# Generate SQL script (dry run — offline mode)
alembic upgrade head --sql > migration.sql

# Mark migration as applied without running it (emergency only)
alembic stamp <revision_id>
```

---

## Production Deploy Checklist

- [ ] Migration tested on staging environment.
- [ ] Downgrade tested — `alembic downgrade -1` works cleanly.
- [ ] No destructive changes in the same deploy as code changes (two-step for drops/renames).
- [ ] Migration has no long-running locks (test with `BEGIN; ... ; ROLLBACK` first).
- [ ] Backup taken before running migration in production.
- [ ] Migration run via Helm pre-upgrade hook or CI/CD job (not ad-hoc).
- [ ] Migration duration estimated: `EXPLAIN ANALYZE` on any ALTER TABLE or UPDATE.
- [ ] Rollback plan documented in the deploy ticket.

### Lock Duration Estimation

```sql
-- Test migration duration without committing
BEGIN;
ALTER TABLE facts ADD COLUMN confidence FLOAT4;
ROLLBACK;  -- Time this to estimate lock duration
```

For tables with > 1M rows, expect:
- `ADD COLUMN`: < 1 second (metadata only)
- `ADD COLUMN NOT NULL`: minutes (full table rewrite)
- `DROP COLUMN`: < 1 second (metadata only for PG 11+, slower before)
- `ADD INDEX`: minutes for large tables (allow concurrent builds with `CONCURRENTLY`)

---

## Baseline Migration

```python
"""initial schema

Revision ID: 20260605_initial_schema
Revises:
Create Date: 2026-06-05 00:00:00.000000
"""


def upgrade():
    # Organizations
    op.create_table(
        "organizations",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("plan", sa.Text(), server_default="free", nullable=False),
        sa.Column("quotas", sa.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # API Keys
    op.create_table(
        "api_keys",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("scopes", sa.ARRAY(sa.Text()), server_default=sa.text("ARRAY['read','write']"), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )

    # Users
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("metadata_", sa.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "external_id"),
    )

    # Sessions
    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("metadata_", sa.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "external_id"),
    )

    # Episodes
    op.create_table(
        "episodes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_", sa.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=True),
        sa.Column("embedding", sa.Vector(dim=1536), nullable=True),
        sa.Column("graphiti_node_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # pgvector index
    op.execute(
        "CREATE INDEX ix_episodes_embedding ON episodes "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )
    op.execute(
        "CREATE INDEX ix_episodes_content_gin ON episodes "
        "USING GIN (to_tsvector('english', content))"
    )

    # Facts
    op.create_table(
        "facts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("predicate", sa.Text(), nullable=True),
        sa.Column("object", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float4(), server_default="1.0", nullable=False),
        sa.Column("source_episode_id", sa.UUID(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embedding", sa.Vector(dim=1536), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_episode_id"], ["episodes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.execute(
        "CREATE INDEX ix_facts_embedding ON facts "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )

    # Dialog classifications
    op.create_table(
        "dialog_classifications",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("episode_id", sa.UUID(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=True),
        sa.Column("emotion", sa.Text(), nullable=True),
        sa.Column("valence", sa.Text(), nullable=True),
        sa.Column("arousal", sa.Text(), nullable=True),
        sa.Column("raw", sa.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["episode_id"], ["episodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # Structured extractions
    op.create_table(
        "structured_extractions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("schema_id", sa.UUID(), nullable=True),
        sa.Column("data_", sa.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("structured_extractions")
    op.drop_table("dialog_classifications")
    op.drop_table("facts")
    op.drop_table("episodes")
    op.drop_table("sessions")
    op.drop_table("users")
    op.drop_table("api_keys")
    op.drop_table("organizations")
```
