"""Asynchronous worker: picks documents from the database queue and processes them.

Start with:  python -m app.worker

Each iteration has three steps, each in its own short transaction (or none):
  1. claim    - atomically take one document and set a lease   (transaction 1)
  2. process  - run the transformation, outside any transaction  (no locks held)
  3. finish   - save the result or the error                     (transaction 2)

If the worker crashes between steps 1 and 3, the document stays PROCESSING until the
lease runs out. Then another worker (or this one after a restart) picks it up again.
That gives at-least-once processing. The transformation is idempotent, so processing
a document twice is harmless.

Several workers can run side by side (`docker compose up --scale worker=3`).
"""

import logging
import os
import random
import signal
import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from app import repository
from app.config import Settings, get_settings
from app.db import create_db_engine, create_session_factory
from app.logging_config import configure_logging
from app.models import DocumentStatus
from app.processing import PermanentProcessingError, transform_order
from app.repository import ClaimedDocument, utcnow

# Fixed name: under `python -m app.worker`, __name__ would be "__main__".
logger = logging.getLogger("app.worker")

# The processing step: raw XML in, JSON-ready result out. Injectable, so tests can pass
# a processor that fails on purpose.
Processor = Callable[[bytes], dict[str, Any]]


class TransientProcessingError(Exception):
    """Temporary error (for example, a downstream system is unreachable); a retry may succeed."""


def retry_delay(attempt: int, base_seconds: float, max_seconds: float) -> timedelta:
    """Exponential backoff: base, 2*base, 4*base, ... capped at max_seconds."""
    return timedelta(seconds=min(base_seconds * 2 ** (attempt - 1), max_seconds))


def with_simulated_failures(processor: Processor, failure_rate: float) -> Processor:
    """Demo only: make a share of processing attempts fail, to show the retry path."""
    if failure_rate <= 0:
        return processor

    def flaky(content: bytes) -> dict[str, Any]:
        if random.random() < failure_rate:
            raise TransientProcessingError("Simulated transient failure")
        return processor(content)

    return flaky


