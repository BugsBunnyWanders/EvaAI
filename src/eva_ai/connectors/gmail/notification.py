import json

from eva_ai.connectors.gmail.contracts import GmailNotification, InvalidNotification

_INVALID_NOTIFICATION_MESSAGE = "invalid Gmail notification"


def decode_notification(data: bytes) -> GmailNotification:
    """Decode already-decoded Pub/Sub notification bytes from Gmail."""
    try:
        decoded = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidNotification(_INVALID_NOTIFICATION_MESSAGE) from error

    if not isinstance(decoded, dict):
        raise InvalidNotification(_INVALID_NOTIFICATION_MESSAGE)

    email_address = decoded.get("emailAddress")
    history_id = decoded.get("historyId")
    if (
        not isinstance(email_address, str)
        or not email_address.strip()
        or not isinstance(history_id, str)
        or not _is_ascii_decimal(history_id)
    ):
        raise InvalidNotification(_INVALID_NOTIFICATION_MESSAGE)

    return GmailNotification(email_address=email_address.strip().lower(), history_id=history_id)


def _is_ascii_decimal(value: str) -> bool:
    return bool(value) and value.isascii() and value.isdecimal()
