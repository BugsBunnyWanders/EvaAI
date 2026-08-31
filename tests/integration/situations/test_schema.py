from uuid import uuid7

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from eva_ai.db import Database
from eva_ai.db.models import Situation, User, Workspace
from eva_ai.situations import AttentionLevel, SituationLifecycle, SituationType


async def schema_details(database: Database, table_name: str) -> tuple[set[str], set[str]]:
    async with database.engine.connect() as connection:
        return await connection.run_sync(
            lambda sync_connection: (
                {
                    item["name"]
                    for item in inspect(sync_connection).get_check_constraints(table_name)
                    if item["name"] is not None
                },
                {
                    item["name"]
                    for item in inspect(sync_connection).get_foreign_keys(table_name)
                    if item["name"] is not None
                },
            )
        )


@pytest.mark.integration
async def test_situation_schema_has_domain_and_scope_constraints(database: Database) -> None:
    checks, foreign_keys = await schema_details(database, "situations")

    assert checks >= {
        "ck_situations_type",
        "ck_situations_lifecycle",
        "ck_situations_attention",
        "ck_situations_version",
    }
    assert foreign_keys >= {"fk_situations_workspace_user"}


@pytest.mark.integration
@pytest.mark.parametrize(
    ("table_name", "expected_checks", "expected_foreign_keys"),
    [
        (
            "situation_events",
            {"ck_situation_events_method"},
            {"fk_situation_events_situation_scope", "fk_situation_events_event_scope"},
        ),
        (
            "situation_goals",
            {"ck_situation_goals_relevance", "ck_situation_goals_contribution"},
            {"fk_situation_goals_situation_scope", "fk_situation_goals_goal_scope"},
        ),
        (
            "situation_correlation_keys",
            {"ck_situation_correlation_keys_kind"},
            {
                "fk_situation_correlation_keys_situation_scope",
                "fk_situation_correlation_keys_workspace_user",
            },
        ),
    ],
)
async def test_relationship_schema_enforces_domain_and_scope(
    database: Database,
    table_name: str,
    expected_checks: set[str],
    expected_foreign_keys: set[str],
) -> None:
    checks, foreign_keys = await schema_details(database, table_name)

    assert checks >= expected_checks
    assert foreign_keys >= expected_foreign_keys


@pytest.mark.integration
async def test_situation_event_relationship_remains_many_to_many(database: Database) -> None:
    async with database.engine.connect() as connection:
        unique_constraints = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_unique_constraints(
                "situation_events"
            )
        )

    assert unique_constraints == []


@pytest.mark.integration
async def test_situation_database_rejects_another_users_workspace(database: Database) -> None:
    first_user_id, second_user_id, workspace_id = uuid7(), uuid7(), uuid7()
    async with database.session() as session:
        async with session.begin():
            session.add_all(
                [
                    User(id=first_user_id, display_name="First"),
                    User(id=second_user_id, display_name="Second"),
                    Workspace(id=workspace_id, user_id=first_user_id, name="Personal"),
                ]
            )

    with pytest.raises(IntegrityError):
        async with database.session() as session:
            async with session.begin():
                session.add(
                    Situation(
                        id=uuid7(),
                        user_id=second_user_id,
                        workspace_id=workspace_id,
                        type=SituationType.EMAIL_THREAD,
                        title="Thread",
                        lifecycle=SituationLifecycle.OPEN,
                        attention=AttentionLevel.NORMAL,
                        summary="",
                        current_state="NEW",
                    )
                )
