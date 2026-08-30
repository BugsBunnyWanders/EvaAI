import asyncio
import gc
import json
import logging
import threading
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp  # type: ignore[import-untyped]
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
        self.error_sequences: dict[str, list[Exception]] = {}
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
        error_sequence = self.error_sequences.get(operation, [])
        error = error_sequence.pop(0) if error_sequence else self.errors.get(operation)
        return ExecutableRequest(self.results.get(operation), self.main_thread_id, error)

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
        "get_profile": {
            "emailAddress": "owner@example.com",
            "historyId": "811",
            "messagesTotal": 3,
        },
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

    assert profile.email_address == "owner@example.com"
    assert profile.history_id == "811"
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
    http_sentinel = object()
    service = GmailResources(main_thread_id)
    credential_calls: list[tuple[dict[str, object], tuple[str, ...]]] = []
    transport_calls: list[tuple[object, float]] = []
    build_calls: list[tuple[str, str, object, bool]] = []

    def credentials_factory(info: dict[str, object], scopes: tuple[str, ...]) -> object:
        assert threading.get_ident() != main_thread_id
        credential_calls.append((info, scopes))
        return credentials_sentinel

    def http_transport_factory(credentials: object, timeout_seconds: float) -> object:
        assert threading.get_ident() != main_thread_id
        transport_calls.append((credentials, timeout_seconds))
        return http_sentinel

    def build_service(
        api: str, version: str, *, http: object, cache_discovery: bool
    ) -> GmailResources:
        assert threading.get_ident() != main_thread_id
        build_calls.append((api, version, http, cache_discovery))
        return service

    caplog.set_level(logging.DEBUG)
    client = await GoogleGmailClientFactory(
        credentials_factory=credentials_factory,
        build_service=build_service,
        http_transport_factory=http_transport_factory,
    ).create(credential_json)

    assert isinstance(client, GoogleGmailClient)
    assert credential_calls == [
        (json.loads(credential_json), ("https://www.googleapis.com/auth/gmail.readonly",))
    ]
    assert transport_calls == [(credentials_sentinel, 30.0)]
    assert build_calls == [("gmail", "v1", http_sentinel, False)]
    assert "factory-secret" not in caplog.text


@pytest.mark.asyncio
async def test_gmail_factory_applies_request_timeout_to_socket_transport() -> None:
    """Fails if the request deadline does not reach the blocking HTTP transport."""
    credentials = Credentials(  # type: ignore[no-untyped-call]
        token="synthetic-access-token",
        refresh_token="synthetic-refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="synthetic-client-id",
        client_secret="synthetic-client-secret",
    )
    service = GmailResources(threading.get_ident())
    captured_http: object | None = None

    def build_service(
        api: str,
        version: str,
        *,
        http: object,
        cache_discovery: bool,
    ) -> GmailResources:
        del api, version, cache_discovery
        nonlocal captured_http
        captured_http = http
        return service

    client = await GoogleGmailClientFactory(
        credentials_factory=lambda *_: credentials,
        build_service=build_service,
        request_timeout_seconds=7.25,
    ).create("{}")

    assert isinstance(captured_http, AuthorizedHttp)
    assert captured_http.http.timeout == 7.25
    await client.close()


@pytest.mark.asyncio
async def test_gmail_client_retries_transient_failures_with_bounded_backoff_and_jitter() -> None:
    """Fails if rate limits, server errors, or socket deadlines bypass bounded retry."""
    service = GmailResources(threading.get_ident())
    service.results["get_profile"] = {
        "emailAddress": "owner@example.com",
        "historyId": "811",
    }
    service.error_sequences["get_profile"] = [
        http_error(429, b'"private-rate-limit"'),
        http_error(503, b'"private-server-response"'),
        TimeoutError("private-socket-deadline"),
    ]
    delays: list[float] = []
    random_values = iter((0.0, 0.5, 1.0))

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = GoogleGmailClient(
        service,
        retry_attempts=4,
        retry_initial_backoff_seconds=1.0,
        retry_max_backoff_seconds=3.0,
        retry_jitter_ratio=0.25,
        sleep=record_sleep,
        randomness=lambda: next(random_values),
    )

    profile = await client.get_profile()

    assert profile.email_address == "owner@example.com"
    assert [operation for operation, _ in service.calls] == ["get_profile"] * 4
    assert delays == [0.75, 2.0, 3.0]


@pytest.mark.asyncio
async def test_gmail_client_retries_reason_coded_http_403_rate_limit() -> None:
    """Fails if Gmail's legacy 403 quota signal is treated as permanent."""
    service = GmailResources(threading.get_ident())
    service.results["get_profile"] = {
        "emailAddress": "owner@example.com",
        "historyId": "811",
    }
    service.error_sequences["get_profile"] = [
        http_error(
            403,
            b'{"error":{"errors":[{"reason":"userRateLimitExceeded"}]}}',
        )
    ]
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = GoogleGmailClient(
        service,
        retry_attempts=2,
        retry_initial_backoff_seconds=0.5,
        retry_jitter_ratio=0.0,
        sleep=record_sleep,
    )

    profile = await client.get_profile()

    assert profile.history_id == "811"
    assert [operation for operation, _ in service.calls] == ["get_profile"] * 2
    assert delays == [0.5]


