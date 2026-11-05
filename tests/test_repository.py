"""Queue semantics: claiming, leases and state transitions."""

from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app import repository
from app.models import DocumentStatus
from tests.conftest import load_sample

LEASE = timedelta(seconds=30)


@pytest.fixture
def document_id(session_factory, clock):
    with session_factory.begin() as session:
        return repository.create_document(session, load_sample("valid_order.xml"), clock()).id


def _claim(session_factory, clock, worker_id="worker-1"):
    with session_factory.begin() as session:
        return repository.claim_next(session, worker_id, clock(), LEASE)


def _load(session_factory, document_id):
    with session_factory() as session:
        return repository.get_document(session, document_id, with_events=True)


def test_database_errors_do_not_expose_query_parameters(session_factory, clock):
    # Database error messages end up in logs (tracebacks). The parameters include the raw
    # XML, so the engine is set up to leave them out.
    with session_factory.begin() as session:
        repository.create_document(session, b"<order/>", clock(), "key-1")

    with pytest.raises(IntegrityError) as exc_info, session_factory.begin() as session:
        repository.create_document(session, b"<order>secret</order>", clock(), "key-1")

    # Only the content is checked: PostgreSQL itself names the conflicting key in its
    # message ("Key (idempotency_key)=(key-1) already exists"). That is a random ID from
    # the client, not personal data.
    assert "secret" not in str(exc_info.value)


def test_new_document_is_received_with_history(session_factory, document_id):
    document = _load(session_factory, document_id)
    assert document.status == DocumentStatus.RECEIVED
    assert document.attempts == 0
    assert [event.status for event in document.events] == [DocumentStatus.RECEIVED]


def test_claim_sets_lease_and_is_exclusive(session_factory, clock, document_id):
    claimed = _claim(session_factory, clock)
    assert claimed.id == document_id
    assert claimed.attempt == 1

    document = _load(session_factory, document_id)
    assert document.status == DocumentStatus.PROCESSING
    assert document.locked_by == "worker-1"

    # The lease is still running, so a second worker gets nothing.
    assert _claim(session_factory, clock, "worker-2") is None


def test_expired_lease_is_taken_over_and_old_claim_becomes_invalid(
    session_factory, clock, document_id
):
    stale_claim = _claim(session_factory, clock, "worker-1")
    clock.advance(seconds=LEASE.total_seconds() + 1)  # worker-1 "crashed"

    new_claim = _claim(session_factory, clock, "worker-2")
    assert new_claim.id == document_id
    assert new_claim.attempt == 2

    # worker-1 comes back to life: its result must not overwrite worker-2's work.
    with session_factory.begin() as session:
        assert not repository.mark_completed(session, stale_claim, {"stale": True}, clock())
    with session_factory.begin() as session:
        assert repository.mark_completed(session, new_claim, {"fresh": True}, clock())

    document = _load(session_factory, document_id)
    assert document.status == DocumentStatus.COMPLETED
    assert document.result == {"fresh": True}
    assert "previous lease expired" in document.events[-2].message


def test_retry_waits_until_next_attempt_at(session_factory, clock, document_id):
    claimed = _claim(session_factory, clock)
    with session_factory.begin() as session:
        retry_at = clock() + timedelta(seconds=10)
        repository.schedule_retry(session, claimed, "timeout", retry_at, clock())

    assert _claim(session_factory, clock) is None  # not yet due
    clock.advance(seconds=11)
    assert _claim(session_factory, clock).attempt == 2


def test_requeue_only_allowed_for_failed(session_factory, clock, document_id):
    with session_factory.begin() as session:
        document = repository.get_document(session, document_id)
        with pytest.raises(ValueError, match="Only FAILED documents can be requeued"):
            repository.requeue(session, document, clock())

    claimed = _claim(session_factory, clock)
    with session_factory.begin() as session:
        repository.mark_failed(session, claimed, "broken", clock())
    with session_factory.begin() as session:
        repository.requeue(session, repository.get_document(session, document_id), clock())

    document = _load(session_factory, document_id)
    assert document.status == DocumentStatus.RECEIVED
    assert document.attempts == 0
    assert document.last_error is None


def test_list_filters_by_status(session_factory, clock, document_id):
    with session_factory.begin() as session:
        repository.create_document(session, load_sample("valid_order.xml"), clock())
    _claim(session_factory, clock)

    with session_factory() as session:
        assert len(repository.list_documents(session, None, 10, 0)) == 2
        processing = repository.list_documents(session, DocumentStatus.PROCESSING, 10, 0)
        assert len(processing) == 1
