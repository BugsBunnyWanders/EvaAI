from collections.abc import AsyncIterator

import pytest
from alembic import command
from alembic.config import Config

from eva_ai.config import get_settings
from eva_ai.db import Database


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    database = Database(get_settings().database_url.get_secret_value())
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture(scope="session", autouse=True)
def apply_migrations() -> None:
    command.upgrade(Config("alembic.ini"), "head")
