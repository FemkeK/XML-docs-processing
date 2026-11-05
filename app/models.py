"""Database model. The `documents` table is the storage and the work queue at the same time."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class DocumentStatus(StrEnum):
    """Lifecycle of a document. repository.py lists the allowed transitions."""

    RECEIVED = "RECEIVED"  # Stored and waiting for a worker.
    PROCESSING = "PROCESSING"  # A worker holds the lease and is processing it.
    RETRYING = "RETRYING"  # An attempt failed transiently; waiting until next_attempt_at.
    COMPLETED = "COMPLETED"  # Final state: result is available.
    FAILED = "FAILED"  # Final state: permanent error or attempts used up.


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetime on every database.

    PostgreSQL stores the offset itself. SQLite has no timezone type, so values are
    stored as naive UTC and become timezone-aware again when read.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed; use datetime.now(UTC)")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    """Base class of all models. `Base.metadata` describes the full schema."""


class Document(Base):
    """One received XML document: the input, its status, the result and the queue fields."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Stored as plain text (native_enum=False), not as a database enum type. Adding a
    # status later then needs no ALTER TYPE migration.
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, native_enum=False, length=20)
    )
    # Raw XML exactly as received, so it can always be processed again.
    content: Mapped[bytes] = mapped_column(LargeBinary)
    # Processing result, set once COMPLETED. JSONB on PostgreSQL (searchable with SQL),
    # plain JSON on SQLite.
    result: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    # Optional key from the client's Idempotency-Key header. A resubmission with the same
    # key returns the existing document instead of creating a duplicate.
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    # SHA-256 of `content`. Used to detect when a key is reused for a different document.
    content_sha256: Mapped[str | None] = mapped_column(String(64))

    # Number of times a worker has claimed this document. Reset by a manual requeue.
    attempts: Mapped[int] = mapped_column(default=0)
    # Message of the most recent failure; cleared on success or requeue.
    last_error: Mapped[str | None] = mapped_column(Text)
    # Earliest moment a worker may (re)process the document.
    next_attempt_at: Mapped[datetime] = mapped_column(UtcDateTime)
    # Lease: while PROCESSING, the document belongs to `locked_by` until `locked_until`.
    # If the worker dies, the lease runs out and another worker picks the document up.
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime)

    received_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)

    # Optimistic locking: every UPDATE checks the version it read. A write based on stale
    # data (for example, a worker whose lease was taken over) fails instead of silently
    # overwriting newer state.
    version: Mapped[int] = mapped_column(default=1)

    events: Mapped[list["DocumentEvent"]] = relationship(
        back_populates="document", order_by="DocumentEvent.id", cascade="all, delete-orphan"
    )

    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        # Supports the worker's claim query: "next eligible document".
        Index("ix_documents_status_next_attempt", "status", "next_attempt_at"),
        # Guarantees one document per key, even when two requests arrive at the same moment.
        UniqueConstraint("idempotency_key", name="uq_documents_idempotency_key"),
    )


class DocumentEvent(Base):
    """Status history (audit trail): one row per state change."""

    __tablename__ = "document_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, native_enum=False, length=20)
    )
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)

    document: Mapped[Document] = relationship(back_populates="events")
