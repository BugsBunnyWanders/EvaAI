import asyncio
import json
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum, auto
from typing import Protocol, cast

import google_auth_httplib2  # type: ignore[import-untyped]
import httplib2  # type: ignore[import-untyped]
from google.auth.credentials import Credentials as GoogleCredentials
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build  # type: ignore[import-untyped]
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

from eva_ai.connectors.gmail.contracts import (
    AuthorizationRevoked,
    GmailClient,
    GmailProfile,
    HistoryCursorExpired,
    HistoryPage,
    MessageListPage,
    WatchResult,
)
from eva_ai.integrations.gmail.oauth import GMAIL_READONLY_SCOPE

_GMAIL_SCOPES = (GMAIL_READONLY_SCOPE,)
_TRANSIENT_HTTP_STATUSES = frozenset({408, 429})
_TRANSIENT_HTTP_403_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})


class GmailProviderError(RuntimeError):
    """Retryable Gmail failure with provider-controlled content removed."""


class InvalidAuthorizedUserCredentials(ValueError):
    """Authorized-user credential input is missing or malformed."""


class _ClientCreationFailure(Enum):
    INVALID_CREDENTIALS = auto()
    PROVIDER = auto()


class _RequestFailure(Enum):
    AUTHORIZATION_REVOKED = auto()
    AUTHORIZATION_REFRESH = auto()
    HISTORY_EXPIRED = auto()
    TRANSIENT = auto()
    PROVIDER = auto()


class ExecutableRequest(Protocol):
    def execute(self) -> Mapping[str, object]: ...


class HistoryResource(Protocol):
    def list(self, **kwargs: object) -> ExecutableRequest: ...


class MessagesResource(Protocol):
    def get(self, **kwargs: object) -> ExecutableRequest: ...

    def list(self, **kwargs: object) -> ExecutableRequest: ...


class UsersResource(Protocol):
    def getProfile(self, **kwargs: object) -> ExecutableRequest: ...  # noqa: N802

    def watch(self, **kwargs: object) -> ExecutableRequest: ...

    def history(self) -> HistoryResource: ...

    def messages(self) -> MessagesResource: ...


class GmailService(Protocol):
    def users(self) -> UsersResource: ...

    def close(self) -> None: ...


class BuildService(Protocol):
    def __call__(
        self,
        api: str,
        version: str,
        *,
        http: object,
        cache_discovery: bool,
    ) -> object: ...


CredentialsFactory = Callable[[dict[str, object], tuple[str, ...]], object]
HttpTransportFactory = Callable[[object, float], object]


def _default_credentials_factory(info: dict[str, object], scopes: tuple[str, ...]) -> object:
    return Credentials.from_authorized_user_info(  # type: ignore[no-untyped-call]
        info, scopes=scopes
    )


def _default_build_service(
    api: str,
    version: str,
    *,
    http: object,
    cache_discovery: bool,
) -> object:
    return build(
        api,
        version,
        http=http,
        cache_discovery=cache_discovery,
    )


def _default_http_transport_factory(credentials: object, timeout_seconds: float) -> object:
    return google_auth_httplib2.AuthorizedHttp(
        cast(GoogleCredentials, credentials),
        http=httplib2.Http(timeout=timeout_seconds),
    )


