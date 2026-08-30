import pytest
from sqlalchemy import inspect, text

from eva_ai.config import Settings
from eva_ai.db import Database


async def constraint_names(database: Database, table_name: str) -> set[str]:
    async with database.engine.connect() as connection:
        return await connection.run_sync(
            lambda sync_connection: (
                {
                    constraint["name"]
                    for constraints in (
                        inspect(sync_connection).get_check_constraints(table_name),
                        inspect(sync_connection).get_foreign_keys(table_name),
                        inspect(sync_connection).get_unique_constraints(table_name),
                    )
                    for constraint in constraints
                    if constraint["name"] is not None
                }
                if inspect(sync_connection).has_table(table_name)
                else set()
            )
        )


async def primary_key_columns(database: Database, table_name: str) -> list[str]:
    async with database.engine.connect() as connection:
        return await connection.run_sync(
            lambda sync_connection: (
                inspect(sync_connection).get_pk_constraint(table_name)["constrained_columns"]
                if inspect(sync_connection).has_table(table_name)
                else []
            )
        )


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
        "connector_accounts",
        "gmail_sync_states",
    } <= set(tables)


@pytest.mark.integration
async def test_gmail_connector_constraints(database: Database) -> None:
    assert await constraint_names(database, "connector_accounts") >= {
        "fk_connector_accounts_workspace_user",
        "uq_connector_accounts_workspace_provider_identity",
        "ck_connector_accounts_status",
        "ck_connector_accounts_active_secret",
    }
    assert await primary_key_columns(database, "gmail_sync_states") == ["connector_account_id"]


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
