import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum, auto
from typing import Protocol, cast

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build  # type: ignore[import-untyped]
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

from eva_ai.connectors.gmail.contracts import (
    AuthorizationRevoked,
    GmailClient,
    HistoryCursorExpired,
    HistoryPage,
    MessageListPage,
    WatchResult,
)
from eva_ai.integrations.gmail.oauth import GMAIL_READONLY_SCOPE

_GMAIL_SCOPES = (GMAIL_READONLY_SCOPE,)


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


class BuildService(Protocol):
    def __call__(
        self,
        api: str,
        version: str,
        *,
        credentials: object,
        cache_discovery: bool,
    ) -> object: ...


CredentialsFactory = Callable[[dict[str, object], tuple[str, ...]], object]


def _default_credentials_factory(info: dict[str, object], scopes: tuple[str, ...]) -> object:
    return Credentials.from_authorized_user_info(  # type: ignore[no-untyped-call]
        info, scopes=scopes
    )


def _default_build_service(
    api: str,
    version: str,
    *,
    credentials: object,
    cache_discovery: bool,
) -> object:
    return build(
        api,
        version,
        credentials=credentials,
        cache_discovery=cache_discovery,
    )


class GoogleGmailClientFactory:
    def __init__(
        self,
        credentials_factory: CredentialsFactory = _default_credentials_factory,
        build_service: BuildService = _default_build_service,
    ) -> None:
        self._credentials_factory = credentials_factory
        self._build_service = build_service

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
                service = self._build_service(
                    "gmail",
                    "v1",
                    credentials=credentials,
                    cache_discovery=False,
                )
            except HttpError:
                return _ClientCreationFailure.PROVIDER
            return GoogleGmailClient(service)

        result = await asyncio.to_thread(create_sync)
        if result is _ClientCreationFailure.INVALID_CREDENTIALS:
            raise InvalidAuthorizedUserCredentials("authorized-user credentials are invalid")
        if result is _ClientCreationFailure.PROVIDER:
            raise GmailProviderError("Gmail client construction failed")
        return result


class GoogleGmailClient:
    def __init__(self, service: object) -> None:
        self._service = cast(GmailService, service)

    async def get_profile(self) -> str:
        response = await self._execute(
            lambda: self._service.users().getProfile(userId="me").execute()
        )
        email_address = response.get("emailAddress")
        if not isinstance(email_address, str) or not email_address:
            raise GmailProviderError("Gmail API returned an invalid profile")
        return email_address

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
        result: Mapping[str, object] | _RequestFailure
        try:
            result = await asyncio.to_thread(operation)
        except RefreshError as error:
            details = " ".join(str(item) for item in error.args).lower()
            if "invalid_grant" in details:
                result = _RequestFailure.AUTHORIZATION_REVOKED
            else:
                result = _RequestFailure.AUTHORIZATION_REFRESH
        except HttpError as error:
            if history_request and getattr(error.resp, "status", None) == 404:
                result = _RequestFailure.HISTORY_EXPIRED
            else:
                result = _RequestFailure.PROVIDER

        if result is _RequestFailure.AUTHORIZATION_REVOKED:
            raise AuthorizationRevoked("Google authorization has been revoked")
        if result is _RequestFailure.AUTHORIZATION_REFRESH:
            raise GmailProviderError("Gmail authorization refresh failed")
        if result is _RequestFailure.HISTORY_EXPIRED:
            raise HistoryCursorExpired("Gmail history cursor expired")
        if result is _RequestFailure.PROVIDER:
            raise GmailProviderError("Gmail API request failed")
        return result


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
