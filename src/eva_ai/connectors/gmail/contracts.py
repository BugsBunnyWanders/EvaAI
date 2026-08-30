from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class GmailNotification:
    email_address: str
    history_id: str


@dataclass(frozen=True, slots=True)
class WatchResult:
    history_id: str
    expiration: datetime


@dataclass(frozen=True, slots=True)
class HistoryPage:
    message_ids: tuple[str, ...]
    history_id: str
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class MessageListPage:
    message_ids: tuple[str, ...]
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class AuthorizedUserGrant:
    authorized_user_json: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PullMessage:
    ack_id: str
    message_id: str
    data: bytes = field(repr=False)


class InvalidNotification(ValueError):
    pass


class HistoryCursorExpired(RuntimeError):
    pass


class AuthorizationRevoked(RuntimeError):
    pass


class GmailClient(Protocol):
    async def get_profile(self) -> str: ...

    async def watch(self, topic_name: str) -> WatchResult: ...

    async def list_history(self, start_history_id: str, page_token: str | None) -> HistoryPage: ...

    async def get_message(self, message_id: str) -> Mapping[str, object]: ...

    async def list_message_ids(self, query: str, page_token: str | None) -> MessageListPage: ...


class GmailClientFactory(Protocol):
    async def create(self, authorized_user_json: str) -> GmailClient: ...


class OAuthAuthorizer(Protocol):
    async def authorize(
        self, client_file: Path, scopes: tuple[str, ...]
    ) -> AuthorizedUserGrant: ...


class CredentialStore(Protocol):
    async def put(self, connector_id: UUID, authorized_user_json: str) -> str: ...

    async def get(self, secret_reference: str) -> str: ...


class PullSubscriber(Protocol):
    async def pull(self, max_messages: int, timeout_seconds: int) -> tuple[PullMessage, ...]: ...

    async def acknowledge(self, ack_ids: tuple[str, ...]) -> None: ...

    async def negative_acknowledge(self, ack_ids: tuple[str, ...]) -> None: ...

    async def close(self) -> None: ...
