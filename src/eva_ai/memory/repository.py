from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, case, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from eva_ai.db.models import (
    EpisodicMemory,
    EpisodicMemoryGoal,
    Goal,
    MemoryFact,
    Situation,
    SituationGoal,
    User,
    Workspace,
)
from eva_ai.db.session import Database
from eva_ai.memory.embedding import EmbeddedText
from eva_ai.memory.errors import MemoryConflictError, MemoryNotFoundError, MemoryScopeError
from eva_ai.memory.types import (
    ContextGoal,
    ContextIdentity,
    ContextSituation,
    EpisodicMemoryDraft,
    EpisodicMemoryRecord,
    EpisodicMemoryStatus,
    MemoryFactDraft,
    MemoryFactRecord,
    MemoryFactStatus,
    MemoryScopeType,
    SemanticCandidate,
)


class MemoryRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def put_fact(self, draft: MemoryFactDraft) -> MemoryFactRecord:
        try:
            return await self._put_fact_transaction(draft)
        except IntegrityError:
            # The partial unique indexes arbitrate concurrent writers that both observed an empty
            # slot. Surface a stable domain conflict instead of leaking a database exception.
            raise MemoryConflictError from None

    async def _put_fact_transaction(self, draft: MemoryFactDraft) -> MemoryFactRecord:
        async with self._database.session() as session:
            async with session.begin():
                existing = await session.scalar(
                    select(MemoryFact).where(
                        MemoryFact.user_id == draft.user_id,
                        MemoryFact.workspace_id == draft.workspace_id,
                        MemoryFact.idempotency_key == draft.idempotency_key,
                    )
                )
                if existing is not None:
                    if not _same_fact_request(existing, draft):
                        raise MemoryConflictError
                    return _fact_record(existing)

                await _validate_fact_scope(session, draft)
                active = await session.scalar(_active_fact_statement(draft).with_for_update())
                predecessor_id = None
                if active is not None:
                    if draft.valid_from <= active.valid_from:
                        raise MemoryConflictError
                    active.status = MemoryFactStatus.SUPERSEDED
                    active.valid_until = draft.valid_from
                    predecessor_id = active.id
                    # Flush the predecessor first so the partial unique index never observes two
                    # active values for the same logical fact slot.
                    await session.flush()

                goal_id, situation_id = _fact_targets(draft)
                row = MemoryFact(
                    id=draft.id,
                    user_id=draft.user_id,
                    workspace_id=draft.workspace_id,
                    namespace=draft.namespace,
                    key=draft.key,
                    value_json=draft.value_json,
                    scope_type=draft.scope_type,
                    goal_id=goal_id,
                    situation_id=situation_id,
                    source_type=draft.source_type,
                    source_ref=draft.source_ref,
                    confidence=draft.confidence,
                    idempotency_key=draft.idempotency_key,
                    status=MemoryFactStatus.ACTIVE,
                    valid_from=draft.valid_from,
                    valid_until=draft.valid_until,
                    supersedes_memory_id=predecessor_id,
                )
                session.add(row)
                await session.flush()
                return _fact_record(row)

    async def get_fact(
        self, *, user_id: UUID, workspace_id: UUID, memory_id: UUID
    ) -> MemoryFactRecord | None:
        async with self._database.session() as session:
            row = await session.scalar(
                select(MemoryFact).where(
                    MemoryFact.id == memory_id,
                    MemoryFact.user_id == user_id,
                    MemoryFact.workspace_id == workspace_id,
                )
            )
        return _fact_record(row) if row is not None else None

    async def list_facts(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        statuses: tuple[MemoryFactStatus, ...] = (),
        limit: int = 50,
    ) -> tuple[MemoryFactRecord, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Memory fact limit must be between 1 and 100")
        active_first = case((MemoryFact.status == MemoryFactStatus.ACTIVE, 1), else_=0)
        statement = (
            select(MemoryFact)
            .where(MemoryFact.user_id == user_id, MemoryFact.workspace_id == workspace_id)
            .order_by(active_first.desc(), MemoryFact.created_at.desc(), MemoryFact.id.desc())
            .limit(limit)
        )
        if statuses:
            statement = statement.where(MemoryFact.status.in_(statuses))
        async with self._database.session() as session:
            rows = (await session.scalars(statement)).all()
        return tuple(_fact_record(row) for row in rows)

    async def retract_fact(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        memory_id: UUID,
        retracted_at: datetime,
    ) -> MemoryFactRecord:
        async with self._database.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(MemoryFact)
                    .where(
                        MemoryFact.id == memory_id,
                        MemoryFact.user_id == user_id,
                        MemoryFact.workspace_id == workspace_id,
                        MemoryFact.status == MemoryFactStatus.ACTIVE,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise MemoryNotFoundError
                if retracted_at <= row.valid_from:
                    raise MemoryConflictError
                row.status = MemoryFactStatus.RETRACTED
                row.valid_until = retracted_at
                await session.flush()
                return _fact_record(row)

    async def get_episode_by_idempotency(
        self, *, user_id: UUID, workspace_id: UUID, idempotency_key: str
    ) -> EpisodicMemoryRecord | None:
        async with self._database.session() as session:
            row = await session.scalar(
                select(EpisodicMemory).where(
                    EpisodicMemory.user_id == user_id,
                    EpisodicMemory.workspace_id == workspace_id,
                    EpisodicMemory.idempotency_key == idempotency_key,
                )
            )
            return await _episode_record(session, row) if row is not None else None

    async def find_episode_replay(self, draft: EpisodicMemoryDraft) -> EpisodicMemoryRecord | None:
        existing = await self.get_episode_by_idempotency(
            user_id=draft.user_id,
            workspace_id=draft.workspace_id,
            idempotency_key=draft.idempotency_key,
        )
        if existing is not None and not _same_episode_request(existing, draft):
            raise MemoryConflictError
        return existing

    async def create_episode(
        self, draft: EpisodicMemoryDraft, embedded: EmbeddedText
    ) -> EpisodicMemoryRecord:
        try:
            return await self._create_episode_transaction(draft, embedded)
        except IntegrityError:
            # A concurrent idempotency-key collision is safe to retry through find_episode_replay.
            raise MemoryConflictError from None

    async def _create_episode_transaction(
        self, draft: EpisodicMemoryDraft, embedded: EmbeddedText
    ) -> EpisodicMemoryRecord:
        async with self._database.session() as session:
            async with session.begin():
                existing = await session.scalar(
                    select(EpisodicMemory).where(
                        EpisodicMemory.user_id == draft.user_id,
                        EpisodicMemory.workspace_id == draft.workspace_id,
                        EpisodicMemory.idempotency_key == draft.idempotency_key,
                    )
                )
                if existing is not None:
                    record = await _episode_record(session, existing)
                    if not _same_episode_request(record, draft):
                        raise MemoryConflictError
                    return record

                await _validate_episode_scope(session, draft)
                row = EpisodicMemory(
                    id=draft.id,
                    user_id=draft.user_id,
                    workspace_id=draft.workspace_id,
                    type=draft.type,
                    summary=draft.summary,
                    entities=list(draft.entities),
                    situation_id=draft.situation_id,
                    importance=draft.importance,
                    confidence=draft.confidence,
                    source_type=draft.source_type,
                    source_ref=draft.source_ref,
                    idempotency_key=draft.idempotency_key,
                    status=EpisodicMemoryStatus.ACTIVE,
                    occurred_at=draft.occurred_at,
                    embedding=list(embedded.values),
                    embedding_model=embedded.model,
                    embedding_dimensions=embedded.dimensions,
                    embedding_input_digest=embedded.input_digest,
                    embedded_at=embedded.embedded_at,
                )
                session.add(row)
                session.add_all(
                    EpisodicMemoryGoal(
                        memory_id=draft.id,
                        goal_id=goal_id,
                        workspace_id=draft.workspace_id,
                        user_id=draft.user_id,
                    )
                    for goal_id in draft.goal_ids
                )
                await session.flush()
                return await _episode_record(session, row)

    async def get_episode(
        self, *, user_id: UUID, workspace_id: UUID, memory_id: UUID
    ) -> EpisodicMemoryRecord | None:
        async with self._database.session() as session:
            row = await session.scalar(
                select(EpisodicMemory).where(
                    EpisodicMemory.id == memory_id,
                    EpisodicMemory.user_id == user_id,
                    EpisodicMemory.workspace_id == workspace_id,
                )
            )
            return await _episode_record(session, row) if row is not None else None

    async def list_episodes(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        statuses: tuple[EpisodicMemoryStatus, ...] = (),
        limit: int = 50,
    ) -> tuple[EpisodicMemoryRecord, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Episode limit must be between 1 and 100")
        statement = (
            select(EpisodicMemory)
            .where(
                EpisodicMemory.user_id == user_id,
                EpisodicMemory.workspace_id == workspace_id,
            )
            .order_by(EpisodicMemory.occurred_at.desc(), EpisodicMemory.id.desc())
            .limit(limit)
        )
        if statuses:
            statement = statement.where(EpisodicMemory.status.in_(statuses))
        async with self._database.session() as session:
            rows = (await session.scalars(statement)).all()
            return tuple([await _episode_record(session, row) for row in rows])

    async def retract_episode(
        self, *, user_id: UUID, workspace_id: UUID, memory_id: UUID
    ) -> EpisodicMemoryRecord:
        async with self._database.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(EpisodicMemory)
                    .where(
                        EpisodicMemory.id == memory_id,
                        EpisodicMemory.user_id == user_id,
                        EpisodicMemory.workspace_id == workspace_id,
                        EpisodicMemory.status == EpisodicMemoryStatus.ACTIVE,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise MemoryNotFoundError
                row.status = EpisodicMemoryStatus.RETRACTED
                await session.flush()
                return await _episode_record(session, row)

    async def has_active_episodes(self, *, user_id: UUID, workspace_id: UUID) -> bool:
        async with self._database.session() as session:
            return (
                await session.scalar(
                    select(EpisodicMemory.id)
                    .where(
                        EpisodicMemory.user_id == user_id,
                        EpisodicMemory.workspace_id == workspace_id,
                        EpisodicMemory.status == EpisodicMemoryStatus.ACTIVE,
                    )
                    .limit(1)
                )
                is not None
            )

    async def semantic_candidates(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        embedding: EmbeddedText,
        limit: int,
    ) -> tuple[SemanticCandidate, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Candidate limit must be between 1 and 100")
        distance = EpisodicMemory.embedding.cosine_distance(list(embedding.values))
        statement = (
            select(EpisodicMemory, distance.label("distance"))
            .where(
                EpisodicMemory.user_id == user_id,
                EpisodicMemory.workspace_id == workspace_id,
                EpisodicMemory.status == EpisodicMemoryStatus.ACTIVE,
                EpisodicMemory.embedding_model == embedding.model,
                EpisodicMemory.embedding_dimensions == embedding.dimensions,
            )
            .order_by(distance, EpisodicMemory.id)
            .limit(limit)
        )
        async with self._database.session() as session:
            rows = (await session.execute(statement)).all()
            candidates = []
            for row in rows:
                candidates.append(
                    SemanticCandidate(
                        memory=await _episode_record(session, row[0]),
                        distance=max(0.0, float(row[1])),
                    )
                )
            return tuple(candidates)

    async def load_context_subject(
        self, *, user_id: UUID, workspace_id: UUID, situation_id: UUID
    ) -> tuple[ContextIdentity, ContextSituation, tuple[ContextGoal, ...]] | None:
        async with self._database.session() as session:
            identity = (
                await session.execute(
                    select(User, Workspace)
                    .join(Workspace, Workspace.user_id == User.id)
                    .where(
                        User.id == user_id,
                        Workspace.id == workspace_id,
                        Workspace.user_id == user_id,
                    )
                )
            ).one_or_none()
            if identity is None:
                return None
            user, workspace = identity
            situation = await session.scalar(
                select(Situation).where(
                    Situation.id == situation_id,
                    Situation.user_id == user_id,
                    Situation.workspace_id == workspace_id,
                )
            )
            if situation is None:
                return None
            goals = (
                await session.scalars(
                    select(Goal)
                    .join(
                        SituationGoal,
                        (SituationGoal.goal_id == Goal.id)
                        & (SituationGoal.user_id == Goal.user_id)
                        & (SituationGoal.workspace_id == Goal.workspace_id),
                    )
                    .where(
                        SituationGoal.situation_id == situation_id,
                        Goal.user_id == user_id,
                        Goal.workspace_id == workspace_id,
                        Goal.status == "ACTIVE",
                    )
                    .order_by(Goal.priority.desc(), Goal.created_at, Goal.id)
                )
            ).all()
        return (
            ContextIdentity(
                display_name=user.display_name,
                workspace_name=workspace.name,
            ),
            ContextSituation(
                id=situation.id,
                title=situation.title,
                summary=situation.summary,
                current_state=situation.current_state,
                next_action=situation.next_action,
                next_expected=situation.next_expected,
                attention=situation.attention,
                last_activity_at=situation.last_activity_at,
            ),
            tuple(
                ContextGoal(
                    id=goal.id,
                    title=goal.title,
                    objective=goal.objective,
                    domain=goal.domain,
                    priority=goal.priority,
                )
                for goal in goals
            ),
        )

    async def list_context_facts(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        situation_id: UUID,
        goal_ids: tuple[UUID, ...],
        at: datetime,
        limit: int,
    ) -> tuple[MemoryFactRecord, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Context fact limit must be between 1 and 100")
        scope_match = or_(
            MemoryFact.scope_type == MemoryScopeType.WORKSPACE,
            (MemoryFact.scope_type == MemoryScopeType.SITUATION)
            & (MemoryFact.situation_id == situation_id),
            (MemoryFact.scope_type == MemoryScopeType.GOAL)
            & (MemoryFact.goal_id.in_(goal_ids or (UUID(int=0),))),
        )
        scope_rank = case(
            (MemoryFact.scope_type == MemoryScopeType.SITUATION, 3),
            (MemoryFact.scope_type == MemoryScopeType.GOAL, 2),
            else_=1,
        )
        statement = (
            select(MemoryFact)
            .where(
                MemoryFact.user_id == user_id,
                MemoryFact.workspace_id == workspace_id,
                MemoryFact.status == MemoryFactStatus.ACTIVE,
                MemoryFact.valid_from <= at,
                or_(MemoryFact.valid_until.is_(None), MemoryFact.valid_until > at),
                scope_match,
            )
            .order_by(
                scope_rank.desc(),
                MemoryFact.confidence.desc(),
                MemoryFact.valid_from.desc(),
                MemoryFact.id,
            )
            .limit(limit)
        )
        async with self._database.session() as session:
            rows = (await session.scalars(statement)).all()
        return tuple(_fact_record(row) for row in rows)


async def _scope_exists(session: AsyncSession, user_id: UUID, workspace_id: UUID) -> bool:
    return (
        await session.scalar(
            select(Workspace.id).where(
                Workspace.id == workspace_id,
                Workspace.user_id == user_id,
            )
        )
        is not None
    )


async def _validate_fact_scope(session: AsyncSession, draft: MemoryFactDraft) -> None:
    if not await _scope_exists(session, draft.user_id, draft.workspace_id):
        raise MemoryScopeError
    if draft.scope_type is MemoryScopeType.GOAL:
        valid = await session.scalar(
            select(Goal.id).where(
                Goal.id == draft.scope_id,
                Goal.user_id == draft.user_id,
                Goal.workspace_id == draft.workspace_id,
            )
        )
        if valid is None:
            raise MemoryScopeError
    elif draft.scope_type is MemoryScopeType.SITUATION:
        valid = await session.scalar(
            select(Situation.id).where(
                Situation.id == draft.scope_id,
                Situation.user_id == draft.user_id,
                Situation.workspace_id == draft.workspace_id,
            )
        )
        if valid is None:
            raise MemoryScopeError


async def _validate_episode_scope(session: AsyncSession, draft: EpisodicMemoryDraft) -> None:
    if not await _scope_exists(session, draft.user_id, draft.workspace_id):
        raise MemoryScopeError
    if draft.situation_id is not None:
        situation = await session.scalar(
            select(Situation.id).where(
                Situation.id == draft.situation_id,
                Situation.user_id == draft.user_id,
                Situation.workspace_id == draft.workspace_id,
            )
        )
        if situation is None:
            raise MemoryScopeError
    if draft.goal_ids:
        goal_ids = set(
            await session.scalars(
                select(Goal.id).where(
                    Goal.id.in_(draft.goal_ids),
                    Goal.user_id == draft.user_id,
                    Goal.workspace_id == draft.workspace_id,
                )
            )
        )
        if goal_ids != set(draft.goal_ids):
            raise MemoryScopeError


def _fact_targets(draft: MemoryFactDraft) -> tuple[UUID | None, UUID | None]:
    return (
        draft.scope_id if draft.scope_type is MemoryScopeType.GOAL else None,
        draft.scope_id if draft.scope_type is MemoryScopeType.SITUATION else None,
    )


def _active_fact_statement(draft: MemoryFactDraft) -> Select[tuple[MemoryFact]]:
    goal_id, situation_id = _fact_targets(draft)
    return select(MemoryFact).where(
        MemoryFact.user_id == draft.user_id,
        MemoryFact.workspace_id == draft.workspace_id,
        MemoryFact.namespace == draft.namespace,
        MemoryFact.key == draft.key,
        MemoryFact.scope_type == draft.scope_type,
        MemoryFact.goal_id == goal_id,
        MemoryFact.situation_id == situation_id,
        MemoryFact.status == MemoryFactStatus.ACTIVE,
    )


def _fact_record(row: MemoryFact) -> MemoryFactRecord:
    scope_id = row.workspace_id
    if row.scope_type == MemoryScopeType.GOAL:
        assert row.goal_id is not None
        scope_id = row.goal_id
    elif row.scope_type == MemoryScopeType.SITUATION:
        assert row.situation_id is not None
        scope_id = row.situation_id
    return MemoryFactRecord(
        id=row.id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        namespace=row.namespace,
        key=row.key,
        value_json=row.value_json,
        scope_type=row.scope_type,
        scope_id=scope_id,
        source_type=row.source_type,
        source_ref=row.source_ref,
        confidence=row.confidence,
        idempotency_key=row.idempotency_key,
        status=row.status,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        supersedes_memory_id=row.supersedes_memory_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _episode_record(session: AsyncSession, row: EpisodicMemory) -> EpisodicMemoryRecord:
    goal_ids: Sequence[UUID] = (
        await session.scalars(
            select(EpisodicMemoryGoal.goal_id)
            .where(EpisodicMemoryGoal.memory_id == row.id)
            .order_by(EpisodicMemoryGoal.goal_id)
        )
    ).all()
    return EpisodicMemoryRecord(
        id=row.id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        type=row.type,
        summary=row.summary,
        entities=tuple(row.entities),
        goal_ids=tuple(goal_ids),
        situation_id=row.situation_id,
        importance=row.importance,
        confidence=row.confidence,
        source_type=row.source_type,
        source_ref=row.source_ref,
        idempotency_key=row.idempotency_key,
        occurred_at=row.occurred_at,
        status=row.status,
        embedding_model=row.embedding_model,
        embedding_dimensions=row.embedding_dimensions,
        embedding_input_digest=row.embedding_input_digest,
        embedded_at=row.embedded_at,
        created_at=row.created_at,
    )


def _same_fact_request(row: MemoryFact, draft: MemoryFactDraft) -> bool:
    goal_id, situation_id = _fact_targets(draft)
    return all(
        (
            row.namespace == draft.namespace,
            row.key == draft.key,
            row.value_json == draft.value_json,
            row.scope_type == draft.scope_type,
            row.goal_id == goal_id,
            row.situation_id == situation_id,
            row.source_type == draft.source_type,
            row.source_ref == draft.source_ref,
            row.confidence == draft.confidence,
            row.valid_from == draft.valid_from,
            row.valid_until == draft.valid_until,
        )
    )


def _same_episode_request(record: EpisodicMemoryRecord, draft: EpisodicMemoryDraft) -> bool:
    return all(
        (
            record.type == draft.type,
            record.summary == draft.summary,
            record.entities == draft.entities,
            record.goal_ids == draft.goal_ids,
            record.situation_id == draft.situation_id,
            record.importance == draft.importance,
            record.confidence == draft.confidence,
            record.source_type == draft.source_type,
            record.source_ref == draft.source_ref,
            record.occurred_at == draft.occurred_at,
        )
    )