@pytest.mark.asyncio
async def test_gmail_client_stops_after_retry_attempt_limit() -> None:
    """Fails if a transient network failure can retry without a fixed attempt bound."""
    service = GmailResources(threading.get_ident())
    marker = "private-network-response"
    service.errors["get_message"] = OSError(marker)
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = GoogleGmailClient(
        service,
        retry_attempts=3,
        retry_initial_backoff_seconds=0.1,
        retry_max_backoff_seconds=1.0,
        retry_jitter_ratio=0.0,
        sleep=record_sleep,
        randomness=lambda: 0.5,
    )

    with pytest.raises(GmailProviderError) as raised:
        await client.get_message("message-1")

    assert str(raised.value) == "Gmail API request failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert marker not in repr(raised.value)
    assert [operation for operation, _ in service.calls] == ["get_message"] * 3
    assert delays == [0.1, 0.2]


@pytest.mark.asyncio
async def test_gmail_client_retry_sleep_propagates_cancellation_unchanged() -> None:
    """Fails if shutdown is delayed or reclassified after a transient Gmail failure."""
    service = GmailResources(threading.get_ident())
    service.errors["get_message"] = http_error(503, b'"private-server-response"')
    cancellation = asyncio.CancelledError("retry-sleep-cancelled")

    async def cancel_sleep(delay: float) -> None:
        del delay
        raise cancellation

    client = GoogleGmailClient(service, sleep=cancel_sleep)

    with pytest.raises(asyncio.CancelledError) as raised:
        await client.get_message("message-1")

    assert raised.value is cancellation
    assert [operation for operation, _ in service.calls] == ["get_message"]


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


@pytest.mark.parametrize("cancellation_count", [2, 5])
async def test_repeated_construction_cancellation_keeps_completed_service_recoverable(
    cancellation_count: int,
) -> None:
    """Fails if a later cancellation cancels the construction owner and orphans its service."""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    service = GmailResources(threading.get_ident())

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
    cancellation_markers = [object() for _ in range(cancellation_count)]
    accepted_cancellations: list[bool] = []
    for marker in cancellation_markers:
        accepted_cancellations.append(creation.cancel(marker))
        await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError) as raised:
        await creation

    assert await asyncio.wait_for(asyncio.to_thread(finished.wait), timeout=2)
    assert accepted_cancellations == [True] * cancellation_count
    assert raised.value.args == (cancellation_markers[0],)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert service.close_calls == 1

    await factory.close()

    assert service.close_calls == 1


async def test_cancelled_construction_exception_is_consumed_without_loop_disclosure(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fails if a cancelled shield or owner reports private construction errors to the loop."""
    started = threading.Event()
    release = threading.Event()
    loop_contexts: list[dict[str, object]] = []
    marker = "private-post-cancellation-construction-marker"

    def build_service(*_: object, **__: object) -> GmailResources:
        started.set()
        assert release.wait(timeout=2)
        raise RuntimeError(marker)

    def collect_loop_error(
        _: asyncio.AbstractEventLoop,
        context: dict[str, object],
    ) -> None:
        loop_contexts.append(context)

    factory = GoogleGmailClientFactory(
        credentials_factory=lambda *_: object(),
        build_service=build_service,
    )
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(collect_loop_error)
    cancellation_marker = object()
    try:
        creation = asyncio.create_task(factory.create("{}"))
        assert await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=2)
        creation.cancel(cancellation_marker)
        release.set()

        with pytest.raises(asyncio.CancelledError) as raised:
            await creation

        for _ in range(3):
            gc.collect()
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert raised.value.args == (cancellation_marker,)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert loop_contexts == []
    captured = capsys.readouterr()
    assert marker not in captured.err
    assert marker not in caplog.text


async def test_unexpected_construction_failure_is_fixed_and_chain_free() -> None:
    """Fails if an unexpected credential/service error exposes its private marker."""
    marker = "private-normal-construction-marker"

    def build_service(*_: object, **__: object) -> GmailResources:
        raise RuntimeError(marker)

    factory = GoogleGmailClientFactory(
        credentials_factory=lambda *_: object(),
        build_service=build_service,
    )

    with pytest.raises(GmailProviderError) as raised:
        await factory.create("{}")

    assert str(raised.value) == "Gmail client construction failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert marker not in repr(raised.value)


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
    assert [operation for operation, _ in service.calls] == ["list_history"]


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
    assert [operation for operation, _ in service.calls] == ["get_profile"]


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
