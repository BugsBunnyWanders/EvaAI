from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid5, uuid7

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from eva_ai.agent.errors import AgentConflictError, AgentNotFoundError, AgentScopeError
from eva_ai.agent.types import (
    AgentEventContext,
    AgentInvestigationResult,
    AgentRunRecord,
    AgentRunRequestedMessage,
    AgentRunStatus,
    AgentUsage,
    ToolCallAudit,
)
from eva_ai.db.models import AgentRun, ConnectorAccount, Event, OutboxMessage, Signal
from eva_ai.db.session import Database
from eva_ai.events.types import OutboxState
from eva_ai.notifications.repository import NotificationRepository
from eva_ai.notifications.types import NotificationKind
from eva_ai.relevance.types import RelevanceDisposition

_RUN_NAMESPACE = UUID("f52ef2db-4f65-5cca-a10a-a75780af41f9")
_OUTBOX_NAMESPACE = UUID("3ae646cc-43bc-5a5d-8431-18bf59b58ad7")


@dataclass(frozen=True, slots=True)
class AgentRunClaim:
    run_id: UUID
    claim_id: UUID
    user_id: UUID
    workspace_id: UUID
    attempt_count: int


@dataclass(frozen=True, slots=True)
class AgentRunSubject:
    run: AgentRunRecord
    event: AgentEventContext
    connector_id: UUID
    secret_reference: str
    gmail_thread_id: str
    signal_is_current: bool
    signal_disposition: RelevanceDisposition


