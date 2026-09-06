from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from pydantic import JsonValue, ValidationError
from sqlalchemy import and_, case, exists, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from eva_ai.db.models import (
    Event,
    Goal,
    RelevanceEvaluationAttempt,
    Signal,
    SignalGoal,
    Situation,
    SituationCorrelationKey,
    SituationGoal,
)
from eva_ai.db.session import Database
from eva_ai.events.processor import StoredEvent
from eva_ai.goals.types import GoalStatus
from eva_ai.relevance.errors import (
    RelevanceError,
    RelevanceNotFoundError,
    RelevanceScopeError,
)
from eva_ai.relevance.types import (
    EvaluationAttemptRecord,
    EvaluationAttemptStatus,
    FinishEvaluationAttempt,
    GoalContext,
    GoalMatch,
    SignalDraft,
    SignalKind,
    SignalRecord,
    SituationContext,
    StartEvaluationAttempt,
)


class RelevanceRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def start_attempt(self, command: StartEvaluationAttempt) -> EvaluationAttemptRecord:
        row = RelevanceEvaluationAttempt(
            id=command.id,
            event_id=command.event_id,
            user_id=command.user_id,
            workspace_id=command.workspace_id,
            evaluation_key=command.evaluation_key,
            attempt_number=command.attempt_number,
            trigger=command.trigger,
            operator_reason=command.operator_reason,
            status=EvaluationAttemptStatus.STARTED,
            provider=command.provider,
            model=command.model,
            classifier_version=command.classifier_version,
            input_digest=command.input_digest,
            failure_code=None,
            started_at=command.started_at,
            completed_at=None,
        )
        async with self._database.session() as session:
            async with session.begin():
                session.add(row)
                await session.flush()
                return _attempt_record(row)

    async def finish_attempt(self, command: FinishEvaluationAttempt) -> EvaluationAttemptRecord:
        statement = (
            update(RelevanceEvaluationAttempt)
            .where(
                RelevanceEvaluationAttempt.id == command.attempt_id,
                RelevanceEvaluationAttempt.event_id == command.event_id,
                RelevanceEvaluationAttempt.user_id == command.user_id,
                RelevanceEvaluationAttempt.workspace_id == command.workspace_id,
                RelevanceEvaluationAttempt.evaluation_key == command.evaluation_key,
                RelevanceEvaluationAttempt.status == EvaluationAttemptStatus.STARTED,
            )
            .values(
                status=command.status,
                failure_code=command.failure_code,
                completed_at=command.completed_at,
            )
            .returning(RelevanceEvaluationAttempt)
        )
        async with self._database.session() as session:
            async with session.begin():
                row = (await session.scalars(statement)).one_or_none()
                if row is None:
                    raise RelevanceNotFoundError("evaluation attempt is not current")
                return _attempt_record(row)

    async def interrupt_started_attempts(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        event_id: UUID,
        evaluation_key: UUID,
        interrupted_at: datetime,
    ) -> int:
        if interrupted_at.utcoffset() is None:
            raise ValueError("interrupted_at must be timezone-aware")
        statement = (
            update(RelevanceEvaluationAttempt)
            .where(
                RelevanceEvaluationAttempt.event_id == event_id,
                RelevanceEvaluationAttempt.user_id == user_id,
                RelevanceEvaluationAttempt.workspace_id == workspace_id,
                RelevanceEvaluationAttempt.evaluation_key == evaluation_key,
                RelevanceEvaluationAttempt.status == EvaluationAttemptStatus.STARTED,
            )
            .values(
                status=EvaluationAttemptStatus.RETRYABLE_FAILURE,
                failure_code="INTERRUPTED",
                completed_at=interrupted_at,
            )
        )
        async with self._database.session() as session:
            async with session.begin():
                result = cast(CursorResult[Any], await session.execute(statement))
                return result.rowcount

    async def latest_attempt(
        self,
        *,
        event_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
        evaluation_key: UUID,
    ) -> EvaluationAttemptRecord | None:
        statement = (
            select(RelevanceEvaluationAttempt)
            .where(
                RelevanceEvaluationAttempt.event_id == event_id,
                RelevanceEvaluationAttempt.user_id == user_id,
                RelevanceEvaluationAttempt.workspace_id == workspace_id,
                RelevanceEvaluationAttempt.evaluation_key == evaluation_key,
            )
            .order_by(
                RelevanceEvaluationAttempt.attempt_number.desc(),
                RelevanceEvaluationAttempt.id.desc(),
            )
            .limit(1)
        )
        async with self._database.session() as session:
            row = await session.scalar(statement)
        return None if row is None else _attempt_record(row)

    async def list_attempts(
        self, *, event_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> tuple[EvaluationAttemptRecord, ...]:
        statement = (
            select(RelevanceEvaluationAttempt)
            .where(
                RelevanceEvaluationAttempt.event_id == event_id,
                RelevanceEvaluationAttempt.user_id == user_id,
                RelevanceEvaluationAttempt.workspace_id == workspace_id,
            )
            .order_by(
                RelevanceEvaluationAttempt.started_at,
                RelevanceEvaluationAttempt.attempt_number,
                RelevanceEvaluationAttempt.id,
            )
        )
        async with self._database.session() as session:
            rows = (await session.scalars(statement)).all()
        return tuple(_attempt_record(row) for row in rows)

    async def get_current(
        self, *, event_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> SignalRecord | None:
        statement = select(Signal).where(
            Signal.event_id == event_id,
            Signal.user_id == user_id,
            Signal.workspace_id == workspace_id,
            Signal.kind == SignalKind.RELEVANCE,
            Signal.is_current.is_(True),
        )
        async with self._database.session() as session:
            row = await session.scalar(statement)
            return None if row is None else await _signal_record(session, row)

    async def get_by_evaluation_key(
        self,
        *,
        event_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
        evaluation_key: UUID,
    ) -> SignalRecord | None:
        statement = select(Signal).where(
            Signal.event_id == event_id,
            Signal.user_id == user_id,
            Signal.workspace_id == workspace_id,
            Signal.evaluation_key == evaluation_key,
        )
        async with self._database.session() as session:
            row = await session.scalar(statement)
            return None if row is None else await _signal_record(session, row)

    async def get_signal(
        self, *, signal_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> SignalRecord:
        statement = select(Signal).where(
            Signal.id == signal_id,
            Signal.user_id == user_id,
            Signal.workspace_id == workspace_id,
        )
        async with self._database.session() as session:
            row = await session.scalar(statement)
            if row is None:
                raise RelevanceNotFoundError("Signal not found")
            return await _signal_record(session, row)

    async def history(
        self, *, event_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> tuple[SignalRecord, ...]:
        statement = (
            select(Signal)
            .where(
                Signal.event_id == event_id,
                Signal.user_id == user_id,
                Signal.workspace_id == workspace_id,
                Signal.kind == SignalKind.RELEVANCE,
            )
            .order_by(Signal.created_at, Signal.id)
        )
        async with self._database.session() as session:
            rows = (await session.scalars(statement)).all()
            records = []
            for row in rows:
                records.append(await _signal_record(session, row))
            return tuple(records)

    async def persist_signal_in_session(
        self,
        session: AsyncSession,
        draft: SignalDraft,
        *,
        situation_id: UUID | None,
    ) -> SignalRecord:
        # Locking the Event serializes first-Signal creation as well as later supersession.
        event = await session.scalar(
            select(Event)
            .where(
                Event.id == draft.event_id,
                Event.user_id == draft.user_id,
                Event.workspace_id == draft.workspace_id,
            )
            .with_for_update()
        )
        if event is None:
            raise RelevanceScopeError("Event scope is invalid")

        existing = await session.scalar(
            select(Signal).where(
                Signal.event_id == draft.event_id,
                Signal.user_id == draft.user_id,
                Signal.workspace_id == draft.workspace_id,
                Signal.evaluation_key == draft.evaluation_key,
            )
        )
        if existing is not None:
            return await _signal_record(session, existing)

        if draft.successful_attempt_id is not None:
            attempt = await session.scalar(
                select(RelevanceEvaluationAttempt).where(
                    RelevanceEvaluationAttempt.id == draft.successful_attempt_id,
                    RelevanceEvaluationAttempt.event_id == draft.event_id,
                    RelevanceEvaluationAttempt.user_id == draft.user_id,
                    RelevanceEvaluationAttempt.workspace_id == draft.workspace_id,
                    RelevanceEvaluationAttempt.evaluation_key == draft.evaluation_key,
                    RelevanceEvaluationAttempt.status == EvaluationAttemptStatus.SUCCEEDED,
                )
            )
            if attempt is None:
                raise RelevanceScopeError("successful attempt scope is invalid")
        if situation_id is not None:
            situation = await session.scalar(
                select(Situation.id).where(
                    Situation.id == situation_id,
                    Situation.user_id == draft.user_id,
                    Situation.workspace_id == draft.workspace_id,
                )
            )
            if situation is None:
                raise RelevanceScopeError("Situation scope is invalid")
        if draft.goal_matches:
            goal_ids = {match.goal_id for match in draft.goal_matches}
            found = set(
                (
                    await session.scalars(
                        select(Goal.id).where(
                            Goal.id.in_(goal_ids),
                            Goal.user_id == draft.user_id,
                            Goal.workspace_id == draft.workspace_id,
                        )
                    )
                ).all()
            )
            if found != goal_ids:
                raise RelevanceScopeError("Goal scope is invalid")

        current = await session.scalar(
            select(Signal)
            .where(
                Signal.event_id == draft.event_id,
                Signal.user_id == draft.user_id,
                Signal.workspace_id == draft.workspace_id,
                Signal.kind == SignalKind.RELEVANCE,
                Signal.is_current.is_(True),
            )
            .with_for_update()
        )
        if current is not None:
            current.is_current = False
            await session.flush()

        row = Signal(
            id=draft.id,
            event_id=draft.event_id,
            user_id=draft.user_id,
            workspace_id=draft.workspace_id,
            kind=SignalKind.RELEVANCE,
            schema_version=1,
            payload=cast(dict[str, JsonValue], draft.payload.model_dump(mode="json")),
            confidence=Decimal(str(draft.confidence)),
            disposition=draft.disposition,
            producer=draft.producer,
            provider=draft.provider,
            model=draft.model,
            classifier_version=draft.classifier_version,
            policy_version=draft.policy_version,
            input_digest=draft.input_digest,
            trigger=draft.trigger,
            operator_reason=draft.operator_reason,
            evaluation_key=draft.evaluation_key,
            successful_attempt_id=draft.successful_attempt_id,
            situation_id=situation_id,
            supersedes_signal_id=current.id if current is not None else None,
            is_current=True,
            created_at=draft.created_at,
        )
        session.add(row)
        await session.flush()
        for match in draft.goal_matches:
            session.add(
                SignalGoal(
                    signal_id=row.id,
                    goal_id=match.goal_id,
                    workspace_id=row.workspace_id,
                    user_id=row.user_id,
                    relevance=Decimal(str(match.relevance)),
                    contribution=match.contribution,
                    reasoning=match.reason,
                    created_at=draft.created_at,
                )
            )
        await session.flush()
        return await _signal_record(session, row)

    async def persist_signal(
        self, draft: SignalDraft, *, situation_id: UUID | None = None
    ) -> SignalRecord:
        async with self._database.session() as session:
            async with session.begin():
                return await self.persist_signal_in_session(
                    session, draft, situation_id=situation_id
                )

    async def find_duplicate(self, event: StoredEvent) -> UUID | None:
        if event.external_id is None or not event.external_id.strip():
            return None
        earlier = or_(
            Event.occurred_at < event.occurred_at,
            and_(Event.occurred_at == event.occurred_at, Event.id < event.id),
        )
        statement = (
            select(Event.id)
            .where(
                Event.id != event.id,
                Event.user_id == event.user_id,
                Event.workspace_id == event.workspace_id,
                Event.source == event.source,
                Event.external_id == event.external_id,
                earlier,
            )
            .order_by(Event.occurred_at, Event.id)
            .limit(1)
        )
        async with self._database.session() as session:
            return cast(UUID | None, await session.scalar(statement))

    async def list_unevaluated_event_ids(
        self, *, user_id: UUID, workspace_id: UUID, limit: int
    ) -> tuple[UUID, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        current_signal = exists(
            select(Signal.id).where(
                Signal.event_id == Event.id,
                Signal.user_id == user_id,
                Signal.workspace_id == workspace_id,
                Signal.kind == SignalKind.RELEVANCE,
                Signal.is_current.is_(True),
            )
        )
        statement = (
            select(Event.id)
            .where(
                Event.user_id == user_id,
                Event.workspace_id == workspace_id,
                ~current_signal,
            )
            .order_by(Event.occurred_at, Event.id)
            .limit(limit)
        )
        async with self._database.session() as session:
            return tuple((await session.scalars(statement)).all())

    async def get_stored_event(
        self, *, event_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> StoredEvent:
        statement = select(Event).where(
            Event.id == event_id,
            Event.user_id == user_id,
            Event.workspace_id == workspace_id,
        )
        async with self._database.session() as session:
            row = await session.scalar(statement)
        if row is None:
            raise RelevanceNotFoundError("Event not found")
        return StoredEvent(
            id=row.id,
            user_id=row.user_id,
            workspace_id=row.workspace_id,
            source=row.source,
            event_type=row.event_type,
            external_id=row.external_id,
            occurred_at=row.occurred_at,
            payload=dict(row.payload),
            correlation_keys=tuple(row.correlation_keys),
            schema_version=row.schema_version,
        )

    async def list_active_goal_contexts(
        self, *, user_id: UUID, workspace_id: UUID, limit: int
    ) -> tuple[GoalContext, ...]:
        if not 1 <= limit <= 20:
            raise ValueError("Goal limit must be between 1 and 20")
        statement = (
            select(Goal)
            .where(
                Goal.user_id == user_id,
                Goal.workspace_id == workspace_id,
                Goal.status == GoalStatus.ACTIVE,
            )
            .order_by(Goal.priority.desc(), Goal.created_at, Goal.id)
            .limit(limit)
        )
        async with self._database.session() as session:
            rows = (await session.scalars(statement)).all()
        return tuple(
            GoalContext(
                id=row.id,
                title=row.title,
                summary=row.objective,
                domain=row.domain,
                priority=row.priority,
            )
            for row in rows
        )

    async def list_situation_contexts(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        correlation_keys: tuple[str, ...],
        goal_ids: tuple[UUID, ...],
        limit: int,
    ) -> tuple[SituationContext, ...]:
        if not 1 <= limit <= 5:
            raise ValueError("Situation limit must be between 1 and 5")

        exact_match = exists(
            select(SituationCorrelationKey.situation_id).where(
                SituationCorrelationKey.situation_id == Situation.id,
                SituationCorrelationKey.user_id == user_id,
                SituationCorrelationKey.workspace_id == workspace_id,
                SituationCorrelationKey.correlation_key.in_(correlation_keys or ("",)),
            )
        )
        goal_match = exists(
            select(SituationGoal.situation_id).where(
                SituationGoal.situation_id == Situation.id,
                SituationGoal.user_id == user_id,
                SituationGoal.workspace_id == workspace_id,
                SituationGoal.goal_id.in_(goal_ids or (UUID(int=0),)),
            )
        )
        attention_rank = case(
            (Situation.attention == "URGENT", 4),
            (Situation.attention == "HIGH", 3),
            (Situation.attention == "NORMAL", 2),
            else_=1,
        )
        statement = (
            select(Situation)
            .where(
                Situation.user_id == user_id,
                Situation.workspace_id == workspace_id,
                Situation.lifecycle.not_in(("RESOLVED", "ABANDONED")),
                or_(exact_match, goal_match),
            )
            .order_by(
                case((exact_match, 1), else_=0).desc(),
                attention_rank.desc(),
                Situation.last_activity_at.desc(),
                Situation.id,
            )
            .limit(limit)
        )
        async with self._database.session() as session:
            rows = (await session.scalars(statement)).all()
        return tuple(
            SituationContext(
                id=row.id,
                title=row.title,
                summary=row.summary,
                current_state=row.current_state,
                attention=row.attention,
                last_activity_at=row.last_activity_at,
            )
            for row in rows
        )


def _attempt_record(row: RelevanceEvaluationAttempt) -> EvaluationAttemptRecord:
    return EvaluationAttemptRecord(
        id=row.id,
        event_id=row.event_id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        evaluation_key=row.evaluation_key,
        attempt_number=row.attempt_number,
        trigger=row.trigger,
        operator_reason=row.operator_reason,
        status=row.status,
        provider=row.provider,
        model=row.model,
        classifier_version=row.classifier_version,
        input_digest=row.input_digest,
        failure_code=row.failure_code,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


async def _signal_record(session: AsyncSession, row: Signal) -> SignalRecord:
    relationships = (
        await session.scalars(
            select(SignalGoal).where(SignalGoal.signal_id == row.id).order_by(SignalGoal.goal_id)
        )
    ).all()
    matches = tuple(
        GoalMatch(
            goal_id=relationship.goal_id,
            relevance=float(relationship.relevance),
            contribution=relationship.contribution,
            reason=relationship.reasoning,
        )
        for relationship in relationships
    )
    try:
        return SignalRecord(
            id=row.id,
            event_id=row.event_id,
            user_id=row.user_id,
            workspace_id=row.workspace_id,
            kind=row.kind,
            schema_version=row.schema_version,
            payload=cast(dict[str, JsonValue], dict(row.payload)),
            confidence=float(row.confidence),
            disposition=row.disposition,
            producer=row.producer,
            provider=row.provider,
            model=row.model,
            classifier_version=row.classifier_version,
            policy_version=row.policy_version,
            input_digest=row.input_digest,
            trigger=row.trigger,
            operator_reason=row.operator_reason,
            evaluation_key=row.evaluation_key,
            successful_attempt_id=row.successful_attempt_id,
            situation_id=row.situation_id,
            supersedes_signal_id=row.supersedes_signal_id,
            is_current=row.is_current,
            goal_matches=matches,
            created_at=row.created_at,
        )
    except ValidationError as error:
        raise RelevanceError("stored Signal is invalid") from error
