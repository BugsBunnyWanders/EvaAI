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


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not-json",
        b"[]",
        b'{"historyId":"1"}',
        b'{"emailAddress":"mail@example.com","historyId":1}',
        b'{"emailAddress":"mail@example.com","historyId":"one"}',
    ],
)
def test_decode_notification_rejects_malformed_data(data: bytes) -> None:
    """Fails if malformed notification shapes reach synchronization."""
    with pytest.raises(InvalidNotification, match="^invalid Gmail notification$"):
        decode_notification(data)
