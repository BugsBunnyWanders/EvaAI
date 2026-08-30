import asyncio
import json
import logging
import threading
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
from httplib2 import Response  # type: ignore[import-untyped]

from eva_ai.connectors.gmail.contracts import AuthorizationRevoked, HistoryCursorExpired
from eva_ai.integrations.gmail.api import (
    GmailProviderError,
    GoogleGmailClient,
    GoogleGmailClientFactory,
    InvalidAuthorizedUserCredentials,
)


class ExecutableRequest:
    def __init__(
        self,
        result: Mapping[str, object] | None,
        main_thread_id: int,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._main_thread_id = main_thread_id
        self._error = error

    def execute(self) -> Mapping[str, object]:
        assert threading.get_ident() != self._main_thread_id
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class GmailResources:
    def __init__(self, main_thread_id: int) -> None:
        self.main_thread_id = main_thread_id
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.results: dict[str, Mapping[str, object]] = {}
        self.errors: dict[str, Exception] = {}
        self.close_calls = 0
        self.close_failures: list[BaseException] = []

    def users(self) -> GmailResources:
        return self

    def history(self) -> GmailResources:
        return self

    def messages(self) -> GmailResources:
        return self

    def _request(self, operation: str, kwargs: dict[str, object]) -> ExecutableRequest:
        assert threading.get_ident() != self.main_thread_id
        self.calls.append((operation, kwargs))
        return ExecutableRequest(
            self.results.get(operation), self.main_thread_id, self.errors.get(operation)
        )

    def getProfile(self, **kwargs: object) -> ExecutableRequest:  # noqa: N802
        return self._request("get_profile", kwargs)

    def watch(self, **kwargs: object) -> ExecutableRequest:
        return self._request("watch", kwargs)

    def list(self, **kwargs: object) -> ExecutableRequest:
        operation = "list_history" if "startHistoryId" in kwargs else "list_messages"
        return self._request(operation, kwargs)

    def get(self, **kwargs: object) -> ExecutableRequest:
        return self._request("get_message", kwargs)

    def close(self) -> None:
        assert threading.get_ident() != self.main_thread_id
        self.close_calls += 1
        if self.close_failures:
            raise self.close_failures.pop(0)


@pytest.mark.asyncio
async def test_gmail_client_emits_exact_requests_and_converts_provider_responses() -> None:
    """Fails on a wrong Gmail endpoint shape, blocking call, or DTO conversion."""
    service = GmailResources(threading.get_ident())
    service.results = {
        "get_profile": {"emailAddress": "owner@example.com", "messagesTotal": 3},
        "watch": {"historyId": "812", "expiration": "1788105600000"},
        "list_history": {
            "history": [
                {
                    "id": "813",
                    "messages": [{"id": "ignored-summary", "threadId": "thread-0"}],
                    "messagesAdded": [
                        {"message": {"id": "message-1", "threadId": "thread-1"}},
                        {"message": {"id": "message-2", "threadId": "thread-2"}},
                    ],
                }
            ],
            "historyId": "814",
            "nextPageToken": "history-page-2",
        },
        "get_message": {
            "id": "message-1",
            "threadId": "thread-1",
            "labelIds": ["INBOX"],
            "snippet": "private message content",
            "internalDate": "1788105600000",
            "payload": {"mimeType": "text/plain", "headers": [], "body": {"size": 0}},
            "sizeEstimate": 42,
            "historyId": "814",
        },
        "list_messages": {
            "messages": [
                {"id": "message-3", "threadId": "thread-3"},
                {"id": "message-4", "threadId": "thread-4"},
            ],
            "nextPageToken": "message-page-2",
            "resultSizeEstimate": 2,
        },
    }
    client = GoogleGmailClient(service)

    profile = await client.get_profile()
    watch = await client.watch("projects/evaai-507018/topics/eva-gmail-notifications")
    history = await client.list_history("812", "history-page-1")
    message = await client.get_message("message-1")
    listed = await client.list_message_ids("after:1788105600 label:inbox", "message-page-1")

    assert profile == "owner@example.com"
    assert watch.history_id == "812"
    assert watch.expiration == datetime(2026, 8, 30, 16, 0, tzinfo=UTC)
    assert history.message_ids == ("message-1", "message-2")
    assert history.history_id == "814"
    assert history.next_page_token == "history-page-2"
    assert message == service.results["get_message"]
    assert listed.message_ids == ("message-3", "message-4")
    assert listed.next_page_token == "message-page-2"
    assert service.calls == [
        ("get_profile", {"userId": "me"}),
        (
            "watch",
            {
                "userId": "me",
                "body": {
                    "labelIds": ["INBOX"],
                    "labelFilterBehavior": "INCLUDE",
                    "topicName": "projects/evaai-507018/topics/eva-gmail-notifications",
                },
            },
        ),
        (
            "list_history",
            {
                "userId": "me",
                "startHistoryId": "812",
                "historyTypes": ["messageAdded"],
                "pageToken": "history-page-1",
            },
        ),
        ("get_message", {"userId": "me", "id": "message-1", "format": "full"}),
        (
            "list_messages",
            {
                "userId": "me",
                "q": "after:1788105600 label:inbox",
                "pageToken": "message-page-1",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_gmail_factory_constructs_credentials_and_service_off_the_event_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fails if credential parsing/service discovery blocks asyncio or credentials are logged."""
    main_thread_id = threading.get_ident()
    credential_json = '{"refresh_token":"factory-secret","client_id":"client-id"}'
    credentials_sentinel = object()
    service = GmailResources(main_thread_id)
    credential_calls: list[tuple[dict[str, object], tuple[str, ...]]] = []
    build_calls: list[tuple[str, str, object, bool]] = []

    def credentials_factory(info: dict[str, object], scopes: tuple[str, ...]) -> object:
        assert threading.get_ident() != main_thread_id
        credential_calls.append((info, scopes))
        return credentials_sentinel

    def build_service(
        api: str, version: str, *, credentials: object, cache_discovery: bool
    ) -> GmailResources:
        assert threading.get_ident() != main_thread_id
        build_calls.append((api, version, credentials, cache_discovery))
        return service

    caplog.set_level(logging.DEBUG)
    client = await GoogleGmailClientFactory(
        credentials_factory=credentials_factory,
        build_service=build_service,
    ).create(credential_json)

    assert isinstance(client, GoogleGmailClient)
    assert credential_calls == [
        (json.loads(credential_json), ("https://www.googleapis.com/auth/gmail.readonly",))
    ]
    assert build_calls == [("gmail", "v1", credentials_sentinel, False)]
    assert "factory-secret" not in caplog.text


@pytest.mark.parametrize(
    ("close_failure", "expected_final_close_calls"),
    [
        pytest.param(None, 1, id="close-success"),
        pytest.param(
            RuntimeError("private-construction-close-marker"),
            2,
            id="ordinary-close-failure-retried",
        ),
        pytest.param(
            asyncio.CancelledError("private-construction-close-interruption"),
            2,
            id="close-interruption-retried",
        ),
    ],
)
async def test_cancelled_factory_construction_never_orphans_completed_service(
    close_failure: BaseException | None,
    expected_final_close_calls: int,
) -> None:
    """Fails if cancellation discards an off-thread service before ownership or cleanup."""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    service = GmailResources(threading.get_ident())
    if close_failure is not None:
        service.close_failures = [close_failure]

    def build_service(*_: object, **__: object) -> GmailResources:
        started.set()
        try:
            assert release.wait(timeout=2)
            return service
        finally:
            finished.set()

    factory = GoogleGmailClientFactory(
        credentials_factory=lambda *_: object(),
        build_service=build_service,
    )
    creation = asyncio.create_task(factory.create("{}"))
    assert await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=2)
    cancellation_marker = object()
    creation.cancel(cancellation_marker)
    release.set()

    with pytest.raises(asyncio.CancelledError) as raised:
        await creation

    assert await asyncio.wait_for(asyncio.to_thread(finished.wait), timeout=2)
    assert raised.value.args == (cancellation_marker,)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "private-construction-close" not in repr(raised.value)
    assert service.close_calls == 1

    await factory.close()

    assert service.close_calls == expected_final_close_calls


@pytest.mark.asyncio
async def test_gmail_factory_closes_every_created_client_once_off_event_loop() -> None:
    """Fails if command cleanup leaks Gmail HTTP clients or closes one twice."""
    main_thread_id = threading.get_ident()
    services = [GmailResources(main_thread_id), GmailResources(main_thread_id)]
    remaining = list(services)

    def build_service(*_: object, **__: object) -> GmailResources:
        return remaining.pop(0)

    factory = GoogleGmailClientFactory(
        credentials_factory=lambda *_: object(),
        build_service=build_service,
    )
    first = await factory.create("{}")
    second = await factory.create("{}")

    await factory.close()
    await factory.close()

    assert isinstance(first, GoogleGmailClient)
    assert isinstance(second, GoogleGmailClient)
    assert [service.close_calls for service in services] == [1, 1]


@pytest.mark.asyncio
async def test_gmail_client_retries_close_after_underlying_failure() -> None:
    """Fails if a failed close permanently marks the client closed."""
    service = GmailResources(threading.get_ident())
    service.close_failures = [RuntimeError("private-provider-close-marker")]
    client = GoogleGmailClient(service)

    with pytest.raises(RuntimeError, match="private-provider-close-marker"):
        await client.close()
    await client.close()

    assert service.close_calls == 2


@pytest.mark.asyncio
async def test_factory_close_attempts_all_clients_and_retries_only_failed_ones() -> None:
    """Fails if one close failure skips later clients or loses retry ownership."""
    services = [
        GmailResources(threading.get_ident()),
        GmailResources(threading.get_ident()),
        GmailResources(threading.get_ident()),
    ]
    services[0].close_failures = [RuntimeError("private-first-close-marker")]
    remaining = list(services)
    factory = GoogleGmailClientFactory(
        credentials_factory=lambda *_: object(),
        build_service=lambda *_args, **_kwargs: remaining.pop(0),
    )
    for _ in services:
        await factory.create("{}")

    with pytest.raises(GmailProviderError) as raised:
        await factory.close()

    assert str(raised.value) == "Gmail client cleanup failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "private-first-close-marker" not in repr(raised.value)
    assert [service.close_calls for service in services] == [1, 1, 1]

    await factory.close()

    assert [service.close_calls for service in services] == [2, 1, 1]


@pytest.mark.asyncio
async def test_factory_close_cancellation_wins_after_every_client_is_attempted() -> None:
    """Fails if aggregate cleanup masks cancellation or skips ordinary/healthy clients."""
    services = [
        GmailResources(threading.get_ident()),
        GmailResources(threading.get_ident()),
        GmailResources(threading.get_ident()),
    ]
    cancellation = asyncio.CancelledError("private-cancelled-close-marker")
    services[0].close_failures = [cancellation]
    services[1].close_failures = [RuntimeError("private-ordinary-close-marker")]
    remaining = list(services)
    factory = GoogleGmailClientFactory(
        credentials_factory=lambda *_: object(),
        build_service=lambda *_args, **_kwargs: remaining.pop(0),
    )
    for _ in services:
        await factory.create("{}")

    with pytest.raises(asyncio.CancelledError) as raised:
        await factory.close()

    assert raised.value is cancellation
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert [service.close_calls for service in services] == [1, 1, 1]

    await factory.close()

    assert [service.close_calls for service in services] == [2, 2, 1]


@pytest.mark.asyncio
async def test_gmail_factory_rejects_malformed_credentials_without_exposing_them() -> None:
    """Fails if malformed authorized-user JSON survives in an exception or its cause."""
    credential_json = '{"refresh_token":"private-provider-body",'

    with pytest.raises(InvalidAuthorizedUserCredentials) as raised:
        await GoogleGmailClientFactory().create(credential_json)

    assert str(raised.value) == "authorized-user credentials are invalid"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def http_error(status: int, private_body: bytes) -> HttpError:
    return HttpError(Response({"status": str(status), "reason": "provider failure"}), private_body)


@pytest.mark.asyncio
async def test_history_404_maps_to_content_free_cursor_expiry() -> None:
    """Fails if expired cursors remain transient or leak the provider response body."""
    service = GmailResources(threading.get_ident())
    service.errors["list_history"] = http_error(404, b'{"error":"private-provider-body"}')

    with pytest.raises(HistoryCursorExpired) as raised:
        await GoogleGmailClient(service).list_history("expired", None)

    assert str(raised.value) == "Gmail history cursor expired"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
async def test_invalid_grant_maps_to_content_free_authorization_revoked() -> None:
    """Fails if refresh-token revocation is retried or provider details escape."""
    service = GmailResources(threading.get_ident())
    service.errors["get_profile"] = RefreshError(  # type: ignore[no-untyped-call]
        "invalid_grant: private-provider-body", response={"private": "body"}
    )

    with pytest.raises(AuthorizationRevoked) as raised:
        await GoogleGmailClient(service).get_profile()

    assert str(raised.value) == "Google authorization has been revoked"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
async def test_other_http_errors_remain_retryable_without_provider_body() -> None:
    """Fails if transient Gmail errors become permanent or expose provider content."""
    service = GmailResources(threading.get_ident())
    service.errors["get_message"] = http_error(503, b'{"error":"private-provider-body"}')

    with pytest.raises(GmailProviderError) as raised:
        await GoogleGmailClient(service).get_message("message-1")

    assert str(raised.value) == "Gmail API request failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
