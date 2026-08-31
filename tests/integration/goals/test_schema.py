from datetime import UTC, datetime
from uuid import uuid7

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from eva_ai.db import Database
from eva_ai.db.models import Goal, User, Workspace
from eva_ai.goals import GoalMode, GoalSource, GoalStatus


@pytest.mark.integration
async def test_goal_schema_has_scope_parent_and_domain_constraints(database: Database) -> None:
    async with database.engine.connect() as connection:
        checks, foreign_keys, unique_constraints, indexes = await connection.run_sync(
            lambda sync_connection: (
                inspect(sync_connection).get_check_constraints("goals"),
                inspect(sync_connection).get_foreign_keys("goals"),
                inspect(sync_connection).get_unique_constraints("goals"),
                inspect(sync_connection).get_indexes("goals"),
            )
        )

    assert {item["name"] for item in checks} >= {
        "ck_goals_mode",
        "ck_goals_priority",
        "ck_goals_status",
        "ck_goals_success_criteria",
        "ck_goals_constraints",
        "ck_goals_autonomy_policy",
        "ck_goals_source",
        "ck_goals_confidence",
    }
    assert {item["name"] for item in foreign_keys} >= {
        "fk_goals_workspace_user",
        "fk_goals_parent_scope",
    }
    assert {item["name"] for item in unique_constraints} >= {"uq_goals_id_workspace_user"}
    assert {item["name"] for item in indexes} >= {
        "ix_goals_scope_status",
        "ix_goals_scope_priority_created",
    }


@pytest.mark.integration
async def test_goal_database_rejects_cross_scope_parent(database: Database) -> None:
    first_user_id, second_user_id = uuid7(), uuid7()
    first_workspace_id, second_workspace_id = uuid7(), uuid7()
    parent_id = uuid7()
    now = datetime.now(UTC)
    async with database.session() as session:
        async with session.begin():
            session.add_all(
                [
                    User(id=first_user_id, display_name="First"),
                    User(id=second_user_id, display_name="Second"),
                    Workspace(id=first_workspace_id, user_id=first_user_id, name="Personal"),
                    Workspace(id=second_workspace_id, user_id=second_user_id, name="Personal"),
                ]
            )
    async with database.session() as session:
        async with session.begin():
            session.add(
                Goal(
                    id=parent_id,
                    user_id=first_user_id,
                    workspace_id=first_workspace_id,
                    title="Parent",
                    objective="Parent objective",
                    domain="personal",
                    mode=GoalMode.ACHIEVE,
                    priority=50,
                    status=GoalStatus.ACTIVE,
                    success_criteria=[],
                    constraints={},
                    autonomy_policy={"mode": "REQUIRE_APPROVAL"},
                    source=GoalSource.USER_EXPLICIT,
                    confidence=1,
                    created_at=now,
                    updated_at=now,
                )
            )

    with pytest.raises(IntegrityError):
        async with database.session() as session:
            async with session.begin():
                session.add(
                    Goal(
                        id=uuid7(),
                        user_id=second_user_id,
                        workspace_id=second_workspace_id,
                        title="Child",
                        objective="Child objective",
                        domain="personal",
                        mode=GoalMode.ACHIEVE,
                        priority=50,
                        status=GoalStatus.ACTIVE,
                        success_criteria=[],
                        constraints={},
                        autonomy_policy={"mode": "REQUIRE_APPROVAL"},
                        source=GoalSource.USER_EXPLICIT,
                        confidence=1,
                        parent_goal_id=parent_id,
                        created_at=now,
                        updated_at=now,
                    )
                )