class GoogleGmailClientFactory:
    def __init__(
        self,
        credentials_factory: CredentialsFactory = _default_credentials_factory,
        build_service: BuildService = _default_build_service,
        request_timeout_seconds: float = 30.0,
        http_transport_factory: HttpTransportFactory = _default_http_transport_factory,
        retry_attempts: int = 3,
        retry_initial_backoff_seconds: float = 0.5,
        retry_max_backoff_seconds: float = 8.0,
        retry_jitter_ratio: float = 0.2,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        randomness: Callable[[], float] = random.random,
    ) -> None:
        self._credentials_factory = credentials_factory
        self._build_service = build_service
        self._request_timeout_seconds = request_timeout_seconds
        self._http_transport_factory = http_transport_factory
        self._retry_attempts = retry_attempts
        self._retry_initial_backoff_seconds = retry_initial_backoff_seconds
        self._retry_max_backoff_seconds = retry_max_backoff_seconds
        self._retry_jitter_ratio = retry_jitter_ratio
        self._sleep = sleep
        self._randomness = randomness
        self._clients: list[GoogleGmailClient] = []

    async def create(self, authorized_user_json: str) -> GmailClient:
        def create_sync() -> GoogleGmailClient | _ClientCreationFailure:
            try:
                decoded = json.loads(authorized_user_json)
            except json.JSONDecodeError, TypeError:
                return _ClientCreationFailure.INVALID_CREDENTIALS
            if not isinstance(decoded, dict):
                return _ClientCreationFailure.INVALID_CREDENTIALS
            info = cast(dict[str, object], decoded)
            try:
                credentials = self._credentials_factory(info, _GMAIL_SCOPES)
            except ValueError:
                return _ClientCreationFailure.INVALID_CREDENTIALS
            try:
                http = self._http_transport_factory(
                    credentials,
                    self._request_timeout_seconds,
                )
                service = self._build_service(
                    "gmail",
                    "v1",
                    http=http,
                    cache_discovery=False,
                )
            except HttpError:
                return _ClientCreationFailure.PROVIDER
            return GoogleGmailClient(
                service,
                self._release,
                retry_attempts=self._retry_attempts,
                retry_initial_backoff_seconds=self._retry_initial_backoff_seconds,
                retry_max_backoff_seconds=self._retry_max_backoff_seconds,
                retry_jitter_ratio=self._retry_jitter_ratio,
                sleep=self._sleep,
                randomness=self._randomness,
            )

        async def own_construction() -> GoogleGmailClient | _ClientCreationFailure:
            try:
                return await asyncio.to_thread(create_sync)
            except BaseException:
                return _ClientCreationFailure.PROVIDER

        construction_owner = asyncio.create_task(own_construction())
        cancelled: asyncio.CancelledError | None = None
        while True:
            try:
                result = await asyncio.shield(construction_owner)
                break
            except asyncio.CancelledError as error:
                if cancelled is None:
                    cancelled = error

        if cancelled is not None:
            if isinstance(result, GoogleGmailClient):
                self._clients.append(result)
                try:
                    await result.close()
                except BaseException:
                    # Failed cancellation cleanup stays factory-owned for aggregate retry.
                    pass
            raise cancelled
        if result is _ClientCreationFailure.INVALID_CREDENTIALS:
            raise InvalidAuthorizedUserCredentials("authorized-user credentials are invalid")
        if result is _ClientCreationFailure.PROVIDER:
            raise GmailProviderError("Gmail client construction failed")
        self._clients.append(result)
        return result

    async def close(self) -> None:
        interruption: BaseException | None = None
        ordinary_failure = False
        for client in tuple(self._clients):
            try:
                await client.close()
            except asyncio.CancelledError as error:
                if interruption is None:
                    interruption = error
            except Exception:
                ordinary_failure = True
            except BaseException as error:
                if interruption is None:
                    interruption = error
        if interruption is not None:
            raise interruption
        if ordinary_failure:
            raise GmailProviderError("Gmail client cleanup failed")

    def _release(self, client: GoogleGmailClient) -> None:
        try:
            self._clients.remove(client)
        except ValueError:
            pass


