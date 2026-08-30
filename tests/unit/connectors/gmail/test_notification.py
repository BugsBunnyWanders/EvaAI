import pytest

from eva_ai.connectors.gmail.contracts import InvalidNotification
from eva_ai.connectors.gmail.notification import decode_notification


def test_decode_notification_uses_client_decoded_bytes() -> None:
    """Fails if the decoder base64-decodes Pub/Sub client output again."""
    notification = decode_notification(
        b'{"emailAddress":"SaswatRay2505@gmail.com","historyId":"12345"}'
    )

    assert notification.email_address == "saswatray2505@gmail.com"
    assert notification.history_id == "12345"


def test_decode_notification_normalizes_live_gmail_numeric_history_id() -> None:
    """Accepts the JSON integer representation emitted by live Gmail delivery."""
    notification = decode_notification(b'{"emailAddress":"mail@example.com","historyId":12345}')

    assert notification.history_id == "12345"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not-json",
        b"[]",
        b'{"historyId":"1"}',
        b'{"emailAddress":"mail@example.com","historyId":true}',
        b'{"emailAddress":"mail@example.com","historyId":-1}',
        b'{"emailAddress":"mail@example.com","historyId":1.5}',
        b'{"emailAddress":"mail@example.com","historyId":"one"}',
    ],
)
def test_decode_notification_rejects_malformed_data(data: bytes) -> None:
    """Fails if malformed notification shapes reach synchronization."""
    with pytest.raises(InvalidNotification, match="^invalid Gmail notification$"):
        decode_notification(data)


def test_decode_notification_does_not_retain_malformed_json_content_in_exception_chain() -> None:
    """Fails if parser errors retain raw Pub/Sub data after rejection."""
    token = b"notification-token-should-not-survive"

    with pytest.raises(InvalidNotification) as caught:
        decode_notification(b'{"emailAddress":"' + token)

    errors = _exception_chain(caught.value)
    assert errors == [caught.value]
    assert all(token.decode() not in repr(error) for error in errors)


def _exception_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return chain
