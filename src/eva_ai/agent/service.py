import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from eva_ai.agent.contracts import AgentInvocationResult, InvestigationAgent
from eva_ai.agent.errors import AgentPermanentError, AgentScopeError, AgentTransientError
from eva_ai.agent.gmail import ScopedGmailInvestigationReader
from eva_ai.agent.repository import AgentRunClaim, AgentRunRepository
from eva_ai.agent.types import (
    AgentEventContext,
    AgentInvocationRequest,
    AgentRunRecord,
    AgentRunRequestedMessage,
    AgentRunStatus,
    InvestigationOutcome,
)
from eva_ai.connectors.gmail.contracts import (
    AuthorizationRevoked,
    CredentialStore,
    GmailClientFactory,
    use_gmail_client,
)
from eva_ai.integrations.gcp.secret_manager import SecretManagerProviderError
from eva_ai.integrations.gmail.api import (
    GmailProviderError,
    InvalidAuthorizedUserCredentials,
)
from eva_ai.memory.context import MemoryContextBuilder
from eva_ai.memory.errors import MemoryNotFoundError
from eva_ai.memory.types import AgentWorkingContext


class AgentInvestigationService:
    def __init__(
        self,
        *,
        runs: AgentRunRepository,
        context_builder: MemoryContextBuilder,
        credential_store: CredentialStore,
        gmail_clients: GmailClientFactory,
        agent: InvestigationAgent,
        lease_seconds: int,
        max_attempts: int,
        retry_initial_backoff_seconds: float,
        retry_max_backoff_seconds: float,
        thread_message_limit: int,
        search_result_limit: int,
        body_max_chars: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._runs = runs
        self._context_builder = context_builder
        self._credential_store = credential_store
        self._gmail_clients = gmail_clients
        self._agent = agent
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._retry_initial_backoff_seconds = retry_initial_backoff_seconds
        self._retry_max_backoff_seconds = retry_max_backoff_seconds
        self._thread_message_limit = thread_message_limit
        self._search_result_limit = search_result_limit
        self._body_max_chars = body_max_chars
        self._clock = clock

    async def process(self, message: AgentRunRequestedMessage) -> InvestigationOutcome:
        now = self._clock()
        claim = await self._runs.claim(message, now=now, lease_seconds=self._lease_seconds)
        if claim is None:
            run = await self._runs.get(
                run_id=message.agent_run_id,
                user_id=message.user_id,
                workspace_id=message.workspace_id,
            )
            if run is None or run.status in {
                AgentRunStatus.SUCCEEDED,
                AgentRunStatus.PERMANENT_FAILURE,
            }:
                return InvestigationOutcome.TERMINAL
            return InvestigationOutcome.DEFERRED

        try:
            subject = await self._runs.load_subject(claim, self._body_max_chars)
            if not subject.signal_is_current or subject.signal_disposition.value != "INVESTIGATE":
                await self._record_failure(
                    claim,
                    retryable=False,
                    code="OBSOLETE_SIGNAL",
                    summary="The triggering Signal is no longer current for investigation.",
                )
                return InvestigationOutcome.TERMINAL
            context = await self._context_builder.build_for_situation(
                user_id=claim.user_id,
                workspace_id=claim.workspace_id,
                situation_id=subject.run.situation_id,
                focus="\n".join(
                    value for value in (subject.event.subject, subject.event.snippet) if value
                ),
            )
            digest = _input_digest(
                run_id=subject.run.id,
                signal_id=subject.run.signal_id,
                event=subject.event,
                context=context,
            )
            request = AgentInvocationRequest(
                agent_run_id=subject.run.id,
                signal_id=subject.run.signal_id,
                event=subject.event,
                context=context,
                input_digest=digest,
            )
            grant = await self._credential_store.get(subject.secret_reference)
            gmail_client = await self._gmail_clients.create(grant)

            async def investigate() -> AgentInvocationResult:
                reader = ScopedGmailInvestigationReader(
                    gmail_client,
                    connector_id=subject.connector_id,
                    user_id=claim.user_id,
                    workspace_id=claim.workspace_id,
                    thread_id=subject.gmail_thread_id,
                    thread_message_limit=self._thread_message_limit,
                    search_result_limit=self._search_result_limit,
                    body_max_chars=self._body_max_chars,
                )
                return await self._agent.investigate(request, reader)

            invocation = await use_gmail_client(gmail_client, investigate)
            await self._runs.complete(
                claim,
                result=invocation.result,
                input_digest=digest,
                provider_response_id=invocation.provider_response_id,
                usage=invocation.usage,
                tool_audit=invocation.tool_audit,
                completed_at=self._clock(),
            )
            return InvestigationOutcome.SUCCEEDED
        except AuthorizationRevoked, InvalidAuthorizedUserCredentials, AgentPermanentError:
            await self._record_failure(
                claim,
                retryable=False,
                code="PERMANENT_INVESTIGATION_FAILURE",
                summary="The investigation cannot continue without corrected configuration.",
            )
            return InvestigationOutcome.TERMINAL
        except AgentScopeError, MemoryNotFoundError:
            await self._record_failure(
                claim,
                retryable=False,
                code="INVALID_SCOPE",
                summary="The investigation scope is invalid or no longer available.",
            )
            return InvestigationOutcome.TERMINAL
        except (
            AgentTransientError,
            GmailProviderError,
            SecretManagerProviderError,
        ):
            return await self._retry(claim, "PROVIDER_UNAVAILABLE")
        except Exception:
            # Unknown failures are retried with content-free metadata; raw exception text may
            # include provider or email content and is intentionally never persisted.
            return await self._retry(claim, "UNEXPECTED_FAILURE")

    async def _retry(self, claim: AgentRunClaim, code: str) -> InvestigationOutcome:
        delay = min(
            self._retry_max_backoff_seconds,
            self._retry_initial_backoff_seconds * (2 ** max(0, claim.attempt_count - 1)),
        )
        record = await self._record_failure(
            claim,
            retryable=True,
            code=code,
            summary="The investigation failed transiently and may be retried.",
            next_retry_at=self._clock() + timedelta(seconds=delay),
        )
        return (
            InvestigationOutcome.RETRY
            if record.status is AgentRunStatus.RETRYABLE_FAILURE
            else InvestigationOutcome.TERMINAL
        )

    async def _record_failure(
        self,
        claim: AgentRunClaim,
        *,
        retryable: bool,
        code: str,
        summary: str,
        next_retry_at: datetime | None = None,
    ) -> AgentRunRecord:
        return await self._runs.fail(
            claim,
            retryable=retryable,
            max_attempts=self._max_attempts,
            next_retry_at=next_retry_at,
            failure_code=code,
            failure_summary=summary,
            completed_at=self._clock(),
        )


def _input_digest(
    *,
    run_id: object,
    signal_id: object,
    event: AgentEventContext,
    context: AgentWorkingContext,
) -> str:
    value = {
        "run_id": str(run_id),
        "signal_id": str(signal_id),
        "event": event.model_dump(mode="json"),
        "context": context.model_dump(mode="json"),
    }
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
