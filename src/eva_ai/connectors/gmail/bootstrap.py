from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from eva_ai.connectors.gmail.contracts import (
    AuthorizationRevoked,
    CredentialStore,
    GmailClientFactory,
    OAuthAuthorizer,
    WatchResult,
)
from eva_ai.connectors.repository import ConnectorRepository
from eva_ai.connectors.types import ConnectorRecord, ConnectorStatus
from eva_ai.integrations.gmail.oauth import GMAIL_READONLY_SCOPE

_GMAIL_SCOPES = (GMAIL_READONLY_SCOPE,)
_WATCH_RENEWAL_INTERVAL = timedelta(hours=24)
_SAFETY_SYNC_INTERVAL = timedelta(minutes=60)


@dataclass(frozen=True, slots=True)
class ConnectGmail:
    user_id: UUID
    workspace_id: UUID
    expected_identity: str
    client_file: Path
    topic_name: str


class AccountIdentityMismatch(ValueError):
    pass


class GmailBootstrapService:
    def __init__(
        self,
        repository: ConnectorRepository,
        authorizer: OAuthAuthorizer,
        credential_store: CredentialStore,
        client_factory: GmailClientFactory,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._authorizer = authorizer
        self._credential_store = credential_store
        self._client_factory = client_factory
        self._clock = clock

    async def connect(self, command: ConnectGmail) -> ConnectorRecord:
        grant = await self._authorizer.authorize(command.client_file, _GMAIL_SCOPES)
        gmail = await self._client_factory.create(grant.authorized_user_json)
        actual_identity = (await gmail.get_profile()).lower()
        if actual_identity != command.expected_identity.lower():
            raise AccountIdentityMismatch("authorized Gmail account does not match configuration")

        now = self._clock()
        connector = await self._repository.reserve_gmail(
            command.user_id,
            command.workspace_id,
            actual_identity,
            _GMAIL_SCOPES,
            now,
        )
        if connector.status == ConnectorStatus.DISABLED:
            return connector
        try:
            secret_reference = await self._credential_store.put(
                connector.id, grant.authorized_user_json
            )
            await self._repository.attach_secret(connector.id, secret_reference)
            watch = await gmail.watch(command.topic_name)
            return await self._repository.activate_initial_watch(
                connector.id,
                watch,
                now,
                _renewal_due_at(watch, now),
                now + _SAFETY_SYNC_INTERVAL,
            )
        except Exception as error:
            await self._persist_failure_state(connector.id, error)
            raise

    async def _persist_failure_state(self, connector_id: UUID, error: Exception) -> None:
        # State persistence is best effort; the original sanitized provider error must win.
        try:
            if isinstance(error, AuthorizationRevoked):
                await self._repository.mark_reauthorization_required(connector_id, error)
            else:
                await self._repository.mark_error(connector_id, error)
        except Exception:
            error.add_note("connector failure state could not be persisted")


def _renewal_due_at(watch: WatchResult, now: datetime) -> datetime:
    return min(
        now + _WATCH_RENEWAL_INTERVAL,
        watch.expiration - _WATCH_RENEWAL_INTERVAL,
    )
