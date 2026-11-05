"""Data access and state transitions for documents (the database queue).

Every function here works inside the caller's session/transaction and does not commit.
The caller decides where the transaction ends. Every status change also writes a
DocumentEvent, so the history is always complete.

State transitions:

    RECEIVED   -> PROCESSING   claim_next      (a worker takes the document)
    RETRYING   -> PROCESSING   claim_next      (backoff has passed)
    PROCESSING -> PROCESSING   claim_next      (lease expired: previous worker crashed)
    PROCESSING -> COMPLETED    mark_completed
    PROCESSING -> RETRYING     schedule_retry  (transient error)
    PROCESSING -> FAILED       mark_failed     (permanent error or attempts used up)
    FAILED     -> RECEIVED     requeue         (manual, by an operator)
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import Document, DocumentEvent, DocumentStatus

# Statuses a worker may pick up once next_attempt_at has passed.
_WAITING = (DocumentStatus.RECEIVED, DocumentStatus.RETRYING)


def utcnow() -> datetime:
    """Current time, timezone-aware in UTC. All timestamps in the system use this."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class ClaimedDocument:
    """Snapshot of a claimed document. The worker processes it outside any transaction.

    `attempt` identifies the claim: the worker may only write a result while the
    document is still PROCESSING under this worker with this attempt number.
    """

    id: uuid.UUID
    content: bytes
    attempt: int
    worker_id: str


def _transition(
    session: Session, document: Document, status: DocumentStatus, message: str, now: datetime
) -> None:
    """Change the status and record it in the history. The only place a status changes."""
    document.status = status
    document.updated_at = now
    session.add(DocumentEvent(document=document, status=status, message=message, created_at=now))


def content_hash(content: bytes) -> str:
    """SHA-256 of the raw XML, to recognise a resubmission of the same document."""
    return hashlib.sha256(content).hexdigest()


def create_document(
    session: Session, content: bytes, now: datetime, idempotency_key: str | None = None
) -> Document:
    """Store a validated document and queue it for processing.

    Raises IntegrityError if another document already has this idempotency key.
    """
    document = Document(
        id=uuid.uuid4(),
        content=content,
        content_sha256=content_hash(content),
        idempotency_key=idempotency_key,
        attempts=0,
        next_attempt_at=now,
        received_at=now,
    )
    _transition(session, document, DocumentStatus.RECEIVED, "Document received and queued", now)
    session.add(document)
    session.flush()
    return document


def get_document(
    session: Session, document_id: uuid.UUID, with_events: bool = False
) -> Document | None:
    """Load a document, optionally with its history in the same round trip."""
    stmt = select(Document).where(Document.id == document_id)
    if with_events:
        stmt = stmt.options(selectinload(Document.events))
    return session.scalars(stmt).one_or_none()


def find_by_idempotency_key(session: Session, idempotency_key: str) -> Document | None:
    stmt = select(Document).where(Document.idempotency_key == idempotency_key)
    return session.scalars(stmt).one_or_none()


def list_documents(
    session: Session, status: DocumentStatus | None, limit: int, offset: int
) -> list[Document]:
    """One page of documents, newest first, optionally filtered by status."""
    stmt = select(Document).order_by(Document.received_at.desc()).limit(limit).offset(offset)
    if status is not None:
        stmt = stmt.where(Document.status == status)
    return list(session.scalars(stmt))


def claim_next(
    session: Session, worker_id: str, now: datetime, lease: timedelta
) -> ClaimedDocument | None:
    """Claim the next document that is ready for processing, or return None.

    Candidates are waiting documents whose next_attempt_at has passed, and PROCESSING
    documents whose lease has expired (their worker crashed or hung).

    On PostgreSQL, `FOR UPDATE SKIP LOCKED` lets several workers claim at the same time
    without blocking each other or picking the same row. On SQLite that clause does not
    exist. There the optimistic `version` check guarantees that at most one claim wins.
    """
    stmt = (
        select(Document)
        .where(
            or_(
                and_(Document.status.in_(_WAITING), Document.next_attempt_at <= now),
                and_(Document.status == DocumentStatus.PROCESSING, Document.locked_until < now),
            )
        )
        # Longest-waiting first, so no document is starved by newer ones.
        .order_by(Document.next_attempt_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    document = session.scalars(stmt).first()
    if document is None:
        return None

    lease_expired = document.status == DocumentStatus.PROCESSING
    # The new attempt number also identifies this claim (see ClaimedDocument).
    document.attempts += 1
    document.locked_by = worker_id
    document.locked_until = now + lease
    message = f"Attempt {document.attempts} started by {worker_id}"
    if lease_expired:
        message += " (previous lease expired)"
    _transition(session, document, DocumentStatus.PROCESSING, message, now)
    session.flush()

    return ClaimedDocument(
        id=document.id, content=document.content, attempt=document.attempts, worker_id=worker_id
    )


def _owned_document(session: Session, claimed: ClaimedDocument) -> Document | None:
    """Return the document only if the given claim is still the active one."""
    document = session.get(Document, claimed.id)
    if (
        document is None
        or document.status != DocumentStatus.PROCESSING
        or document.locked_by != claimed.worker_id
        or document.attempts != claimed.attempt
    ):
        return None
    return document


def _release(document: Document) -> None:
    """Clear the lease once the worker is done with the document."""
    document.locked_by = None
    document.locked_until = None


def mark_completed(session: Session, claimed: ClaimedDocument, result: dict, now: datetime) -> bool:
    """Save the result. Returns False if the claim is no longer valid."""
    document = _owned_document(session, claimed)
    if document is None:
        return False
    document.result = result
    document.last_error = None
    _release(document)
    _transition(session, document, DocumentStatus.COMPLETED, "Processing completed", now)
    session.flush()
    return True


def schedule_retry(
    session: Session, claimed: ClaimedDocument, error: str, retry_at: datetime, now: datetime
) -> bool:
    """Record a transient error and schedule a new attempt at `retry_at`.

    Returns False if the claim is no longer valid.
    """
    document = _owned_document(session, claimed)
    if document is None:
        return False
    document.last_error = error
    document.next_attempt_at = retry_at
    _release(document)
    message = f"Attempt {claimed.attempt} failed, retry at {retry_at.isoformat()}: {error}"
    _transition(session, document, DocumentStatus.RETRYING, message, now)
    session.flush()
    return True


def mark_failed(session: Session, claimed: ClaimedDocument, error: str, now: datetime) -> bool:
    """Mark the document as permanently failed. Returns False if the claim is no longer valid."""
    document = _owned_document(session, claimed)
    if document is None:
        return False
    document.last_error = error
    _release(document)
    _transition(session, document, DocumentStatus.FAILED, f"Processing failed: {error}", now)
    session.flush()
    return True


def requeue(session: Session, document: Document, now: datetime) -> None:
    """Put a FAILED document back in the queue with a fresh attempt budget.

    Meant for operators, for example after a bug is fixed.
    """
    if document.status != DocumentStatus.FAILED:
        raise ValueError(f"Only FAILED documents can be requeued (status is {document.status})")
    document.attempts = 0
    document.last_error = None
    document.next_attempt_at = now
    _transition(session, document, DocumentStatus.RECEIVED, "Manually requeued", now)
    session.flush()
