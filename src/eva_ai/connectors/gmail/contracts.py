import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID


@dataclass(frozen=True, slots=True)
class GmailNotification:
    email_address: str
    history_id: str


@dataclass(frozen=True, slots=True)
class GmailProfile:
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


class GmailClientCleanupError(RuntimeError):
    """Content-free failure to close one per-attempt Gmail client."""


class GmailClient(Protocol):
    async def get_profile(self) -> GmailProfile: ...

    async def watch(self, topic_name: str) -> WatchResult: ...

    async def list_history(self, start_history_id: str, page_token: str | None) -> HistoryPage: ...

    async def get_message(self, message_id: str) -> Mapping[str, object]: ...

    async def list_message_ids(self, query: str, page_token: str | None) -> MessageListPage: ...

    async def close(self) -> None: ...


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


async def use_gmail_client[ResultT](
    client: GmailClient,
    operation: Callable[[], Awaitable[ResultT]],
) -> ResultT:
    """Run one Gmail attempt and close its client with primary-error precedence."""
    primary_failure: BaseException | None = None
    result: ResultT | None = None
    try:
        result = await operation()
    except BaseException as error:
        primary_failure = error

    cleanup_interruption: BaseException | None = None
    cleanup_failed = False
    try:
        await client.close()
    except asyncio.CancelledError as error:
        cleanup_interruption = error
    except Exception:
        cleanup_failed = True
    except BaseException as error:
        cleanup_interruption = error

    if primary_failure is not None:
        raise primary_failure
    if cleanup_interruption is not None:
        raise cleanup_interruption
    if cleanup_failed:
        raise GmailClientCleanupError("Gmail client cleanup failed")
    return cast(ResultT, result)
