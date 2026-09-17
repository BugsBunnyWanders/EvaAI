import base64
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import Message
from email.parser import BytesParser
from typing import Any, cast
from uuid import UUID, uuid7

import pytest

from eva_ai.actions.canonical import CanonicalEmail, canonical_email_hash
from eva_ai.actions.executor import ActionExecutor
from eva_ai.actions.types import (
    ActionClaim,
    ActionClaimOutcome,
    ActionClaimResult,
    ActionExecutionOutcome,
    ActionExecutionSubject,
    ActionOrigin,
    ActionProposalRecord,
    ActionProposalStatus,
    ActionTaskRequest,
    ApprovalRecord,
    ApprovalStatus,
    GmailActionCapability,
    ManagedDraftStatus,
    ManagedGmailDraftRecord,
    PolicyDecision,
    TelegramApprovalPrincipal,
)
from eva_ai.connectors.gmail.contracts import (
    AuthorizationRevoked,
    GmailActionClient,
    GmailDraftResult,
    GmailSendResult,
)
from eva_ai.connectors.gmail.mime import build_gmail_mime_message
from eva_ai.connectors.types import ConnectorStatus
from eva_ai.integrations.gmail.oauth import GMAIL_COMPOSE_SCOPE, GMAIL_READONLY_SCOPE

NOW = datetime(2030, 1, 1, tzinfo=UTC)


class Credentials:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.references: list[str] = []

    async def get(self, secret_reference: str) -> str:
        self.references.append(secret_reference)
        if self.error is not None:
            raise self.error
        return "authorized-user-json"


