"""Initial schema: documents (storage + queue) and document_events (status history).

Revision ID: 0001
Revises:
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Status is stored as a plain string (no database enum type), so adding a status later
# needs no ALTER TYPE.
STATUS = sa.Enum(
    "RECEIVED",
    "PROCESSING",
    "RETRYING",
    "COMPLETED",
    "FAILED",
    name="documentstatus",
    native_enum=False,
    length=20,
)


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", STATUS, nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "result", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(length=100), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_documents_status_next_attempt", "documents", ["status", "next_attempt_at"])

    op.create_table(
        "document_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("status", STATUS, nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_events_document_id", "document_events", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_document_events_document_id", table_name="document_events")
    op.drop_table("document_events")
    op.drop_index("ix_documents_status_next_attempt", table_name="documents")
    op.drop_table("documents")
