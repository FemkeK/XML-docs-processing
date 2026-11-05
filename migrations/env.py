"""Alembic environment: connects with the app's DATABASE_URL and compares against its models."""

from typing import Any

from alembic import context

from app.config import get_settings
from app.db import create_db_engine
from app.logging_config import configure_logging
from app.models import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    # Tests pass a URL through config.attributes; otherwise use the app settings.
    return config.attributes.get("database_url") or get_settings().database_url


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        # SQLite cannot ALTER most things in place. Batch mode recreates the table
        # instead, so the same migrations work on SQLite and PostgreSQL.
        render_as_batch=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`: print the SQL instead of running it."""
    _configure(url=_database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(_database_url())
    with engine.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if not config.attributes.get("skip_logging_setup"):
    configure_logging(get_settings().log_level)

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
