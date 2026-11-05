"""The Alembic migrations must produce exactly the schema the models describe.

The other tests build the schema straight from the models (faster). This test makes sure
that forgetting a migration (or getting one wrong) is caught in CI.
"""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import text

from app.db import create_db_engine
from app.models import Base


@pytest.fixture
def alembic_config(settings) -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url"] = settings.database_url
    config.attributes["skip_logging_setup"] = True
    return config


@pytest.fixture
def empty_database(settings):
    engine = create_db_engine(settings.database_url)
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    yield engine
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    engine.dispose()


def test_migrations_match_models(alembic_config, empty_database):
    command.upgrade(alembic_config, "head")

    with empty_database.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert differences == []


def test_migrations_can_be_rolled_back(alembic_config, empty_database):
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
