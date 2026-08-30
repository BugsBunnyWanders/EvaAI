from datetime import UTC, datetime
from uuid import uuid7

import pytest
from sqlalchemy.exc import IntegrityError

from eva_ai.db import Database
from eva_ai.db.base import Base
from eva_ai.db.models import Event, EventProcessing, OutboxMessage, User, Workspace
from eva_ai.events.types import PrincipalType


@pytest.mark.parametrize(
    ("model", "column_name", "expected"),
    [
        (User, "created_at", "now()"),
        (User, "updated_at", "now()"),
        (Workspace, "created_at", "now()"),
        (Workspace, "updated_at", "now()"),
        (Event, "created_at", "now()"),
        (Event, "updated_at", "now()"),
        (EventProcessing, "created_at", "now()"),
        (EventProcessing, "updated_at", "now()"),
        (EventProcessing, "stage", "RECEIVED"),
        (EventProcessing, "attempt_count", "0"),
        (OutboxMessage, "created_at", "now()"),
        (OutboxMessage, "updated_at", "now()"),
        (OutboxMessage, "state", "PENDING"),
        (OutboxMessage, "attempt_count", "0"),
    ],
)
def test_orm_metadata_matches_required_database_defaults(
    model: type[Base], column_name: str, expected: str
) -> None:
    server_default = model.__table__.c[column_name].server_default

    assert server_default is not None
    assert str(server_default.arg) == expected


@pytest.mark.integration
async def test_event_cannot_claim_another_users_workspace(database: Database) -> None:
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
                    Event(
                        id=uuid7(),
                        user_id=second_user_id,
                        workspace_id=workspace_id,
                        source="test",
                        event_type="test.created",
                        idempotency_key=f"test:{uuid7()}",
                        occurred_at=datetime.now(UTC),
                        received_at=datetime.now(UTC),
                        principal_type=PrincipalType.USER,
                        payload={},
                        event_metadata={},
                        correlation_keys=[],
                        schema_version=1,
                    )
                )
