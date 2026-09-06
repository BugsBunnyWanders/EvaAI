import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eva_ai.db.models import EventProcessing
from eva_ai.db.session import Database
from eva_ai.events.processor import EventCommit, StoredEvent
from eva_ai.events.types import ProcessingStage
from eva_ai.relevance.classifier import ClassifierRunRequest, RelevanceClassifierRunner
from eva_ai.relevance.context import RelevanceContextBuilder, context_digest
from eva_ai.relevance.errors import RelevanceConflictError, RelevanceScopeError
from eva_ai.relevance.filters import (
    RelevanceRuleProvider,
    RelevanceRuleSet,
    ScreeningFacts,
    screen_event,
)
from eva_ai.relevance.policy import RelevanceRoutingPolicy
from eva_ai.relevance.repository import RelevanceRepository
from eva_ai.relevance.types import (
    AIRelevancePayload,
    DeterministicRelevancePayload,
    EvaluationTrigger,
    ReevaluateEvent,
    RelevanceDisposition,
    SignalDraft,
    SignalProducer,
    SignalRecord,
)
from eva_ai.situations.repository import SituationRepository
from eva_ai.situations.resolver import InitialSituationSnapshot
from eva_ai.situations.types import (
    AttentionLevel,
    ResolveEvent,
    SituationLifecycle,
    SituationType,
)

_EVALUATION_NAMESPACE = UUID("7ca0dd62-9cc2-5aaf-87d6-69c12f80cc43")


def initial_evaluation_key(event_id: UUID) -> UUID:
    return uuid5(_EVALUATION_NAMESPACE, f"initial:{event_id}")


def backfill_evaluation_key(event_id: UUID) -> UUID:
    return uuid5(_EVALUATION_NAMESPACE, f"backfill:{event_id}")


class BackfillSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    selected: int
    succeeded: int
    failed: int
    signal_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ExistingRelevanceCommit:
    signal: SignalRecord

    async def apply(self, session: AsyncSession, committed_at: datetime) -> ProcessingStage:
        return (
            ProcessingStage.CORRELATED
            if self.signal.situation_id is not None
            else ProcessingStage.CLASSIFIED
        )


@dataclass(frozen=True, slots=True)
class PreparedRelevanceCommit:
    draft: SignalDraft
    signals: RelevanceRepository
    situations: SituationRepository
    resolve_command: ResolveEvent
    correlation_key: str
    initial_snapshot: InitialSituationSnapshot

    async def apply(self, session: AsyncSession, committed_at: datetime) -> ProcessingStage:
        existing = await self.signals.get_by_evaluation_key_in_session(
            session,
            event_id=self.draft.event_id,
            user_id=self.draft.user_id,
            workspace_id=self.draft.workspace_id,
            evaluation_key=self.draft.evaluation_key,
        )
        if existing is not None:
            return (
                ProcessingStage.CORRELATED
                if existing.situation_id is not None
                else ProcessingStage.CLASSIFIED
            )

        situation_id = None
        if self.draft.disposition in {
            RelevanceDisposition.NOTIFY,
            RelevanceDisposition.INVESTIGATE,
        }:
            resolution = await self.situations.resolve_gmail_event_in_session(
                session,
                command=self.resolve_command,
                correlation_key=self.correlation_key,
                initial_snapshot=self.initial_snapshot,
            )
            situation_id = resolution.situation.id
        await self.signals.persist_signal_in_session(session, self.draft, situation_id=situation_id)
        return (
            ProcessingStage.CORRELATED if situation_id is not None else ProcessingStage.CLASSIFIED
        )


