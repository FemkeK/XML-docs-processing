"""Database engine and session setup."""

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry


def create_db_engine(database_url: str) -> Engine:
    # hide_parameters: SQLAlchemy error messages leave out the query parameters. Those
    # include the raw XML, and error messages end up in logs (tracebacks).
    if database_url.startswith("sqlite"):
        engine = create_engine(
            database_url, connect_args={"check_same_thread": False}, hide_parameters=True
        )
        _configure_sqlite(engine)
        return engine
    # pool_pre_ping detects dropped connections, for example after a database restart.
    return create_engine(database_url, pool_pre_ping=True, hide_parameters=True)


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
        cursor = dbapi_connection.cursor()
        # WAL lets the API and a worker read and write at the same time. busy_timeout
        # makes a connection wait briefly for a lock instead of failing at once.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: objects stay readable after commit, for example to build
    # the API response.
    return sessionmaker(bind=engine, expire_on_commit=False)
