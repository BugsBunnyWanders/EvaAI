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
    use_gmail_client,
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
        watch_renewal_interval: timedelta = _WATCH_RENEWAL_INTERVAL,
        safety_sync_interval: timedelta = _SAFETY_SYNC_INTERVAL,
    ) -> None:
        self._repository = repository
        self._authorizer = authorizer
        self._credential_store = credential_store
        self._client_factory = client_factory
        self._clock = clock
        self._watch_renewal_interval = watch_renewal_interval
        self._safety_sync_interval = safety_sync_interval

    async def connect(self, command: ConnectGmail) -> ConnectorRecord:
        grant = await self._authorizer.authorize(command.client_file, _GMAIL_SCOPES)
        gmail = await self._client_factory.create(grant.authorized_user_json)

        async def bootstrap() -> ConnectorRecord:
            boundary_at = self._clock()
            profile = await gmail.get_profile()
            actual_identity = profile.email_address.lower()
            if actual_identity != command.expected_identity.lower():
                raise AccountIdentityMismatch(
                    "authorized Gmail account does not match configuration"
                )

            connector = await self._repository.reserve_gmail(
                command.user_id,
                command.workspace_id,
                actual_identity,
                _GMAIL_SCOPES,
                boundary_at,
            )
            if connector.status == ConnectorStatus.DISABLED:
                return connector
            try:
                secret_reference = await self._credential_store.put(
                    connector.id, grant.authorized_user_json
                )
                await self._repository.attach_secret(connector.id, secret_reference)
                # This lower profile cursor makes a post-watch crash replayable on retry.
                await self._repository.prepare_initial_watch(
                    connector.id,
                    profile.history_id,
                    boundary_at,
                )
                watch = await gmail.watch(command.topic_name)
                activated_at = self._clock()
                return await self._repository.activate_initial_watch(
                    connector.id,
                    watch,
                    _renewal_due_at(watch, activated_at, self._watch_renewal_interval),
                    activated_at + self._safety_sync_interval,
                )
            except Exception as error:
                await self._persist_failure_state(connector.id, error)
                raise

        return await use_gmail_client(gmail, bootstrap)

    async def _persist_failure_state(self, connector_id: UUID, error: Exception) -> None:
        # State persistence is best effort; the original sanitized provider error must win.
        try:
            if isinstance(error, AuthorizationRevoked):
                await self._repository.mark_reauthorization_required(connector_id, error)
            else:
                await self._repository.mark_error(connector_id, error)
        except Exception:
            error.add_note("connector failure state could not be persisted")


def _renewal_due_at(
    watch: WatchResult,
    now: datetime,
    renewal_interval: timedelta = _WATCH_RENEWAL_INTERVAL,
) -> datetime:
    return min(
        now + renewal_interval,
        watch.expiration - renewal_interval,
    )
