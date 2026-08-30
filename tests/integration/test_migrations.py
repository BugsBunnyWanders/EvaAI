import pytest
from sqlalchemy import inspect, text

from eva_ai.config import Settings
from eva_ai.db import Database


@pytest.mark.integration
async def test_event_backbone_tables_exist(database: Database) -> None:
    async with database.engine.connect() as connection:
        tables = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_table_names()
        )
    assert {
        "users",
        "workspaces",
        "events",
        "event_processing",
        "outbox_messages",
    } <= set(tables)


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
