import pytest
from sqlalchemy import text

from eva_ai.config import Settings
from eva_ai.db import Database


@pytest.mark.integration
async def test_initial_migration_installs_pgvector() -> None:
    settings = Settings(_env_file=None)
    database = Database(settings.database_url.get_secret_value())

    try:
        async with database.session() as session:
            extension = await session.scalar(
                text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            )
    finally:
        await database.close()

    assert extension == "vector"
