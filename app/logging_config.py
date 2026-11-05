"""Structured (JSON) logging with correlation fields.

Each log line is one JSON object, so a log platform (Loki, ELK, CloudWatch) can filter
on fields such as `document_id` or `request_id`. Add context through `extra=`:

    logger.info("Document received", extra={"document_id": str(doc.id)})
"""

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime

# Set by the API's request middleware; included in every log line of that request.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes every LogRecord has. Anything else came in through `extra=`.
_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Write each record as one JSON line, including the request ID and `extra=` fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if request_id := request_id_var.get():
            entry["request_id"] = request_id
        entry.update(
            {key: value for key, value in record.__dict__.items() if key not in _STANDARD_ATTRS}
        )
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Send all logging to stderr as JSON. Called once at startup by the API and the worker."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Send uvicorn's own logs through the same JSON handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
