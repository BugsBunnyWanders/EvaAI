from dataclasses import dataclass
from uuid import UUID, uuid7

from eva_ai.db.models import User, Workspace
from eva_ai.db.session import Database


@dataclass(frozen=True, slots=True)
class Scope:
    user_id: UUID
    workspace_id: UUID


async def create_scope(database: Database) -> Scope:
    scope = Scope(user_id=uuid7(), workspace_id=uuid7())
    async with database.session() as session:
        async with session.begin():
            session.add(User(id=scope.user_id, display_name=f"User {scope.user_id}"))
            session.add(
                Workspace(
                    id=scope.workspace_id,
                    user_id=scope.user_id,
                    name=f"Workspace {scope.workspace_id}",
                )
            )
    return scope
