from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid7

from eva_ai.agent.contracts import InvestigationAgent
from eva_ai.agent.errors import AgentTransientError
from eva_ai.agent.repository import AgentRunClaim, AgentRunRepository, AgentRunSubject
from eva_ai.agent.service import AgentInvestigationService
from eva_ai.agent.types import (
    AgentDecision,
    AgentEventContext,
    AgentInvestigationResult,
    AgentRunRecord,
    AgentRunRequestedMessage,
    AgentRunStatus,
    AgentUsage,
    InvestigationOutcome,
    ToolCallAudit,
)
from eva_ai.connectors.gmail.contracts import CredentialStore, GmailClientFactory
from eva_ai.memory.context import MemoryContextBuilder
from eva_ai.memory.types import AgentWorkingContext, ContextSituation
from eva_ai.relevance.types import RelevanceDisposition
from eva_ai.situations.types import AttentionLevel

NOW = datetime(2026, 9, 7, tzinfo=UTC)


class Runs:
    def __init__(self, subject: AgentRunSubject, *, attempt_count: int = 1) -> None:
        self.subject = subject
        self.attempt_count = attempt_count
        self.completed = False
        self.failures: list[tuple[bool, str]] = []

    async def claim(
        self, message: AgentRunRequestedMessage, *, now: datetime, lease_seconds: int
    ) -> AgentRunClaim:
        return AgentRunClaim(
            message.agent_run_id,
            uuid7(),
            message.user_id,
            message.workspace_id,
            self.attempt_count,
        )

    async def load_subject(self, claim: AgentRunClaim, body_max_chars: int) -> AgentRunSubject:
        return self.subject

    async def complete(self, claim: AgentRunClaim, **kwargs: object) -> AgentRunRecord:
        self.completed = True
        return self.subject.run.model_copy(update={"status": AgentRunStatus.SUCCEEDED})

    async def fail(
        self,
        claim: AgentRunClaim,
        *,
        retryable: bool,
        failure_code: str,
        max_attempts: int,
        **kwargs: object,
    ) -> AgentRunRecord:
        self.failures.append((retryable, failure_code))
        should_retry = retryable and claim.attempt_count < max_attempts
        return self.subject.run.model_copy(
            update={
                "status": (
                    AgentRunStatus.RETRYABLE_FAILURE
                    if should_retry
                    else AgentRunStatus.PERMANENT_FAILURE
                )
            }
        )


class ContextBuilder:
    def __init__(self, context: AgentWorkingContext) -> None:
        self.context = context

    async def build_for_situation(self, **kwargs: object) -> AgentWorkingContext:
        return self.context


class Store:
    async def get(self, secret_reference: str) -> str:
        return "authorized-user-json"


class Client:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class Factory:
    def __init__(self, client: Client) -> None:
        self.client = client

    async def create(self, authorized_user_json: str) -> Any:
        return self.client


@dataclass(frozen=True)
class Invocation:
    result: AgentInvestigationResult
    provider_response_id: str | None
    usage: AgentUsage
    tool_audit: tuple[ToolCallAudit, ...] = ()


class Agent:
    def __init__(self, *, transient: bool = False) -> None:
        self.transient = transient

    async def investigate(self, request: object, reader: object) -> Invocation:
        if self.transient:
            raise AgentTransientError("safe")
        return Invocation(
            AgentInvestigationResult(
                decision=AgentDecision.NO_ACTION,
                reasoning_summary="No action is required.",
            ),
            "response-1",
            AgentUsage(input_tokens=5, output_tokens=2, total_tokens=7),
        )


async def test_service_persists_success_and_closes_gmail_client() -> None:
    message, subject, context = _fixture()
    runs = Runs(subject)
    client = Client()
    service = _service(runs, context, client, Agent())

    outcome = await service.process(message)

    assert outcome is InvestigationOutcome.SUCCEEDED
    assert runs.completed is True
    assert runs.failures == []
    assert client.closed is True


