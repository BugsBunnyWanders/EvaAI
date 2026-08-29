import pytest
from alembic import command
from alembic.config import Config


@pytest.fixture(scope="session", autouse=True)
def apply_migrations() -> None:
    command.upgrade(Config("alembic.ini"), "head")