class Client:
    def __init__(
        self,
        *,
        provider_draft: Mapping[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.provider_draft = provider_draft or {}
        self.error = error
        self.calls: list[tuple[str, Any]] = []

    async def create_draft(self, raw: str, thread_id: str | None) -> GmailDraftResult:
        self.calls.append(("create", (raw, thread_id)))
        if self.error is not None:
            raise self.error
        return GmailDraftResult("draft-1", "message-1", thread_id)

    async def update_draft(
        self, draft_id: str, raw: str, thread_id: str | None
    ) -> GmailDraftResult:
        self.calls.append(("update", (draft_id, raw, thread_id)))
        if self.error is not None:
            raise self.error
        return GmailDraftResult(draft_id, "message-2", thread_id)

    async def delete_draft(self, draft_id: str) -> None:
        self.calls.append(("delete", draft_id))
        if self.error is not None:
            raise self.error

    async def send_draft(self, draft_id: str) -> GmailSendResult:
        self.calls.append(("send", draft_id))
        if self.error is not None:
            raise self.error
        return GmailSendResult("sent-message", "sent-thread")

    async def get_draft(self, draft_id: str) -> Mapping[str, object]:
        self.calls.append(("get", draft_id))
        return self.provider_draft

    async def close(self) -> None:
        self.calls.append(("close", None))


class Factory:
    def __init__(self, client: Client) -> None:
        self.client = client
        self.credentials: list[str] = []

    async def create_action(self, authorized_user_json: str) -> GmailActionClient:
        self.credentials.append(authorized_user_json)
        return cast(GmailActionClient, self.client)


class Repository:
    def __init__(
        self,
        subject: ActionExecutionSubject,
        *,
        claim_outcome: ActionClaimOutcome = ActionClaimOutcome.CLAIMED,
    ) -> None:
        self.subject = subject
        self.claim_outcome = claim_outcome
        self.calls: list[tuple[str, Any]] = []

    async def claim_action(
        self, request: ActionTaskRequest, *, now: datetime, lease_seconds: int
    ) -> ActionClaimResult:
        self.calls.append(("claim", (request, now, lease_seconds)))
        claim = self.subject.claim if self.claim_outcome is ActionClaimOutcome.CLAIMED else None
        return ActionClaimResult(outcome=self.claim_outcome, claim=claim)

    async def load_execution_subject(self, claim: ActionClaim) -> ActionExecutionSubject:
        self.calls.append(("load", claim))
        return self.subject

    async def mark_provider_call_started(self, claim: ActionClaim, *, started_at: datetime) -> bool:
        self.calls.append(("boundary", (claim, started_at)))
        return True

    async def complete_create_draft(self, claim: ActionClaim, **values: object) -> None:
        self.calls.append(("complete_create", (claim, values)))

    async def complete_update_draft(self, claim: ActionClaim, **values: object) -> None:
        self.calls.append(("complete_update", (claim, values)))

    async def complete_delete_draft(self, claim: ActionClaim, **values: object) -> None:
        self.calls.append(("complete_delete", (claim, values)))

    async def complete_send_draft(self, claim: ActionClaim, **values: object) -> None:
        self.calls.append(("complete_send", (claim, values)))

    async def release_pre_provider_failure(
        self, claim: ActionClaim, *, failed_at: datetime
    ) -> None:
        self.calls.append(("release", (claim, failed_at)))

    async def mark_execution_unknown(self, claim: ActionClaim, *, failed_at: datetime) -> None:
        self.calls.append(("unknown", (claim, failed_at)))

    async def mark_execution_unavailable(
        self,
        claim: ActionClaim,
        *,
        connector_id: UUID,
        read_access_preserved: bool,
        failed_at: datetime,
    ) -> None:
        self.calls.append(("unavailable", (claim, connector_id, read_access_preserved, failed_at)))

    async def fail_validation(self, claim: ActionClaim, *, failed_at: datetime) -> None:
        self.calls.append(("invalid", (claim, failed_at)))


def _message() -> CanonicalEmail:
    return CanonicalEmail(
        mode="NEW",
        to=("person@example.com",),
        subject="Hello",
        text_body="A useful body.",
    )


def _subject(
    capability: GmailActionCapability,
    *,
    message: CanonicalEmail | None = None,
) -> ActionExecutionSubject:
    action_id = uuid7()
    proposal_id = uuid7()
    user_id = uuid7()
    workspace_id = uuid7()
    connector_id = uuid7()
    message = message or _message()
    managed = None
    approval = None
    policy = PolicyDecision.ALLOW
    proposal_status = ActionProposalStatus.QUEUED
    telegram = TelegramApprovalPrincipal(
        telegram_account_id=uuid7(),
        provider_chat_id=12345,
    )
    if capability is not GmailActionCapability.CREATE_DRAFT:
        managed = ManagedGmailDraftRecord(
            id=uuid7(),
            user_id=user_id,
            workspace_id=workspace_id,
            connector_account_id=connector_id,
            provider_draft_id="draft-1",
            provider_message_id="message-1",
            gmail_thread_id=message.thread_id,
            create_proposal_id=uuid7(),
            active_send_proposal_id=proposal_id,
            send_proposal_version=1,
            current_content_hash=canonical_email_hash(message),
            rfc_message_id="<managed@eva.evaatyourservice.com>",
            status=(
                ManagedDraftStatus.SENDING
                if capability is GmailActionCapability.SEND_DRAFT
                else ManagedDraftStatus.READY
            ),
            created_at=NOW,
            updated_at=NOW,
            sent_at=None,
            deleted_at=None,
        )
    if capability is GmailActionCapability.SEND_DRAFT:
        policy = PolicyDecision.REQUIRE_APPROVAL
        proposal_status = ActionProposalStatus.APPROVED
        approval = ApprovalRecord(
            id=uuid7(),
            proposal_id=proposal_id,
            user_id=user_id,
            workspace_id=workspace_id,
            parameters_hash=canonical_email_hash(message),
            principal_type="TELEGRAM",
            telegram_account_id=telegram.telegram_account_id,
            provider_chat_id=telegram.provider_chat_id,
            status=ApprovalStatus.GRANTED,
            requested_at=NOW - timedelta(minutes=1),
            decided_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
    return ActionExecutionSubject(
        claim=ActionClaim(
            action_id=action_id,
            proposal_id=proposal_id,
            claim_id=uuid7(),
            user_id=user_id,
            workspace_id=workspace_id,
            capability=capability,
            attempt_count=1,
        ),
        proposal=ActionProposalRecord(
            id=proposal_id,
            user_id=user_id,
            workspace_id=workspace_id,
            connector_account_id=connector_id,
            event_id=uuid7(),
            situation_id=None,
            goal_id=None,
            agent_run_id=None,
            conversation_turn_id=None,
            supersedes_proposal_id=None,
            source_key=f"test:{proposal_id}",
            proposal_family_key=f"test:{proposal_id}",
            origin=ActionOrigin.TELEGRAM_USER,
            capability=capability,
            message=message,
            parameters_hash=canonical_email_hash(message),
            description="Perform action",
            risk_level="high" if capability is GmailActionCapability.SEND_DRAFT else "low",
            policy_decision=policy,
            status=proposal_status,
            version=1,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            terminal_at=None,
        ),
        connector_id=connector_id,
        connector_identity="owner@example.com",
        connector_status=ConnectorStatus.ACTIVE,
        connector_scopes=(GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE),
        secret_reference="projects/eva/secrets/gmail/versions/1",
        managed_draft=managed,
        approval=approval,
        telegram_principal=telegram,
    )


def _executor(
    subject: ActionExecutionSubject,
    client: Client,
    *,
    credentials: Credentials | None = None,
    claim_outcome: ActionClaimOutcome = ActionClaimOutcome.CLAIMED,
) -> tuple[ActionExecutor, Repository]:
    repository = Repository(subject, claim_outcome=claim_outcome)
    executor = ActionExecutor(
        repository,
        credentials or Credentials(),
        Factory(client),
        lease_seconds=60,
        approval_ttl=timedelta(hours=24),
        clock=lambda: NOW,
    )
    return executor, repository


def _gmail_normalized_draft(
    subject: ActionExecutionSubject,
    *,
    mutate: Callable[[Message], None] | None = None,
) -> Mapping[str, object]:
    """Mirror the transport-only headers Gmail added to the production draft."""

    assert subject.managed_draft is not None
    expected_raw = build_gmail_mime_message(
        subject.proposal.message,
        sender=subject.connector_identity,
        rfc_message_id=subject.managed_draft.rfc_message_id,
    ).raw
    expected_bytes = base64.urlsafe_b64decode(expected_raw + ("=" * (-len(expected_raw) % 4)))
    message = BytesParser(policy=policy.SMTP).parsebytes(expected_bytes)
    message["Received"] = "by gmail.example.test with SMTP id provider-generated"
    message["Date"] = "Thu, 18 Sep 2026 02:06:58 +0530"
    del message["Message-ID"]
    message["Message-ID"] = "<provider-generated@gmail.example.test>"
    if mutate is not None:
        mutate(message)
    normalized_raw = base64.urlsafe_b64encode(message.as_bytes(policy=policy.SMTP)).decode()
    return {
        "id": subject.managed_draft.provider_draft_id,
        "message": {"raw": normalized_raw.rstrip("=")},
    }


def _replace_header(name: str, value: str) -> Callable[[Message], None]:
    def replace(message: Message) -> None:
        message.replace_header(name, value)

    return replace


def _replace_body(message: Message) -> None:
    message.set_payload("Changed after approval.\r\n")


@pytest.mark.asyncio
async def test_create_success_marks_boundary_and_creates_pending_send_approval() -> None:
    subject = _subject(GmailActionCapability.CREATE_DRAFT)
    executor, repository = _executor(subject, Client())

    result = await executor.execute(ActionTaskRequest(action_id=subject.claim.action_id))

    assert result.outcome is ActionExecutionOutcome.SUCCEEDED
    names = [name for name, _ in repository.calls]
    assert names.index("boundary") < names.index("complete_create")
    _, (_, values) = repository.calls[names.index("complete_create")]
    assert values["provider_draft_id"] == "draft-1"
    assert len(cast(str, values["callback_token_digest"])) == 64
    assert "message" not in result.model_dump()
    assert "recipient" not in result.model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capability,expected_provider_call,expected_completion",
    [
        (GmailActionCapability.UPDATE_DRAFT, "update", "complete_update"),
        (GmailActionCapability.DELETE_DRAFT, "delete", "complete_delete"),
    ],
)
async def test_managed_draft_mutations_use_only_persisted_provider_identity(
    capability: GmailActionCapability,
    expected_provider_call: str,
    expected_completion: str,
) -> None:
    subject = _subject(capability)
    client = Client()
    executor, repository = _executor(subject, client)

    result = await executor.execute(ActionTaskRequest(action_id=subject.claim.action_id))

    assert result.outcome is ActionExecutionOutcome.SUCCEEDED
    assert expected_provider_call in [name for name, _ in client.calls]
    assert expected_completion in [name for name, _ in repository.calls]


@pytest.mark.asyncio
async def test_send_requires_exact_current_provider_draft_content() -> None:
    subject = _subject(GmailActionCapability.SEND_DRAFT)
    assert subject.managed_draft is not None
    expected_raw = build_gmail_mime_message(
        subject.proposal.message,
        sender=subject.connector_identity,
        rfc_message_id=subject.managed_draft.rfc_message_id,
    ).raw
    client = Client(provider_draft={"id": "draft-1", "message": {"raw": expected_raw}})
    executor, repository = _executor(subject, client)

    result = await executor.execute(ActionTaskRequest(action_id=subject.claim.action_id))

    assert result.outcome is ActionExecutionOutcome.SUCCEEDED
    assert [name for name, _ in client.calls][:2] == ["get", "send"]
    assert "complete_send" in [name for name, _ in repository.calls]


@pytest.mark.asyncio
async def test_send_accepts_gmail_owned_transport_normalization() -> None:
    reply = CanonicalEmail(
        mode="REPLY",
        to=("person@example.com",),
        subject="Re: Dinner",
        text_body="Yes, I would be happy to join.",
        thread_id="thread-1",
        in_reply_to="<incoming@example.com>",
        references=("<earlier@example.com>", "<incoming@example.com>"),
    )
    subject = _subject(GmailActionCapability.SEND_DRAFT, message=reply)
    client = Client(provider_draft=_gmail_normalized_draft(subject))
    executor, repository = _executor(subject, client)

    result = await executor.execute(ActionTaskRequest(action_id=subject.claim.action_id))

    assert result.outcome is ActionExecutionOutcome.SUCCEEDED
    assert [name for name, _ in client.calls][:2] == ["get", "send"]
    assert "complete_send" in [name for name, _ in repository.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        _replace_header("To", "attacker@example.com"),
        _replace_header("Subject", "Changed subject"),
        _replace_header("In-Reply-To", "<different@example.com>"),
        _replace_body,
    ],
    ids=("recipient", "subject", "reply-context", "body"),
)
async def test_send_rejects_security_relevant_changes_after_approval(
    mutate: Callable[[Message], None],
) -> None:
    reply = CanonicalEmail(
        mode="REPLY",
        to=("person@example.com",),
        subject="Re: Dinner",
        text_body="Yes, I would be happy to join.",
        thread_id="thread-1",
        in_reply_to="<incoming@example.com>",
        references=("<earlier@example.com>", "<incoming@example.com>"),
    )
    subject = _subject(GmailActionCapability.SEND_DRAFT, message=reply)
    client = Client(provider_draft=_gmail_normalized_draft(subject, mutate=mutate))
    executor, repository = _executor(subject, client)

    result = await executor.execute(ActionTaskRequest(action_id=subject.claim.action_id))

    assert result.outcome is ActionExecutionOutcome.TERMINAL
    assert [name for name, _ in client.calls] == ["get", "close"]
    assert "invalid" in [name for name, _ in repository.calls]
    assert "boundary" not in [name for name, _ in repository.calls]


@pytest.mark.asyncio
async def test_send_hash_mismatch_fails_before_provider_mutation() -> None:
    subject = _subject(GmailActionCapability.SEND_DRAFT)
    client = Client(provider_draft={"id": "draft-1", "message": {"raw": "changed"}})
    executor, repository = _executor(subject, client)

    result = await executor.execute(ActionTaskRequest(action_id=subject.claim.action_id))

    assert result.outcome is ActionExecutionOutcome.TERMINAL
    assert [name for name, _ in client.calls] == ["get", "close"]
    assert "invalid" in [name for name, _ in repository.calls]
    assert "boundary" not in [name for name, _ in repository.calls]


@pytest.mark.asyncio
async def test_pre_provider_transient_failure_is_retryable() -> None:
    subject = _subject(GmailActionCapability.CREATE_DRAFT)
    executor, repository = _executor(
        subject,
        Client(),
        credentials=Credentials(error=RuntimeError("secret manager unavailable")),
    )

    result = await executor.execute(ActionTaskRequest(action_id=subject.claim.action_id))

    assert result.outcome is ActionExecutionOutcome.RETRY
    assert "release" in [name for name, _ in repository.calls]
    assert "boundary" not in [name for name, _ in repository.calls]


@pytest.mark.asyncio
async def test_authorization_revocation_marks_action_unavailable() -> None:
    subject = _subject(GmailActionCapability.CREATE_DRAFT)
    executor, repository = _executor(subject, Client(error=AuthorizationRevoked("revoked")))

    result = await executor.execute(ActionTaskRequest(action_id=subject.claim.action_id))

    assert result.outcome is ActionExecutionOutcome.UNAVAILABLE
    assert "unavailable" in [name for name, _ in repository.calls]
    assert "unknown" not in [name for name, _ in repository.calls]


@pytest.mark.asyncio
async def test_replay_after_provider_boundary_is_unknown_without_gmail_call() -> None:
    subject = _subject(GmailActionCapability.SEND_DRAFT)
    client = Client()
    executor, repository = _executor(
        subject,
        client,
        claim_outcome=ActionClaimOutcome.UNKNOWN,
    )

    result = await executor.execute(ActionTaskRequest(action_id=subject.claim.action_id))

    assert result.outcome is ActionExecutionOutcome.UNKNOWN
    assert client.calls == []
    assert [name for name, _ in repository.calls] == ["claim"]


@pytest.mark.asyncio
async def test_terminal_task_replay_returns_without_gmail_call() -> None:
    subject = _subject(GmailActionCapability.CREATE_DRAFT)
    client = Client()
    executor, _ = _executor(subject, client, claim_outcome=ActionClaimOutcome.TERMINAL)

    result = await executor.execute(ActionTaskRequest(action_id=subject.claim.action_id))

    assert result.outcome is ActionExecutionOutcome.TERMINAL
    assert client.calls == []