class Worker:
    """One worker loop. Run several processes side by side to scale out."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        processor: Processor = transform_order,
        worker_id: str | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.processor = processor
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        # An injectable clock lets tests move time forward (backoff, lease expiry).
        self.clock = clock

    def run_forever(self, stop: threading.Event) -> None:
        """Keep processing documents until `stop` is set (by SIGTERM or Ctrl+C)."""
        logger.info("Worker started", extra={"worker_id": self.worker_id})
        while not stop.is_set():
            self._heartbeat()
            try:
                did_work = self.run_once()
            except Exception:
                # For example, the database is temporarily unreachable. Log it and keep
                # running; the next iteration tries again.
                logger.exception("Worker iteration failed", extra={"worker_id": self.worker_id})
                did_work = False
            if not did_work:
                # Queue is empty (or there was an error): wait before polling again.
                stop.wait(self.settings.worker_poll_interval_seconds)
        logger.info("Worker stopped", extra={"worker_id": self.worker_id})

    def _heartbeat(self) -> None:
        """Show that the loop is alive (liveness, not readiness).

        The heartbeat continues while the database is down. Restarting the worker would
        not fix that, and the loop recovers on its own. What the healthcheck catches is a
        loop that is stuck or no longer running.
        """
        if self.settings.worker_heartbeat_file:
            Path(self.settings.worker_heartbeat_file).touch()

    def run_once(self) -> bool:
        """Process at most one document. Returns True if a document was picked up."""
        claimed = self._claim()
        if claimed is None:
            return False
        self._process(claimed)
        return True

    def _claim(self) -> ClaimedDocument | None:
        """Step 1: take the next document and set a lease, in a short transaction."""
        lease = timedelta(seconds=self.settings.worker_lease_seconds)
        try:
            with self.session_factory.begin() as session:
                return repository.claim_next(session, self.worker_id, self.clock(), lease)
        except StaleDataError:
            # Another worker claimed the same document at the same moment (only possible
            # on SQLite). Not an error: just skip it.
            return None

    def _process(self, claimed: ClaimedDocument) -> None:
        """Steps 2 and 3: process the document, then record the outcome.

        PermanentProcessingError goes straight to FAILED. Any other exception is treated
        as transient and retried with backoff until the attempts run out.
        """
        log_context: dict[str, Any] = {
            "document_id": str(claimed.id),
            "attempt": claimed.attempt,
            "worker_id": self.worker_id,
        }
        logger.info("Processing started", extra=log_context)
        started = time.perf_counter()

        try:
            result = self.processor(claimed.content)
        except PermanentProcessingError as exc:
            log_context["error_type"] = type(exc).__name__
            self._fail(claimed, str(exc), log_context)
        except Exception as exc:
            # The full message goes to the document (last_error, readable via the API) but
            # not to the log: it can quote values from the document, such as personal data.
            log_context["error_type"] = type(exc).__name__
            # Unknown errors count as transient: retry until the attempts run out.
            error = f"{type(exc).__name__}: {exc}"
            if claimed.attempt >= self.settings.worker_max_attempts:
                error = f"Giving up after {claimed.attempt} attempts. Last error: {error}"
                self._fail(claimed, error, log_context)
            else:
                self._retry(claimed, error, log_context)
        else:
            self._complete(claimed, result, log_context)

        log_context["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
        logger.info("Processing finished", extra=log_context)

    def _complete(
        self, claimed: ClaimedDocument, result: dict[str, Any], log_context: dict[str, Any]
    ) -> None:
        """Step 3 on success: store the result."""
        self._save(
            DocumentStatus.COMPLETED,
            log_context,
            lambda session, now: repository.mark_completed(session, claimed, result, now),
        )

    def _retry(self, claimed: ClaimedDocument, error: str, log_context: dict[str, Any]) -> None:
        """Step 3 on a transient error: schedule the next attempt after a backoff."""
        delay = retry_delay(
            claimed.attempt,
            self.settings.worker_backoff_base_seconds,
            self.settings.worker_backoff_max_seconds,
        )
        self._save(
            DocumentStatus.RETRYING,
            log_context,
            lambda session, now: repository.schedule_retry(
                session, claimed, error, now + delay, now
            ),
        )

    def _fail(self, claimed: ClaimedDocument, error: str, log_context: dict[str, Any]) -> None:
        """Step 3 on a permanent error (or no attempts left): mark the document FAILED."""
        self._save(
            DocumentStatus.FAILED,
            log_context,
            lambda session, now: repository.mark_failed(session, claimed, error, now),
        )

    def _save(
        self,
        outcome: DocumentStatus,
        log_context: dict[str, Any],
        write: Callable[[Session, datetime], bool],
    ) -> None:
        """Write the outcome in its own transaction, but only if our claim is still valid.

        `write` returns False when the claim is no longer ours. A StaleDataError means the
        same thing, caught by the version check at the moment of writing.
        """
        try:
            with self.session_factory.begin() as session:
                saved = write(session, self.clock())
        except StaleDataError:
            saved = False

        log_context["outcome"] = outcome if saved else "LEASE_LOST"
        if not saved:
            # Our lease ran out and another worker took over. Its outcome wins; we drop ours.
            logger.warning("Lease lost, outcome discarded", extra=log_context)


def main() -> None:
    """Entry point of the worker container."""
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(settings.database_url)

    worker = Worker(
        create_session_factory(engine),
        settings,
        processor=with_simulated_failures(transform_order, settings.simulated_failure_rate),
    )

    # Stop cleanly on SIGTERM (docker stop) or Ctrl+C: finish the current document first.
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())

    worker.run_forever(stop)
    engine.dispose()


if __name__ == "__main__":
    main()
