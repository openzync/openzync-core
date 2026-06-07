"""remove_duplicate_user_id_in_session

Removes a duplicate ``user_id`` mapped_column declaration from the
``Session`` ORM model (``models/session.py:35-39``).

The DB schema already has exactly one ``user_id`` column — the second
Python declaration was shadowed by SQLAlchemy and never produced an
extra column.  This migration is a **no-op** at the schema level; it
exists to mark the model fix in the migration history.

Revision ID: 99a299513d53
Revises: 0006
Create Date: 2026-06-07 10:29:21.689192
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '99a299513d53'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op — the DB schema was never affected by the duplicated ORM column."""
    pass


def downgrade() -> None:
    """No-op — see upgrade()."""
    pass
