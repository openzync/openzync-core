"""Add CHECK constraints preventing empty-array embeddings.

Context
-------
Embeddings are stored in ``double precision[]`` columns rather than
pgvector's ``vector(N)`` because different organisations use embedding
models with different output dimensions (768, 1536, 3072).

The original ``0017`` draft (abandoned)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
This migration was initially written to convert the ``embedding``
columns from ``double precision[]`` to ``vector(4096)`` so that
pgvector operators (``<=>``) could be used directly on the storage
column.

That approach was **abandoned** because ``vector(N)`` is
fixed-dimension — a single ``vector(4096)`` column cannot store rows
with 768 or 1536 dimensions:

.. code:: sql

    ALTER TABLE facts ALTER COLUMN embedding TYPE vector(4096)
      USING embedding::vector(4096);
    -- ERROR:  expected 4096 dimensions, not 768

Instead, ``float8[]`` storage is retained and cast to ``vector(N)``
only at query time using the per-org ``embedding_dim``.

The runtime approach
~~~~~~~~~~~~~~~~~~~~
.. code:: python

    from pgvector.sqlalchemy import Vector
    from sqlalchemy import cast

    cast(Fact.embedding, Vector(org.embedding_dim)).op("<=>")(query_vec)

Renders as:

.. code:: sql

    CAST(facts.embedding AS VECTOR(768)) <=> '[0.79,…]'::vector(768)

Why a CHECK constraint?
~~~~~~~~~~~~~~~~~~~~~~~
The ``fact_repository`` previously inserted facts with
``"embedding": []``, which PostgreSQL stores as ``{}``
(empty array).  At query time ``CAST({} AS vector(N))`` throws
``vector must have at least 1 dimension``.

The ``embedding`` column is set to ``None`` at insert time and
backfilled asynchronously by the ARQ embedding worker.  A CHECK
constraint ensures that if a row ever has a non-NULL embedding it
must have at least one element — preventing the empty-array crash
at the database level regardless of application bugs.

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-17 10:00:00.000000
"""

from typing import Sequence, Union
from alembic import op
from sqlalchemy import text

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add CHECK constraints preventing empty-array embeddings."""
    op.create_check_constraint(
        "ck_facts_embedding_not_empty",
        "facts",
        text("embedding IS NULL OR cardinality(embedding) > 0"),
    )
    op.create_check_constraint(
        "ck_episodes_embedding_not_empty",
        "episodes",
        text("embedding IS NULL OR cardinality(embedding) > 0"),
    )


def downgrade() -> None:
    """Drop the CHECK constraints."""
    op.drop_constraint("ck_facts_embedding_not_empty", "facts", type_="check")
    op.drop_constraint("ck_episodes_embedding_not_empty", "episodes", type_="check")
