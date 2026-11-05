"""Worker behaviour: success, permanent and transient errors, retries and crash recovery."""

import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app import repository
from app.models import Document, DocumentEvent, DocumentStatus
from app.processing import transform_order
from app.worker import TransientProcessingError, Worker, retry_delay, with_simulated_failures
from app.worker_healthcheck import is_alive
from tests.conftest import load_sample


def _submit(session_factory, clock, sample="valid_order.xml"):
    with session_factory.begin() as session:
        return repository.create_document(session, load_sample(sample), clock()).id


def _load(session_factory, document_id):
    with session_factory() as session:
        return repository.get_document(session, document_id, with_events=True)


def always_failing(_content: bytes) -> dict:
    raise TransientProcessingError("downstream unavailable")


def test_empty_queue_does_nothing(make_worker):
    assert make_worker().run_once() is False


def test_successful_processing_stores_result(session_factory, clock, make_worker):
    document_id = _submit(session_factory, clock)

    assert make_worker().run_once() is True

    document = _load(session_factory, document_id)
    assert document.status == DocumentStatus.COMPLETED
    assert document.result["summary"]["totalAmount"] == "160.80"
    assert document.locked_by is None
    assert [event.status for event in document.events] == [
        DocumentStatus.RECEIVED,
        DocumentStatus.PROCESSING,
        DocumentStatus.COMPLETED,
    ]


def test_permanent_error_fails_without_retry(session_factory, clock, make_worker):
    document_id = _submit(session_factory, clock, "total_mismatch_order.xml")

    make_worker().run_once()

    document = _load(session_factory, document_id)
    assert document.status == DocumentStatus.FAILED
    assert document.attempts == 1
    assert "does not match" in document.last_error


def test_error_messages_are_stored_but_not_logged(session_factory, clock, make_worker, caplog):
    def leaking_processor(_content: bytes) -> dict:
        raise ValueError("invalid customer email: jan@example.com")

    document_id = _submit(session_factory, clock)
    caplog.set_level("INFO", logger="app.worker")

    make_worker(processor=leaking_processor).run_once()

    # The full message is stored with the document, behind the same API key as the data.
    assert "jan@example.com" in _load(session_factory, document_id).last_error
    # The log only says which kind of error it was.
    logged = [record.__dict__ for record in caplog.records]
    assert any(entry.get("error_type") == "ValueError" for entry in logged)
    assert "jan@example.com" not in str(logged)


def test_transient_error_retries_with_backoff_then_gives_up(
    session_factory, clock, make_worker, settings
):
    document_id = _submit(session_factory, clock)
    worker = make_worker(processor=always_failing)

    for attempt in range(1, settings.worker_max_attempts):
        assert worker.run_once() is True
        document = _load(session_factory, document_id)
        assert document.status == DocumentStatus.RETRYING
        assert document.attempts == attempt

        # Too early: the retry is not due yet.
        assert worker.run_once() is False
        clock.advance(seconds=settings.worker_backoff_max_seconds)

    worker.run_once()  # last attempt
    document = _load(session_factory, document_id)
    assert document.status == DocumentStatus.FAILED
    assert document.attempts == settings.worker_max_attempts
    assert "Giving up after 3 attempts" in document.last_error


def test_transient_error_then_success(session_factory, clock, make_worker, settings):
    document_id = _submit(session_factory, clock)

    make_worker(processor=always_failing).run_once()
    clock.advance(seconds=settings.worker_backoff_max_seconds)
    make_worker().run_once()

    document = _load(session_factory, document_id)
    assert document.status == DocumentStatus.COMPLETED
    assert document.attempts == 2
    assert document.last_error is None


def test_crashed_worker_is_recovered_after_lease_expiry(
    session_factory, clock, make_worker, settings
):
    document_id = _submit(session_factory, clock)
    # Worker A claims the document and then "crashes": it never writes an outcome.
    with session_factory.begin() as session:
        lease = timedelta(seconds=settings.worker_lease_seconds)
        repository.claim_next(session, "crashed-worker", clock(), lease)

    worker_b = make_worker(worker_id="worker-b")
    assert worker_b.run_once() is False  # lease still valid
    clock.advance(seconds=settings.worker_lease_seconds + 1)
    assert worker_b.run_once() is True

    document = _load(session_factory, document_id)
    assert document.status == DocumentStatus.COMPLETED
    assert document.attempts == 2


def test_retry_delay_is_exponential_and_capped():
    delays = [retry_delay(attempt, 2, 30).total_seconds() for attempt in range(1, 7)]
    assert delays == [2, 4, 8, 16, 30, 30]


def test_concurrent_workers_process_each_document_exactly_once(session_factory, clock, make_worker):
    document_count = 20
    for _ in range(document_count):
        _submit(session_factory, clock)

    def run_until_empty(worker):
        idle_rounds = 0
        while idle_rounds < 3:
            try:
                did_work = worker.run_once()
            except OperationalError:
                did_work = True  # SQLite: write conflict between workers; just try again
            idle_rounds = 0 if did_work else idle_rounds + 1

    threads = [
        threading.Thread(target=run_until_empty, args=(make_worker(worker_id=f"w{i}"),))
        for i in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    with session_factory() as session:
        completed = session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.status == DocumentStatus.COMPLETED)
        )
        processing_starts = session.scalar(
            select(func.count())
            .select_from(DocumentEvent)
            .where(DocumentEvent.status == DocumentStatus.PROCESSING)
        )
    assert completed == document_count
    assert processing_starts == document_count  # no document was claimed twice


def test_worker_loop_writes_heartbeat(session_factory, settings, clock, tmp_path):
    heartbeat = tmp_path / "heartbeat"
    settings = settings.model_copy(
        update={"worker_heartbeat_file": str(heartbeat), "worker_poll_interval_seconds": 0.01}
    )
    worker = Worker(session_factory, settings, worker_id="w1", clock=clock)

    stop = threading.Event()
    thread = threading.Thread(target=worker.run_forever, args=(stop,))
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(timeout=5)

    assert is_alive(heartbeat, max_age_seconds=60)


def test_healthcheck_detects_stale_or_missing_heartbeat(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    assert not is_alive(heartbeat, max_age_seconds=60)  # worker never started

    heartbeat.touch()
    beat = heartbeat.stat().st_mtime
    assert is_alive(heartbeat, max_age_seconds=60, now=beat + 59)
    assert not is_alive(heartbeat, max_age_seconds=60, now=beat + 61)  # loop is stuck


def test_worker_loop_survives_database_errors(session_factory, settings, clock, monkeypatch):
    settings = settings.model_copy(update={"worker_poll_interval_seconds": 0.01})
    worker = Worker(session_factory, settings, worker_id="w1", clock=clock)
    stop = threading.Event()
    calls = []

    def flaky_run_once():
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("SELECT 1", {}, Exception("database unavailable"))
        stop.set()  # second iteration reached: the loop survived the error
        return False

    monkeypatch.setattr(worker, "run_once", flaky_run_once)
    worker.run_forever(stop)

    assert len(calls) == 2


def test_simulated_failures_raise_transient_errors(monkeypatch):
    monkeypatch.setattr("app.worker.random.random", lambda: 0.0)
    flaky = with_simulated_failures(lambda _content: {"ok": True}, failure_rate=0.5)
    with pytest.raises(TransientProcessingError):
        flaky(b"<order/>")

    unchanged = with_simulated_failures(transform_order, failure_rate=0)
    assert unchanged is transform_order