class GoogleGmailClient:
    def __init__(
        self,
        service: object,
        on_closed: Callable[[GoogleGmailClient], None] | None = None,
        *,
        retry_attempts: int = 3,
        retry_initial_backoff_seconds: float = 0.5,
        retry_max_backoff_seconds: float = 8.0,
        retry_jitter_ratio: float = 0.2,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        randomness: Callable[[], float] = random.random,
    ) -> None:
        self._service = cast(GmailService, service)
        self._on_closed = on_closed
        self._closed = False
        self._retry_attempts = retry_attempts
        self._retry_initial_backoff_seconds = retry_initial_backoff_seconds
        self._retry_max_backoff_seconds = retry_max_backoff_seconds
        self._retry_jitter_ratio = retry_jitter_ratio
        self._sleep = sleep
        self._randomness = randomness

    async def close(self) -> None:
        if self._closed:
            return
        await asyncio.to_thread(self._service.close)
        self._closed = True
        if self._on_closed is not None:
            self._on_closed(self)

    async def get_profile(self) -> GmailProfile:
        response = await self._execute(
            lambda: self._service.users().getProfile(userId="me").execute()
        )
        return GmailProfile(
            email_address=_required_string(response, "emailAddress", "profile"),
            history_id=_required_string(response, "historyId", "profile"),
        )

    async def watch(self, topic_name: str) -> WatchResult:
        response = await self._execute(
            lambda: (
                self._service.users()
                .watch(
                    userId="me",
                    body={
                        "labelIds": ["INBOX"],
                        "labelFilterBehavior": "INCLUDE",
                        "topicName": topic_name,
                    },
                )
                .execute()
            )
        )
        history_id = _required_string(response, "historyId", "watch")
        expiration_millis = _required_string(response, "expiration", "watch")
        expiration = _parse_expiration(expiration_millis)
        if expiration is None:
            raise GmailProviderError("Gmail API returned an invalid watch response")
        return WatchResult(history_id=history_id, expiration=expiration)

    async def list_history(self, start_history_id: str, page_token: str | None) -> HistoryPage:
        response = await self._execute(
            lambda: (
                self._service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                )
                .execute()
            ),
            history_request=True,
        )
        return HistoryPage(
            message_ids=_history_message_ids(response.get("history")),
            history_id=_required_string(response, "historyId", "history"),
            next_page_token=_optional_string(response, "nextPageToken", "history"),
        )

    async def get_message(self, message_id: str) -> Mapping[str, object]:
        return await self._execute(
            lambda: (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        )

    async def list_message_ids(self, query: str, page_token: str | None) -> MessageListPage:
        response = await self._execute(
            lambda: (
                self._service.users()
                .messages()
                .list(userId="me", q=query, pageToken=page_token)
                .execute()
            )
        )
        return MessageListPage(
            message_ids=_message_ids(response.get("messages")),
            next_page_token=_optional_string(response, "nextPageToken", "message list"),
        )

    async def _execute(
        self,
        operation: Callable[[], Mapping[str, object]],
        *,
        history_request: bool = False,
    ) -> Mapping[str, object]:
        result: Mapping[str, object] | _RequestFailure = _RequestFailure.PROVIDER
        for attempt in range(self._retry_attempts):
            try:
                result = await asyncio.to_thread(operation)
            except RefreshError as error:
                details = " ".join(str(item) for item in error.args).lower()
                if "invalid_grant" in details:
                    result = _RequestFailure.AUTHORIZATION_REVOKED
                else:
                    result = _RequestFailure.AUTHORIZATION_REFRESH
            except HttpError as error:
                status = getattr(error.resp, "status", None)
                if history_request and status == 404:
                    result = _RequestFailure.HISTORY_EXPIRED
                elif _is_transient_http_error(error, status) or (
                    isinstance(status, int) and 500 <= status <= 599
                ):
                    result = _RequestFailure.TRANSIENT
                else:
                    result = _RequestFailure.PROVIDER
            except httplib2.HttpLib2Error, OSError, TransportError:
                result = _RequestFailure.TRANSIENT

            retryable = result is _RequestFailure.AUTHORIZATION_REFRESH or (
                result is _RequestFailure.TRANSIENT
            )
            if not retryable or attempt + 1 >= self._retry_attempts:
                break
            await self._sleep(self._retry_delay(attempt))

        if result is _RequestFailure.AUTHORIZATION_REVOKED:
            raise AuthorizationRevoked("Google authorization has been revoked")
        if result is _RequestFailure.AUTHORIZATION_REFRESH:
            raise GmailProviderError("Gmail authorization refresh failed")
        if result is _RequestFailure.HISTORY_EXPIRED:
            raise HistoryCursorExpired("Gmail history cursor expired")
        if result is _RequestFailure.TRANSIENT or result is _RequestFailure.PROVIDER:
            raise GmailProviderError("Gmail API request failed")
        return result

    def _retry_delay(self, retry_index: int) -> float:
        base = min(
            self._retry_initial_backoff_seconds * (2**retry_index),
            self._retry_max_backoff_seconds,
        )
        sample = min(1.0, max(0.0, self._randomness()))
        jitter_factor = 1.0 - self._retry_jitter_ratio + (2.0 * self._retry_jitter_ratio * sample)
        return float(min(self._retry_max_backoff_seconds, base * jitter_factor))


def _is_transient_http_error(error: HttpError, status: object) -> bool:
    if status in _TRANSIENT_HTTP_STATUSES:
        return True
    if status != 403:
        return False
    try:
        payload = json.loads(error.content)
    except json.JSONDecodeError, TypeError, UnicodeDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    error_data = payload.get("error")
    if not isinstance(error_data, dict):
        return False
    details = error_data.get("errors")
    if not isinstance(details, list):
        return False
    return any(
        isinstance(detail, dict) and detail.get("reason") in _TRANSIENT_HTTP_403_REASONS
        for detail in details
    )


def _parse_expiration(expiration_millis: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(expiration_millis) / 1000, tz=UTC)
    except OverflowError, ValueError:
        return None


def _required_string(response: Mapping[str, object], key: str, operation: str) -> str:
    value = response.get(key)
    if not isinstance(value, str) or not value:
        raise GmailProviderError(f"Gmail API returned an invalid {operation} response")
    return value


def _optional_string(response: Mapping[str, object], key: str, operation: str) -> str | None:
    value = response.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise GmailProviderError(f"Gmail API returned an invalid {operation} response")
    return value


def _history_message_ids(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise GmailProviderError("Gmail API returned an invalid history response")

    message_ids: list[str] = []
    for history_record in value:
        if not isinstance(history_record, Mapping):
            raise GmailProviderError("Gmail API returned an invalid history response")
        added = history_record.get("messagesAdded", ())
        if not isinstance(added, Sequence) or isinstance(added, (str, bytes)):
            raise GmailProviderError("Gmail API returned an invalid history response")
        for addition in added:
            if not isinstance(addition, Mapping):
                raise GmailProviderError("Gmail API returned an invalid history response")
            message = addition.get("message")
            if not isinstance(message, Mapping):
                raise GmailProviderError("Gmail API returned an invalid history response")
            message_ids.append(_required_string(message, "id", "history"))
    return tuple(message_ids)


def _message_ids(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise GmailProviderError("Gmail API returned an invalid message list response")
    ids: list[str] = []
    for message in value:
        if not isinstance(message, Mapping):
            raise GmailProviderError("Gmail API returned an invalid message list response")
        ids.append(_required_string(message, "id", "message list"))
    return tuple(ids)
