from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

import pytest

from eva_ai.db import Database
from eva_ai.events.service import EventService
from eva_ai.events.types import NewEvent, PrincipalType
from eva_ai.relevance.repository import RelevanceRepository
from eva_ai.relevance.types import (
    DeterministicRelevancePayload,
    EvaluationAttemptStatus,
    EvaluationTrigger,
    FinishEvaluationAttempt,
    RelevanceDisposition,
    ScreeningReason,
    SignalDraft,
    SignalProducer,
    StartEvaluationAttempt,
)
from tests.integration.factories import Scope, create_scope

NOW = datetime(2026, 9, 1, tzinfo=UTC)


async def create_event(database: Database, scope: Scope, *, occurred_at: datetime = NOW) -> UUID:
    event = NewEvent(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        source="gmail",
        event_type="email.received",
        external_id=str(uuid7()),
        idempotency_key=str(uuid7()),
        occurred_at=occurred_at,
        principal_type=PrincipalType.EXTERNAL,
        payload={"message_id": "m", "thread_id": "t"},
        correlation_keys=["gmail-thread:t"],
    )
    return (await EventService(database, "eva-events").ingest(event)).event_id


def draft(scope: Scope, event_id: UUID, *, evaluation_key: UUID | None = None) -> SignalDraft:
    return SignalDraft(
        event_id=event_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        payload=DeterministicRelevancePayload(reason=ScreeningReason.IGNORED_LABEL),
        confidence=1,
        disposition=RelevanceDisposition.IGNORE,
        producer=SignalProducer.DETERMINISTIC,
        provider="application",
        classifier_version="v1",
        policy_version="p1",
        input_digest="a" * 64,
        trigger=EvaluationTrigger.INITIAL,
        evaluation_key=evaluation_key or uuid7(),
        created_at=NOW,
    )


@pytest.mark.integration
async def test_attempt_lifecycle_is_scoped_and_sanitized(database: Database) -> None:
    scope = await create_scope(database)
    event_id = await create_event(database, scope)
    repository = RelevanceRepository(database)
    key = uuid7()
    started = await repository.start_attempt(
        StartEvaluationAttempt(
            event_id=event_id,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            evaluation_key=key,
            attempt_number=1,
            trigger=EvaluationTrigger.INITIAL,
            provider="openai",
            model="gpt-test",
            classifier_version="v1",
            input_digest="b" * 64,
            started_at=NOW,
        )
    )
    finished = await repository.finish_attempt(
        FinishEvaluationAttempt(
            attempt_id=started.id,
            event_id=event_id,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            evaluation_key=key,
            status=EvaluationAttemptStatus.RETRYABLE_FAILURE,
            failure_code="RATE_LIMIT",
            completed_at=NOW + timedelta(seconds=1),
        )
    )
    assert finished.failure_code == "RATE_LIMIT"
    assert (
        await repository.latest_attempt(
            event_id=event_id,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            evaluation_key=key,
        )
        == finished
    )


@pytest.mark.integration
async def test_signal_supersession_and_old_key_replay(database: Database) -> None:
    scope = await create_scope(database)
    event_id = await create_event(database, scope)
    repository = RelevanceRepository(database)
    first_key, second_key = uuid7(), uuid7()
    first = await repository.persist_signal(draft(scope, event_id, evaluation_key=first_key))
    second = await repository.persist_signal(draft(scope, event_id, evaluation_key=second_key))
    replay = await repository.persist_signal(draft(scope, event_id, evaluation_key=first_key))

    assert second.supersedes_signal_id == first.id
    assert second.is_current is True
    assert replay.id == first.id and replay.is_current is False
    current = await repository.get_current(
        event_id=event_id, user_id=scope.user_id, workspace_id=scope.workspace_id
    )
    assert current is not None and current.id == second.id


@pytest.mark.integration
async def test_backfill_selection_is_scoped_bounded_and_ordered(database: Database) -> None:
    scope = await create_scope(database)
    first = await create_event(database, scope, occurred_at=NOW)
    second = await create_event(database, scope, occurred_at=NOW + timedelta(seconds=1))
    third = await create_event(database, scope, occurred_at=NOW + timedelta(seconds=2))
    repository = RelevanceRepository(database)
    await repository.persist_signal(draft(scope, second))

    selected = await repository.list_unevaluated_event_ids(
        user_id=scope.user_id, workspace_id=scope.workspace_id, limit=2
    )
    assert selected == (first, third)
