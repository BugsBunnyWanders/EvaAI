import asyncio
import json
from uuid import uuid7

import pytest
from alembic import command
from alembic.config import Config
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
        "goals",
        "situations",
        "situation_events",
        "situation_goals",
        "situation_correlation_keys",
    } <= set(tables)


@pytest.mark.integration
async def test_goal_situation_primary_keys_and_event_scope_constraint(database: Database) -> None:
    assert await primary_key_columns(database, "situation_events") == [
        "situation_id",
        "event_id",
    ]
    assert await primary_key_columns(database, "situation_goals") == [
        "situation_id",
        "goal_id",
    ]
    assert await primary_key_columns(database, "situation_correlation_keys") == [
        "workspace_id",
        "correlation_key",
    ]
    assert await constraint_names(database, "events") >= {"uq_events_id_workspace_user"}


@pytest.mark.integration
async def test_gmail_connector_constraints(database: Database) -> None:
    assert await constraint_names(database, "connector_accounts") >= {
        "fk_connector_accounts_workspace_user",
        "uq_connector_accounts_workspace_provider_identity",
        "ck_connector_accounts_status",
        "ck_connector_accounts_active_secret",
    }
    assert await constraint_names(database, "gmail_sync_states") >= {
        "fk_gmail_sync_states_connector_account"
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


def test_goal_situation_migration_round_trip_preserves_existing_event() -> None:
    settings = Settings(_env_file=None)
    database_url = settings.database_url.get_secret_value()
    user_id, workspace_id, event_id = uuid7(), uuid7(), uuid7()
    expected_payload = {"kind": "pre-milestone-3", "sequence": 7}

    async def insert_existing_event() -> None:
        database = Database(database_url)
        try:
            async with database.session() as session:
                async with session.begin():
                    await session.execute(
                        text("INSERT INTO users (id, display_name) VALUES (:user_id, 'Migration')"),
                        {"user_id": user_id},
                    )
                    await session.execute(
                        text(
                            "INSERT INTO workspaces (id, user_id, name) "
                            "VALUES (:workspace_id, :user_id, 'Migration')"
                        ),
                        {"workspace_id": workspace_id, "user_id": user_id},
                    )
                    await session.execute(
                        text(
                            "INSERT INTO events ("
                            "id, user_id, workspace_id, source, event_type, idempotency_key, "
                            "occurred_at, received_at, principal_type, payload, metadata, "
                            "correlation_keys, schema_version"
                            ") VALUES ("
                            ":event_id, :user_id, :workspace_id, 'test', 'test.existing', "
                            ":idempotency_key, now(), now(), 'SYSTEM', CAST(:payload AS jsonb), "
                            "'{}'::jsonb, ARRAY['existing-key'], 1"
                            ")"
                        ),
                        {
                            "event_id": event_id,
                            "user_id": user_id,
                            "workspace_id": workspace_id,
                            "idempotency_key": f"migration:{event_id}",
                            "payload": json.dumps(expected_payload),
                        },
                    )
        finally:
            await database.close()

    async def read_event_and_tables() -> tuple[dict[str, object], list[str]]:
        database = Database(database_url)
        try:
            async with database.session() as session:
                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT source, event_type, payload, correlation_keys "
                                "FROM events WHERE id = :event_id"
                            ),
                            {"event_id": event_id},
                        )
                    )
                    .mappings()
                    .one()
                )
            async with database.engine.connect() as connection:
                tables = await connection.run_sync(
                    lambda sync_connection: inspect(sync_connection).get_table_names()
                )
            return dict(row), tables
        finally:
            await database.close()

    asyncio.run(insert_existing_event())
    configuration = Config("alembic.ini")
    command.downgrade(configuration, "20260830_0003")
    downgraded_event, downgraded_tables = asyncio.run(read_event_and_tables())

    assert downgraded_event == {
        "source": "test",
        "event_type": "test.existing",
        "payload": expected_payload,
        "correlation_keys": ["existing-key"],
    }
    assert "goals" not in downgraded_tables
    assert "situations" not in downgraded_tables

    command.upgrade(configuration, "head")
    upgraded_event, upgraded_tables = asyncio.run(read_event_and_tables())

    assert upgraded_event == downgraded_event
    assert {"goals", "situations", "situation_events", "situation_goals"} <= set(upgraded_tables)
