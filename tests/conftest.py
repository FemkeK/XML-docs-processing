"""Shared test fixtures.

By default each test uses its own temporary SQLite database. Set TEST_DATABASE_URL to
run the same tests against PostgreSQL (CI does this).
"""

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import create_db_engine, create_session_factory
from app.main import create_app
from app.models import Base
from app.processing import transform_order
from app.worker import Processor, Worker

SAMPLES_DIR = Path(__file__).parent.parent / "samples"
API_KEY = "test-key"


def load_sample(name: str) -> bytes:
    return (SAMPLES_DIR / name).read_bytes()


class FakeClock:
    """Real time plus an adjustable offset, so tests can "jump ahead" (backoff, lease)."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset

    def advance(self, **kwargs: float) -> None:
        self.offset += timedelta(**kwargs)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}",
        api_keys=[API_KEY],
        worker_lease_seconds=30,
        worker_max_attempts=3,
        worker_backoff_base_seconds=10,
        worker_backoff_max_seconds=60,
    )


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    engine = create_db_engine(settings.database_url)
    # Start from an empty schema (matters for a shared PostgreSQL database). The schema is
    # built straight from the models for speed; test_migrations.py checks that the
    # Alembic migrations produce exactly the same schema.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_worker(
    session_factory: sessionmaker[Session], settings: Settings, clock: FakeClock
) -> Callable[..., Worker]:
    def factory(processor: Processor = transform_order, worker_id: str = "worker-1") -> Worker:
        return Worker(session_factory, settings, processor, worker_id=worker_id, clock=clock)

    return factory


@pytest.fixture
def client(settings: Settings, engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        test_client.headers["X-API-Key"] = API_KEY
        yield test_client
