import json

from eva_ai.connectors.gmail.contracts import GmailNotification, InvalidNotification

_INVALID_NOTIFICATION_MESSAGE = "invalid Gmail notification"


def decode_notification(data: bytes) -> GmailNotification:
    """Decode already-decoded Pub/Sub notification bytes from Gmail."""
    decoded = _parse_json(data)
    if not isinstance(decoded, dict):
        raise InvalidNotification(_INVALID_NOTIFICATION_MESSAGE)

    email_address = decoded.get("emailAddress")
    history_id = decoded.get("historyId")
    normalized_history_id = _normalize_history_id(history_id)
    if (
        not isinstance(email_address, str)
        or not email_address.strip()
        or normalized_history_id is None
    ):
        raise InvalidNotification(_INVALID_NOTIFICATION_MESSAGE)

    return GmailNotification(
        email_address=email_address.strip().lower(), history_id=normalized_history_id
    )


def _normalize_history_id(value: object) -> str | None:
    if isinstance(value, str):
        return value if _is_ascii_decimal(value) else None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    return None


def _is_ascii_decimal(value: str) -> bool:
    return bool(value) and value.isascii() and value.isdecimal()


def _parse_json(data: bytes) -> object | None:
    try:
        decoded: object = json.loads(data)
        return decoded
    except UnicodeDecodeError, json.JSONDecodeError:
        return None