class RelevanceEventHandler:
    def __init__(
        self,
        *,
        signals: RelevanceRepository,
        situations: SituationRepository,
        rules: RelevanceRuleProvider,
        context_builder: RelevanceContextBuilder,
        classifier: RelevanceClassifierRunner,
        policy: RelevanceRoutingPolicy,
        classifier_version: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._signals = signals
        self._situations = situations
        self._rules = rules
        self._context_builder = context_builder
        self._classifier = classifier
        self._policy = policy
        self._classifier_version = classifier_version
        self._clock = clock

    async def prepare(self, event: StoredEvent) -> EventCommit:
        return await self.prepare_evaluation(
            event,
            evaluation_key=initial_evaluation_key(event.id),
            trigger=EvaluationTrigger.INITIAL,
            operator_reason=None,
            requested_at=self._clock(),
        )

    async def prepare_evaluation(
        self,
        event: StoredEvent,
        *,
        evaluation_key: UUID,
        trigger: EvaluationTrigger,
        operator_reason: str | None,
        requested_at: datetime,
    ) -> EventCommit:
        existing = await self._signals.get_by_evaluation_key(
            event_id=event.id,
            user_id=event.user_id,
            workspace_id=event.workspace_id,
            evaluation_key=evaluation_key,
        )
        if existing is not None:
            return ExistingRelevanceCommit(existing)
        if trigger is not EvaluationTrigger.EXPLICIT_REEVALUATION:
            current = await self._signals.get_current(
                event_id=event.id,
                user_id=event.user_id,
                workspace_id=event.workspace_id,
            )
            if current is not None:
                return ExistingRelevanceCommit(current)

        rules = await self._rules.for_scope(user_id=event.user_id, workspace_id=event.workspace_id)
        duplicate = await self._signals.find_duplicate(event)
        screening = screen_event(event, ScreeningFacts(duplicate), rules)
        if screening is not None:
            draft = SignalDraft(
                event_id=event.id,
                user_id=event.user_id,
                workspace_id=event.workspace_id,
                payload=DeterministicRelevancePayload(reason=screening.reason),
                confidence=1,
                disposition=RelevanceDisposition.IGNORE,
                producer=SignalProducer.DETERMINISTIC,
                provider="deterministic",
                classifier_version=self._classifier_version,
                policy_version=self._policy.version,
                input_digest=_deterministic_digest(
                    event, screening.reason.value, rules, self._policy.version
                ),
                trigger=trigger,
                operator_reason=operator_reason,
                evaluation_key=evaluation_key,
                created_at=requested_at,
            )
        else:
            context = await self._context_builder.build(event)
            digest = context_digest(context)
            completed = await self._classifier.run(
                ClassifierRunRequest(
                    context=context,
                    input_digest=digest,
                    evaluation_key=evaluation_key,
                    trigger=trigger,
                    operator_reason=operator_reason,
                )
            )
            allowed_goal_ids = {goal.id for goal in context.goals}
            if any(
                match.goal_id not in allowed_goal_ids for match in completed.result.goal_matches
            ):
                raise RelevanceScopeError("classifier returned an unavailable Goal")
            disposition = self._policy.route(completed.result)
            draft = SignalDraft(
                event_id=event.id,
                user_id=event.user_id,
                workspace_id=event.workspace_id,
                payload=AIRelevancePayload(
                    result=completed.result,
                    disposition=disposition,
                ),
                confidence=completed.result.confidence,
                disposition=disposition,
                producer=SignalProducer.AI,
                provider=self._classifier.provider,
                model=self._classifier.model,
                classifier_version=self._classifier_version,
                policy_version=self._policy.version,
                input_digest=digest,
                trigger=trigger,
                operator_reason=operator_reason,
                evaluation_key=evaluation_key,
                successful_attempt_id=completed.successful_attempt_id,
                goal_matches=completed.result.goal_matches,
                created_at=requested_at,
            )
        return PreparedRelevanceCommit(
            draft=draft,
            signals=self._signals,
            situations=self._situations,
            resolve_command=ResolveEvent(
                event_id=event.id,
                user_id=event.user_id,
                workspace_id=event.workspace_id,
                goal_ids=tuple(match.goal_id for match in draft.goal_matches),
                resolved_at=requested_at,
            ),
            correlation_key=_gmail_correlation_key(event),
            initial_snapshot=_initial_snapshot(event),
        )


class RelevanceService:
    def __init__(
        self,
        database: Database,
        repository: RelevanceRepository,
        handler: RelevanceEventHandler,
    ) -> None:
        self._database = database
        self._repository = repository
        self._handler = handler

    async def reevaluate(self, command: ReevaluateEvent) -> SignalRecord:
        event = await self._repository.get_stored_event(
            event_id=command.event_id,
            user_id=command.user_id,
            workspace_id=command.workspace_id,
        )
        prepared = await self._handler.prepare_evaluation(
            event,
            evaluation_key=command.evaluation_key,
            trigger=EvaluationTrigger.EXPLICIT_REEVALUATION,
            operator_reason=command.reason,
            requested_at=command.requested_at,
        )
        await self._apply_direct(command.event_id, prepared, command.requested_at)
        result = await self._repository.get_by_evaluation_key(
            event_id=command.event_id,
            user_id=command.user_id,
            workspace_id=command.workspace_id,
            evaluation_key=command.evaluation_key,
        )
        if result is None:
            raise RelevanceScopeError("re-evaluation did not create a Signal")
        return result

    async def backfill(self, *, user_id: UUID, workspace_id: UUID, limit: int) -> BackfillSummary:
        event_ids = await self._repository.list_unevaluated_event_ids(
            user_id=user_id, workspace_id=workspace_id, limit=limit
        )
        signal_ids: list[UUID] = []
        failed = 0
        for event_id in event_ids:
            try:
                event = await self._repository.get_stored_event(
                    event_id=event_id, user_id=user_id, workspace_id=workspace_id
                )
                key = backfill_evaluation_key(event_id)
                prepared = await self._handler.prepare_evaluation(
                    event,
                    evaluation_key=key,
                    trigger=EvaluationTrigger.BACKFILL,
                    operator_reason=None,
                    requested_at=datetime.now(UTC),
                )
                await self._apply_direct(event_id, prepared, datetime.now(UTC))
                signal = await self._repository.get_by_evaluation_key(
                    event_id=event_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    evaluation_key=key,
                )
                if signal is None:
                    # Another worker may have evaluated an Event after this batch was selected.
                    signal = await self._repository.get_current(
                        event_id=event_id,
                        user_id=user_id,
                        workspace_id=workspace_id,
                    )
                if signal is None:
                    raise RelevanceScopeError("backfill did not create a Signal")
                signal_ids.append(signal.id)
            except Exception:
                # Backfill reports only counts; per-Event exception text is never surfaced.
                failed += 1
        return BackfillSummary(
            selected=len(event_ids),
            succeeded=len(signal_ids),
            failed=failed,
            signal_ids=tuple(signal_ids),
        )

    async def _apply_direct(
        self, event_id: UUID, prepared: EventCommit, committed_at: datetime
    ) -> None:
        async with self._database.session() as session:
            async with session.begin():
                processing = await session.scalar(
                    select(EventProcessing)
                    .where(EventProcessing.event_id == event_id)
                    .with_for_update()
                )
                if processing is None:
                    raise RelevanceScopeError("Event processing state not found")
                if processing.stage != ProcessingStage.HANDLED and processing.claim_id is not None:
                    raise RelevanceConflictError("Event has an active processing claim")
                await prepared.apply(session, committed_at)
                if processing.stage != ProcessingStage.HANDLED:
                    processing.stage = ProcessingStage.HANDLED
                    processing.processed_at = committed_at
                    processing.claim_id = None
                    processing.lease_expires_at = None


def _deterministic_digest(
    event: StoredEvent, reason: str, rules: RelevanceRuleSet, policy_version: str
) -> str:
    value = {
        "event_id": str(event.id),
        "event_schema_version": event.schema_version,
        "reason": reason,
        "policy_version": policy_version,
        "rules": {
            "ignored_sources": rules.ignored_sources,
            "ignored_event_types": rules.ignored_event_types,
            "ignored_senders": rules.ignored_senders,
            "ignored_labels": rules.ignored_labels,
        },
    }
    canonical = json.dumps(value, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _gmail_correlation_key(event: StoredEvent) -> str:
    keys = tuple(key for key in event.correlation_keys if key.startswith("gmail-thread:"))
    return keys[0] if len(keys) == 1 else ""


def _initial_snapshot(event: StoredEvent) -> InitialSituationSnapshot:
    headers = event.payload.get("headers")
    raw_subject = headers.get("subject") if isinstance(headers, dict) else None
    raw_snippet = event.payload.get("snippet")
    title = " ".join(raw_subject.split())[:300] if isinstance(raw_subject, str) else ""
    summary = " ".join(raw_snippet.split())[:2000] if isinstance(raw_snippet, str) else ""
    return InitialSituationSnapshot(
        type=SituationType.EMAIL_THREAD,
        title=title or "Gmail conversation",
        lifecycle=SituationLifecycle.OPEN,
        attention=AttentionLevel.NORMAL,
        summary=summary,
        current_state="NEW",
        next_action=None,
        next_expected=None,
        last_activity_at=event.occurred_at,
    )