class AgentRunRepository:
    def __init__(self, database: Database, notification_destination: str | None = None) -> None:
        self._database = database
        self._notification_destination = notification_destination
        self._notifications = NotificationRepository(database)

    async def schedule_in_session(
        self,
        session: AsyncSession,
        *,
        event_id: UUID,
        signal_id: UUID,
        situation_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
        destination: str,
        provider: str,
        model: str,
        agent_version: str,
        prompt_version: str,
        queued_at: datetime,
    ) -> AgentRunRecord:
        run_id = uuid5(_RUN_NAMESPACE, f"{workspace_id}:{user_id}:{signal_id}:{agent_version}")
        outbox_id = uuid5(_OUTBOX_NAMESPACE, str(run_id))
        statement = (
            insert(AgentRun)
            .values(
                id=run_id,
                user_id=user_id,
                workspace_id=workspace_id,
                event_id=event_id,
                signal_id=signal_id,
                situation_id=situation_id,
                status=AgentRunStatus.QUEUED,
                provider=provider,
                model=model,
                agent_version=agent_version,
                prompt_version=prompt_version,
                output_schema_version=1,
                queued_at=queued_at,
            )
            .on_conflict_do_nothing(constraint="uq_agent_runs_signal_version")
            .returning(AgentRun)
        )
        row = (await session.scalars(statement)).one_or_none()
        if row is None:
            row = await session.scalar(
                select(AgentRun).where(
                    AgentRun.workspace_id == workspace_id,
                    AgentRun.user_id == user_id,
                    AgentRun.signal_id == signal_id,
                    AgentRun.agent_version == agent_version,
                )
            )
            if row is None:
                raise AgentConflictError("scheduled AgentRun was not visible")
            return _record(row)

        envelope = AgentRunRequestedMessage(
            outbox_message_id=outbox_id,
            agent_run_id=run_id,
            event_id=event_id,
            signal_id=signal_id,
            situation_id=situation_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        session.add(
            OutboxMessage(
                id=outbox_id,
                event_id=event_id,
                destination=destination,
                message_type=envelope.message_type,
                schema_version=envelope.schema_version,
                payload=envelope.model_dump(mode="json"),
                state=OutboxState.PENDING,
                available_at=queued_at,
            )
        )
        await session.flush()
        return _record(row)

    async def claim(
        self,
        message: AgentRunRequestedMessage,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> AgentRunClaim | None:
        claim_id = uuid7()
        eligible = or_(
            AgentRun.status == AgentRunStatus.QUEUED,
            and_(
                AgentRun.status == AgentRunStatus.RETRYABLE_FAILURE,
                or_(AgentRun.next_retry_at.is_(None), AgentRun.next_retry_at <= now),
            ),
            and_(
                AgentRun.status == AgentRunStatus.RUNNING,
                AgentRun.lease_expires_at <= now,
            ),
        )
        statement = (
            update(AgentRun)
            .where(
                AgentRun.id == message.agent_run_id,
                AgentRun.event_id == message.event_id,
                AgentRun.signal_id == message.signal_id,
                AgentRun.situation_id == message.situation_id,
                AgentRun.user_id == message.user_id,
                AgentRun.workspace_id == message.workspace_id,
                eligible,
            )
            .values(
                status=AgentRunStatus.RUNNING,
                claim_id=claim_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempt_count=AgentRun.attempt_count + 1,
                started_at=now,
                completed_at=None,
                next_retry_at=None,
                failure_code=None,
                failure_summary=None,
            )
            .returning(AgentRun.attempt_count)
        )
        async with self._database.session() as session:
            async with session.begin():
                attempt = (await session.execute(statement)).scalar_one_or_none()
        if attempt is None:
            return None
        return AgentRunClaim(
            run_id=message.agent_run_id,
            claim_id=claim_id,
            user_id=message.user_id,
            workspace_id=message.workspace_id,
            attempt_count=attempt,
        )

    async def load_subject(self, claim: AgentRunClaim, body_max_chars: int) -> AgentRunSubject:
        statement = (
            select(AgentRun, Event, Signal, ConnectorAccount)
            .join(
                Event,
                and_(
                    Event.id == AgentRun.event_id,
                    Event.workspace_id == AgentRun.workspace_id,
                    Event.user_id == AgentRun.user_id,
                ),
            )
            .join(
                Signal,
                and_(
                    Signal.id == AgentRun.signal_id,
                    Signal.event_id == AgentRun.event_id,
                    Signal.workspace_id == AgentRun.workspace_id,
                    Signal.user_id == AgentRun.user_id,
                ),
            )
            .join(
                ConnectorAccount,
                and_(
                    ConnectorAccount.id == Event.principal_id,
                    ConnectorAccount.workspace_id == AgentRun.workspace_id,
                    ConnectorAccount.user_id == AgentRun.user_id,
                    ConnectorAccount.provider == "gmail",
                ),
            )
            .where(
                AgentRun.id == claim.run_id,
                AgentRun.user_id == claim.user_id,
                AgentRun.workspace_id == claim.workspace_id,
                AgentRun.status == AgentRunStatus.RUNNING,
                AgentRun.claim_id == claim.claim_id,
            )
        )
        async with self._database.session() as session:
            values = (await session.execute(statement)).one_or_none()
        if values is None:
            raise AgentScopeError("AgentRun subject is unavailable")
        run, event, signal, connector = values
        if connector.secret_reference is None:
            raise AgentScopeError("Gmail connector credentials are unavailable")
        thread_id = _string(event.payload.get("thread_id"))
        if not thread_id:
            raise AgentScopeError("Gmail thread is unavailable")
        return AgentRunSubject(
            run=_record(run),
            event=_event_context(event, body_max_chars),
            connector_id=connector.id,
            secret_reference=connector.secret_reference,
            gmail_thread_id=thread_id,
            signal_is_current=signal.is_current,
            signal_disposition=RelevanceDisposition(signal.disposition),
        )

    async def complete(
        self,
        claim: AgentRunClaim,
        *,
        result: AgentInvestigationResult,
        input_digest: str,
        provider_response_id: str | None,
        usage: AgentUsage,
        tool_audit: tuple[ToolCallAudit, ...],
        completed_at: datetime,
    ) -> AgentRunRecord:
        statement = (
            update(AgentRun)
            .where(_claim_predicate(claim))
            .values(
                status=AgentRunStatus.SUCCEEDED,
                input_digest=input_digest,
                result=result.model_dump(mode="json"),
                provider_response_id=provider_response_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                tool_audit=[item.model_dump(mode="json") for item in tool_audit],
                completed_at=completed_at,
                claim_id=None,
                lease_expires_at=None,
                next_retry_at=None,
                failure_code=None,
                failure_summary=None,
            )
            .returning(AgentRun)
        )
        async with self._database.session() as session:
            async with session.begin():
                row = (await session.scalars(statement)).one_or_none()
                if row is None:
                    raise AgentConflictError("AgentRun claim is stale")
                if result.notification is not None and self._notification_destination is not None:
                    # Agent completion and proactive delivery intent commit together. A crash can
                    # delay publication, but cannot leave a successful user-facing run invisible.
                    await self._notifications.create_in_session(
                        session,
                        event_id=row.event_id,
                        user_id=row.user_id,
                        workspace_id=row.workspace_id,
                        situation_id=row.situation_id,
                        agent_run_id=row.id,
                        kind=NotificationKind.PROACTIVE,
                        urgency=result.notification.urgency,
                        message=result.notification.message,
                        dedupe_key=(
                            f"agent-run:{row.id}:notification:v{row.output_schema_version}"
                        ),
                        destination=self._notification_destination,
                        created_at=completed_at,
                    )
                return _record(row)

    async def fail(
        self,
        claim: AgentRunClaim,
        *,
        retryable: bool,
        max_attempts: int,
        next_retry_at: datetime | None,
        failure_code: str,
        failure_summary: str,
        completed_at: datetime,
    ) -> AgentRunRecord:
        should_retry = retryable and claim.attempt_count < max_attempts
        statement = (
            update(AgentRun)
            .where(_claim_predicate(claim))
            .values(
                status=(
                    AgentRunStatus.RETRYABLE_FAILURE
                    if should_retry
                    else AgentRunStatus.PERMANENT_FAILURE
                ),
                next_retry_at=next_retry_at if should_retry else None,
                failure_code=failure_code,
                failure_summary=failure_summary,
                completed_at=completed_at,
                claim_id=None,
                lease_expires_at=None,
            )
            .returning(AgentRun)
        )
        return await self._finish(statement)

    async def get(
        self, *, run_id: UUID, user_id: UUID, workspace_id: UUID
    ) -> AgentRunRecord | None:
        statement = select(AgentRun).where(
            AgentRun.id == run_id,
            AgentRun.user_id == user_id,
            AgentRun.workspace_id == workspace_id,
        )
        async with self._database.session() as session:
            row = await session.scalar(statement)
        return None if row is None else _record(row)

    async def list(
        self, *, user_id: UUID, workspace_id: UUID, limit: int = 50
    ) -> tuple[AgentRunRecord, ...]:
        statement = (
            select(AgentRun)
            .where(AgentRun.user_id == user_id, AgentRun.workspace_id == workspace_id)
            .order_by(AgentRun.queued_at.desc(), AgentRun.id.desc())
            .limit(limit)
        )
        async with self._database.session() as session:
            rows = (await session.scalars(statement)).all()
        return tuple(_record(row) for row in rows)

    async def retry(
        self,
        *,
        run_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
        destination: str,
        requested_at: datetime,
    ) -> AgentRunRecord:
        async with self._database.session() as session:
            async with session.begin():
                row = await session.scalar(
                    select(AgentRun)
                    .where(
                        AgentRun.id == run_id,
                        AgentRun.user_id == user_id,
                        AgentRun.workspace_id == workspace_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise AgentNotFoundError("AgentRun not found")
                if row.status not in {
                    AgentRunStatus.RETRYABLE_FAILURE,
                    AgentRunStatus.PERMANENT_FAILURE,
                }:
                    raise AgentConflictError("AgentRun cannot be retried from its current state")
                outbox_id = uuid7()
                envelope = AgentRunRequestedMessage(
                    outbox_message_id=outbox_id,
                    agent_run_id=row.id,
                    event_id=row.event_id,
                    signal_id=row.signal_id,
                    situation_id=row.situation_id,
                    user_id=row.user_id,
                    workspace_id=row.workspace_id,
                )
                row.status = AgentRunStatus.QUEUED
                row.next_retry_at = None
                row.completed_at = None
                row.failure_code = None
                row.failure_summary = None
                session.add(
                    OutboxMessage(
                        id=outbox_id,
                        event_id=row.event_id,
                        destination=destination,
                        message_type=envelope.message_type,
                        schema_version=1,
                        payload=envelope.model_dump(mode="json"),
                        state=OutboxState.PENDING,
                        available_at=requested_at,
                    )
                )
                await session.flush()
                return _record(row)

    async def _finish(self, statement: Any) -> AgentRunRecord:
        async with self._database.session() as session:
            async with session.begin():
                row = (await session.scalars(statement)).one_or_none()
        if row is None:
            raise AgentConflictError("AgentRun claim is no longer current")
        return _record(row)


def _claim_predicate(claim: AgentRunClaim) -> Any:
    return and_(
        AgentRun.id == claim.run_id,
        AgentRun.user_id == claim.user_id,
        AgentRun.workspace_id == claim.workspace_id,
        AgentRun.status == AgentRunStatus.RUNNING,
        AgentRun.claim_id == claim.claim_id,
    )


def _record(row: AgentRun) -> AgentRunRecord:
    return AgentRunRecord(
        id=row.id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        event_id=row.event_id,
        signal_id=row.signal_id,
        situation_id=row.situation_id,
        status=row.status,
        provider=row.provider,
        model=row.model,
        agent_version=row.agent_version,
        prompt_version=row.prompt_version,
        output_schema_version=row.output_schema_version,
        attempt_count=row.attempt_count,
        next_retry_at=row.next_retry_at,
        claim_id=row.claim_id,
        lease_expires_at=row.lease_expires_at,
        input_digest=row.input_digest,
        result=(
            None if row.result is None else AgentInvestigationResult.model_validate(row.result)
        ),
        provider_response_id=row.provider_response_id,
        usage=AgentUsage(
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            total_tokens=row.total_tokens,
        ),
        tool_audit=tuple(ToolCallAudit.model_validate(item) for item in row.tool_audit),
        failure_code=row.failure_code,
        failure_summary=row.failure_summary,
        queued_at=row.queued_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _event_context(row: Event, body_max_chars: int) -> AgentEventContext:
    headers = row.payload.get("headers")
    header_values = headers if isinstance(headers, dict) else {}
    label_ids = row.payload.get("label_ids")
    attachments = row.payload.get("attachments")
    return AgentEventContext(
        event_id=row.id,
        event_type=row.event_type,
        occurred_at=row.occurred_at,
        sender=_string(header_values.get("from"))[:500],
        subject=_string(header_values.get("subject"))[:500],
        snippet=_string(row.payload.get("snippet"))[:1000],
        plain_text=_string(row.payload.get("plain_text"))[:body_max_chars],
        label_ids=tuple(value[:200] for value in label_ids if isinstance(value, str))
        if isinstance(label_ids, list)
        else (),
        attachments=tuple(value for value in attachments if isinstance(value, dict))[:50]
        if isinstance(attachments, list)
        else (),
    )


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""