async def test_service_classifies_transient_agent_failure_for_retry() -> None:
    message, subject, context = _fixture()
    runs = Runs(subject)
    client = Client()
    service = _service(runs, context, client, Agent(transient=True))

    outcome = await service.process(message)

    assert outcome is InvestigationOutcome.RETRY
    assert runs.completed is False
    assert runs.failures == [(True, "PROVIDER_UNAVAILABLE")]
    assert client.closed is True


async def test_service_stops_retrying_after_attempt_budget_is_exhausted() -> None:
    message, subject, context = _fixture()
    runs = Runs(subject, attempt_count=3)
    client = Client()
    service = _service(runs, context, client, Agent(transient=True))

    outcome = await service.process(message)

    assert outcome is InvestigationOutcome.TERMINAL
    assert runs.failures == [(True, "PROVIDER_UNAVAILABLE")]
    assert client.closed is True


def _service(
    runs: Runs,
    context: AgentWorkingContext,
    client: Client,
    agent: Agent,
) -> AgentInvestigationService:
    return AgentInvestigationService(
        runs=cast(AgentRunRepository, runs),
        context_builder=cast(MemoryContextBuilder, ContextBuilder(context)),
        credential_store=cast(CredentialStore, Store()),
        gmail_clients=cast(GmailClientFactory, Factory(client)),
        agent=cast(InvestigationAgent, agent),
        lease_seconds=60,
        max_attempts=3,
        retry_initial_backoff_seconds=1,
        retry_max_backoff_seconds=10,
        thread_message_limit=10,
        search_result_limit=5,
        body_max_chars=1000,
        clock=lambda: NOW,
    )


def _fixture() -> tuple[AgentRunRequestedMessage, AgentRunSubject, AgentWorkingContext]:
    user_id, workspace_id, event_id, signal_id, situation_id, run_id = (
        uuid7(),
        uuid7(),
        uuid7(),
        uuid7(),
        uuid7(),
        uuid7(),
    )
    message = AgentRunRequestedMessage(
        outbox_message_id=uuid7(),
        agent_run_id=run_id,
        event_id=event_id,
        signal_id=signal_id,
        situation_id=situation_id,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    run = AgentRunRecord(
        id=run_id,
        user_id=user_id,
        workspace_id=workspace_id,
        event_id=event_id,
        signal_id=signal_id,
        situation_id=situation_id,
        status=AgentRunStatus.RUNNING,
        provider="openai",
        model="gpt-test",
        agent_version="v1",
        prompt_version="p1",
        output_schema_version=1,
        attempt_count=1,
        next_retry_at=None,
        claim_id=uuid7(),
        lease_expires_at=NOW,
        result=None,
        usage=AgentUsage(),
        tool_audit=(),
        queued_at=NOW,
        started_at=NOW,
        completed_at=None,
    )
    event = AgentEventContext(
        event_id=event_id,
        event_type="email.received",
        occurred_at=NOW,
        sender="person@example.com",
        subject="Meeting",
        snippet="Pick a time",
        plain_text="Can you meet tomorrow?",
    )
    context = AgentWorkingContext(
        user_id=user_id,
        workspace_id=workspace_id,
        situation=ContextSituation(
            id=situation_id,
            title="Meeting",
            summary="A time needs confirmation.",
            current_state="NEW",
            next_action=None,
            next_expected=None,
            attention=AttentionLevel.HIGH,
            last_activity_at=NOW,
        ),
        goals=(),
        facts=(),
        episodes=(),
        built_at=NOW,
        digest="a" * 64,
    )
    subject = AgentRunSubject(
        run=run,
        event=event,
        connector_id=uuid7(),
        secret_reference="secret",
        gmail_thread_id="thread-1",
        signal_is_current=True,
        signal_disposition=RelevanceDisposition.INVESTIGATE,
    )
    return message, subject, context
