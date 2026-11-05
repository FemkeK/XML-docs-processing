"""Idempotent submission: Idempotency-Key and content hash on documents.

Both columns are nullable. Rows that already exist have no key, and a key is optional.
The unique constraint allows any number of NULLs, on PostgreSQL and on SQLite.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.add_column(sa.Column("idempotency_key", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("content_sha256", sa.String(length=64), nullable=True))
        batch.create_unique_constraint("uq_documents_idempotency_key", ["idempotency_key"])


def downgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.drop_constraint("uq_documents_idempotency_key", type_="unique")
        batch.drop_column("content_sha256")
        batch.drop_column("idempotency_key")
