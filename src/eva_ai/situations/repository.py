from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import case, desc, select, update
from sqlalchemy.dialects.postgresql import insert

from eva_ai.db.models import Event, Goal, Situation, SituationEvent, SituationGoal
from eva_ai.db.session import Database
from eva_ai.situations.errors import (
    SituationNotFoundError,
    SituationScopeError,
    SituationVersionConflictError,
)
from eva_ai.situations.transitions import validate_situation_transition
from eva_ai.situations.types import (
    AttentionLevel,
    CorrelationMethod,
    GoalContribution,
    LinkSituationGoal,
    SituationEventRecord,
    SituationGoalRecord,
    SituationLifecycle,
    SituationRecord,
    SituationSnapshotUpdate,
    SituationType,
)


class SituationRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        situation_id: UUID,
    ) -> SituationRecord | None:
        statement = select(Situation).where(
            Situation.id == situation_id,
            Situation.user_id == user_id,
            Situation.workspace_id == workspace_id,
        )
        async with self._database.session() as session:
            situation = await session.scalar(statement)
        return _situation_record(situation) if situation is not None else None

    async def list(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        lifecycles: tuple[SituationLifecycle, ...] = (),
        limit: int = 50,
    ) -> tuple[SituationRecord, ...]:
        terminal_order = case(
            (
                Situation.lifecycle.in_(
                    (SituationLifecycle.RESOLVED, SituationLifecycle.ABANDONED)
                ),
                1,
            ),
            else_=0,
        )
        attention_order = case(
            (Situation.attention == AttentionLevel.URGENT, 4),
            (Situation.attention == AttentionLevel.HIGH, 3),
            (Situation.attention == AttentionLevel.NORMAL, 2),
            else_=1,
        )
        statement = (
            select(Situation)
            .where(Situation.user_id == user_id, Situation.workspace_id == workspace_id)
            .order_by(
                terminal_order,
                desc(attention_order),
                desc(Situation.last_activity_at),
                Situation.id,
            )
            .limit(limit)
        )
        if lifecycles:
            statement = statement.where(Situation.lifecycle.in_(lifecycles))
        async with self._database.session() as session:
            situations = (await session.scalars(statement)).all()
        return tuple(_situation_record(situation) for situation in situations)

    async def update_snapshot(self, command: SituationSnapshotUpdate) -> SituationRecord:
        # Compare-and-swap keeps a stale reader from overwriting a newer curated snapshot.
        statement = (
            update(Situation)
            .where(
                Situation.id == command.situation_id,
                Situation.user_id == command.user_id,
                Situation.workspace_id == command.workspace_id,
                Situation.version == command.expected_version,
            )
            .values(
                title=command.title,
                summary=command.summary,
                current_state=command.current_state,
                next_action=command.next_action,
                next_expected=command.next_expected,
                version=Situation.version + 1,
                updated_at=command.updated_at,
            )
            .returning(Situation)
        )
        async with self._database.session() as session:
            async with session.begin():
                situation = (await session.scalars(statement)).one_or_none()
                if situation is not None:
                    return _situation_record(situation)
                existing = await session.scalar(
                    select(Situation.id).where(
                        Situation.id == command.situation_id,
                        Situation.user_id == command.user_id,
                        Situation.workspace_id == command.workspace_id,
                    )
                )
                if existing is None:
                    raise SituationNotFoundError
                raise SituationVersionConflictError

    async def update_lifecycle(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        situation_id: UUID,
        lifecycle: SituationLifecycle,
    ) -> SituationRecord:
        statement = (
            select(Situation)
            .where(
                Situation.id == situation_id,
                Situation.user_id == user_id,
                Situation.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        async with self._database.session() as session:
            async with session.begin():
                situation = await session.scalar(statement)
                if situation is None:
                    raise SituationNotFoundError
                validate_situation_transition(
                    SituationLifecycle(situation.lifecycle),
                    lifecycle,
                )
                situation.lifecycle = lifecycle
                await session.flush()
                return _situation_record(situation)

    async def update_attention(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        situation_id: UUID,
        attention: AttentionLevel,
    ) -> SituationRecord:
        statement = (
            update(Situation)
            .where(
                Situation.id == situation_id,
                Situation.user_id == user_id,
                Situation.workspace_id == workspace_id,
            )
            .values(attention=attention)
            .returning(Situation)
        )
        async with self._database.session() as session:
            async with session.begin():
                situation = (await session.scalars(statement)).one_or_none()
                if situation is None:
                    raise SituationNotFoundError
                return _situation_record(situation)

    async def link_goal(self, command: LinkSituationGoal) -> SituationGoalRecord:
        # This explicit mutation path may replace relationship metadata. Resolver linking uses
        # insert-only conflict handling so automated correlation cannot overwrite curation.
        relationship_insert = (
            insert(SituationGoal)
            .values(
                situation_id=command.situation_id,
                goal_id=command.goal_id,
                workspace_id=command.workspace_id,
                user_id=command.user_id,
                relevance=command.relevance,
                contribution=command.contribution,
                reasoning=command.reasoning,
                linked_at=command.linked_at,
            )
            .on_conflict_do_update(
                index_elements=[SituationGoal.situation_id, SituationGoal.goal_id],
                set_={
                    "relevance": command.relevance,
                    "contribution": command.contribution,
                    "reasoning": command.reasoning,
                    "linked_at": command.linked_at,
                },
            )
            .returning(SituationGoal)
        )
        async with self._database.session() as session:
            async with session.begin():
                situation_exists = await session.scalar(
                    select(Situation.id).where(
                        Situation.id == command.situation_id,
                        Situation.user_id == command.user_id,
                        Situation.workspace_id == command.workspace_id,
                    )
                )
                goal_exists = await session.scalar(
                    select(Goal.id).where(
                        Goal.id == command.goal_id,
                        Goal.user_id == command.user_id,
                        Goal.workspace_id == command.workspace_id,
                    )
                )
                if situation_exists is None or goal_exists is None:
                    raise SituationScopeError
                relationship = (await session.scalars(relationship_insert)).one()
                return _situation_goal_record(relationship)

    async def list_events(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        situation_id: UUID,
    ) -> tuple[SituationEventRecord, ...]:
        statement = (
            select(SituationEvent, Event.occurred_at)
            .join(
                Event,
                (Event.id == SituationEvent.event_id)
                & (Event.workspace_id == SituationEvent.workspace_id)
                & (Event.user_id == SituationEvent.user_id),
            )
            .where(
                SituationEvent.situation_id == situation_id,
                SituationEvent.user_id == user_id,
                SituationEvent.workspace_id == workspace_id,
            )
            .order_by(Event.occurred_at, SituationEvent.event_id)
        )
        async with self._database.session() as session:
            rows = (await session.execute(statement)).all()
        return tuple(
            _situation_event_record(relationship, occurred_at) for relationship, occurred_at in rows
        )

    async def list_goals(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        situation_id: UUID,
    ) -> tuple[SituationGoalRecord, ...]:
        statement = (
            select(SituationGoal)
            .where(
                SituationGoal.situation_id == situation_id,
                SituationGoal.user_id == user_id,
                SituationGoal.workspace_id == workspace_id,
            )
            .order_by(SituationGoal.linked_at, SituationGoal.goal_id)
        )
        async with self._database.session() as session:
            relationships = (await session.scalars(statement)).all()
        return tuple(_situation_goal_record(relationship) for relationship in relationships)


def _situation_record(situation: Situation) -> SituationRecord:
    return SituationRecord(
        id=situation.id,
        user_id=situation.user_id,
        workspace_id=situation.workspace_id,
        type=SituationType(situation.type),
        title=situation.title,
        lifecycle=SituationLifecycle(situation.lifecycle),
        attention=AttentionLevel(situation.attention),
        summary=situation.summary,
        current_state=situation.current_state,
        next_action=situation.next_action,
        next_expected=situation.next_expected,
        version=situation.version,
        last_activity_at=situation.last_activity_at,
        created_at=situation.created_at,
        updated_at=situation.updated_at,
    )


def _situation_event_record(
    relationship: SituationEvent,
    occurred_at: object,
) -> SituationEventRecord:
    return SituationEventRecord(
        situation_id=relationship.situation_id,
        event_id=relationship.event_id,
        user_id=relationship.user_id,
        workspace_id=relationship.workspace_id,
        correlation_method=CorrelationMethod(relationship.correlation_method),
        correlation_key=relationship.correlation_key,
        event_occurred_at=cast(datetime, occurred_at),
        linked_at=relationship.linked_at,
    )


def _situation_goal_record(relationship: SituationGoal) -> SituationGoalRecord:
    return SituationGoalRecord(
        situation_id=relationship.situation_id,
        goal_id=relationship.goal_id,
        user_id=relationship.user_id,
        workspace_id=relationship.workspace_id,
        relevance=relationship.relevance,
        contribution=GoalContribution(relationship.contribution),
        reasoning=relationship.reasoning,
        linked_at=relationship.linked_at,
    )
