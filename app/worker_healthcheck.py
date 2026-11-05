"""Healthcheck for the worker container: exit code 0 if the worker loop is alive.

    python -m app.worker_healthcheck

The worker touches WORKER_HEARTBEAT_FILE on every loop iteration. The check fails if
the file is missing or older than one lease period. A loop that has not come back
within a lease is stuck: its document is already being handed to another worker.
"""

import sys
import time
from pathlib import Path

from app.config import get_settings


def is_alive(heartbeat_file: Path, max_age_seconds: float, now: float | None = None) -> bool:
    """True if the heartbeat file exists and was touched less than `max_age_seconds` ago."""
    try:
        last_beat = heartbeat_file.stat().st_mtime
    except FileNotFoundError:
        return False
    return ((now or time.time()) - last_beat) < max_age_seconds


def main() -> int:
    """Return the exit code for Docker: 0 is healthy, 1 is unhealthy."""
    settings = get_settings()
    if not settings.worker_heartbeat_file:
        print("WORKER_HEARTBEAT_FILE is not set", file=sys.stderr)
        return 1
    alive = is_alive(Path(settings.worker_heartbeat_file), settings.worker_lease_seconds)
    return 0 if alive else 1


if __name__ == "__main__":
    sys.exit(main())
