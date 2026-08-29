import pytest
from sqlalchemy import text

from eva_ai.config import Settings
from eva_ai.db.session import Database


@pytest.mark.integration
async def test_database_can_ping_and_open_session() -> None:
    settings = Settings(_env_file=None)
    database = Database(settings.database_url.get_secret_value())

    try:
        await database.ping()
        async with database.session() as session:
            assert await session.scalar(text("SELECT 1")) == 1
    finally:
        await database.close()
