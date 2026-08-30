from dataclasses import dataclass
from uuid import UUID, uuid7

from sqlalchemy import select

from eva_ai.db.models import User, Workspace
from eva_ai.db.session import Database


@dataclass(frozen=True, slots=True)
class LocalScope:
    user_id: UUID
    workspace_id: UUID


async def create_local_scope(
    database: Database,
    *,
    display_name: str,
    workspace_name: str,
) -> LocalScope:
    if not display_name.strip() or not workspace_name.strip():
        raise ValueError("scope names must not be blank")
    scope = LocalScope(user_id=uuid7(), workspace_id=uuid7())
    async with database.session() as session:
        async with session.begin():
            session.add_all(
                [
                    User(id=scope.user_id, display_name=display_name),
                    Workspace(
                        id=scope.workspace_id,
                        user_id=scope.user_id,
                        name=workspace_name,
                    ),
                ]
            )
    return scope


async def local_scope_exists(database: Database, user_id: UUID, workspace_id: UUID) -> bool:
    statement = (
        select(Workspace.id)
        .join(User, User.id == Workspace.user_id)
        .where(
            User.id == user_id,
            Workspace.id == workspace_id,
            Workspace.user_id == user_id,
        )
        .limit(1)
    )
    async with database.session() as session:
        return await session.scalar(statement) is not None
