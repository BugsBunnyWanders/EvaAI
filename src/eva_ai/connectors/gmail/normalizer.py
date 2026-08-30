from base64 import urlsafe_b64decode
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from email.message import Message
from email.utils import parseaddr
from typing import cast

from pydantic import JsonValue

from eva_ai.connectors.types import ConnectorRecord
from eva_ai.events.types import NewEvent, PrincipalType

_SELECTED_HEADERS = {
    "from": "from",
    "to": "to",
    "cc": "cc",
    "bcc": "bcc",
    "reply_to": "reply-to",
    "date": "date",
    "message_id": "message-id",
    "subject": "subject",
}
_INVALID_MESSAGE_MESSAGE = "invalid Gmail message"


def normalize_message(
    raw: Mapping[str, object], connector: ConnectorRecord, history_id: str
) -> NewEvent:
    """Convert one full Gmail response to the canonical received-email event."""
    message_id = _required_ascii_string(raw, "id")
    thread_id = _required_ascii_string(raw, "threadId")
    occurred_at = _occurred_at(_required_ascii_decimal(raw, "internalDate"))
    warnings: list[str] = []

    payload_mapping = _mapping_or_none(raw.get("payload"))
    if payload_mapping is None:
        _add_warning(warnings, "missing_payload")
        headers = _empty_headers()
        plain_parts: list[str] = []
        html_parts: list[str] = []
        attachments: list[dict[str, JsonValue]] = []
    else:
        headers = _selected_headers(payload_mapping, warnings)
        plain_parts, html_parts, attachments = _collect_parts(payload_mapping, warnings)

    actor = _actor_from_sender(headers["from"], warnings)
    payload: dict[str, JsonValue] = {
        "message_id": message_id,
        "thread_id": thread_id,
        "headers": cast(JsonValue, headers),
        "snippet": _string_or_empty(raw.get("snippet"), "invalid_snippet", warnings),
        "label_ids": cast(JsonValue, _label_ids(raw.get("labelIds"), warnings)),
        "plain_text": "\n".join(plain_parts),
        "html": "\n".join(html_parts),
        "attachments": cast(JsonValue, attachments),
    }
    metadata: dict[str, JsonValue] = {
        "connector_id": str(connector.id),
        "gmail_thread_id": thread_id,
        "history_id": history_id,
        "normalization_schema_version": 1,
        "normalization_warnings": cast(JsonValue, warnings),
    }

    return NewEvent(
        user_id=connector.user_id,
        workspace_id=connector.workspace_id,
        source="gmail",
        event_type="email.received",
        external_id=message_id,
        idempotency_key=f"gmail:{connector.id}:{message_id}:received",
        occurred_at=occurred_at,
        principal_type=PrincipalType.EXTERNAL,
        principal_id=connector.id,
        actor=actor,
        subject={"type": "email", "id": message_id},
        payload=payload,
        metadata=metadata,
        correlation_keys=[f"gmail-thread:{thread_id}"],
        schema_version=1,
    )


def _required_ascii_string(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(_INVALID_MESSAGE_MESSAGE)
    return value


def _required_ascii_decimal(raw: Mapping[str, object], key: str) -> str:
    value = _required_ascii_string(raw, key)
    if not (value.isascii() and value.isdecimal()):
        raise ValueError(_INVALID_MESSAGE_MESSAGE)
    return value


def _occurred_at(internal_date: str) -> datetime:
    try:
        return datetime.fromtimestamp(int(internal_date) / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError(_INVALID_MESSAGE_MESSAGE) from error


def _mapping_or_none(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _empty_headers() -> dict[str, str]:
    return dict.fromkeys(_SELECTED_HEADERS, "")


def _selected_headers(payload: Mapping[str, object], warnings: list[str]) -> dict[str, str]:
    headers = _empty_headers()
    raw_headers = payload.get("headers")
    if raw_headers is None:
        return headers
    if not isinstance(raw_headers, list):
        _add_warning(warnings, "malformed_headers")
        return headers

    by_name: dict[str, str] = {}
    for raw_header in raw_headers:
        header = _mapping_or_none(raw_header)
        if header is None:
            _add_warning(warnings, "malformed_headers")
            continue
        name = header.get("name")
        value = header.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            _add_warning(warnings, "malformed_headers")
            continue
        by_name.setdefault(name.lower(), value)

    for key, header_name in _SELECTED_HEADERS.items():
        headers[key] = by_name.get(header_name, "")
    return headers


def _collect_parts(
    payload: Mapping[str, object], warnings: list[str]
) -> tuple[list[str], list[str], list[dict[str, JsonValue]]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, JsonValue]] = []
    for part in _walk_parts(payload, warnings):
        mime_type, charset = _mime_details(part, warnings)
        content_disposition = _content_disposition(part, warnings)
        body = _mapping_or_none(part.get("body"))
        filename = _string_or_empty(part.get("filename"), "malformed_filename", warnings)
        attachment_id = (
            None if body is None else _optional_string(body.get("attachmentId"), warnings)
        )
        if filename or attachment_id is not None:
            attachments.append(
                {
                    "filename": filename,
                    "mime_type": mime_type,
                    "size": _attachment_size(body, warnings),
                    "attachment_id": attachment_id,
                }
            )

        if mime_type in {"text/plain", "text/html"} and content_disposition != "attachment":
            decoded = _decode_text(body, charset, warnings)
            if decoded is not None:
                (plain_parts if mime_type == "text/plain" else html_parts).append(decoded)
    return plain_parts, html_parts, attachments


def _walk_parts(
    payload: Mapping[str, object], warnings: list[str]
) -> Iterable[Mapping[str, object]]:
    yield payload
    raw_parts = payload.get("parts")
    if raw_parts is None:
        return
    if not isinstance(raw_parts, list):
        _add_warning(warnings, "malformed_parts")
        return
    for raw_part in raw_parts:
        part = _mapping_or_none(raw_part)
        if part is None:
            _add_warning(warnings, "malformed_parts")
            continue
        yield from _walk_parts(part, warnings)


def _mime_details(part: Mapping[str, object], warnings: list[str]) -> tuple[str, str | None]:
    raw_mime_type = part.get("mimeType")
    if not isinstance(raw_mime_type, str) or not raw_mime_type:
        _add_warning(warnings, "malformed_mime_type")
        return "application/octet-stream", None

    message = Message()
    message["Content-Type"] = raw_mime_type
    mime_type = message.get_content_type().lower()
    charset = message.get_content_charset()
    if charset is not None:
        return mime_type, charset

    for raw_header in _header_mappings(part.get("headers"), warnings):
        if raw_header[0].lower() == "content-type":
            message = Message()
            message["Content-Type"] = raw_header[1]
            return mime_type, message.get_content_charset()
    return mime_type, None


def _header_mappings(value: object, warnings: list[str]) -> Iterable[tuple[str, str]]:
    if value is None:
        return
    if not isinstance(value, list):
        _add_warning(warnings, "malformed_headers")
        return
    for item in value:
        header = _mapping_or_none(item)
        if header is None:
            _add_warning(warnings, "malformed_headers")
            continue
        name = header.get("name")
        header_value = header.get("value")
        if isinstance(name, str) and isinstance(header_value, str):
            yield name, header_value
        else:
            _add_warning(warnings, "malformed_headers")


def _content_disposition(part: Mapping[str, object], warnings: list[str]) -> str | None:
    for name, value in _header_mappings(part.get("headers"), warnings):
        if name.lower() == "content-disposition":
            return value.split(";", maxsplit=1)[0].strip().lower()
    return None


def _decode_text(
    body: Mapping[str, object] | None, charset: str | None, warnings: list[str]
) -> str | None:
    if body is None:
        return None
    encoded = body.get("data")
    if encoded is None:
        return None
    if not isinstance(encoded, str):
        _add_warning(warnings, "invalid_body_data")
        return None
    try:
        raw_bytes = urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except ValueError, UnicodeEncodeError:
        _add_warning(warnings, "invalid_base64url")
        return None

    if charset is not None:
        try:
            return raw_bytes.decode(charset)
        except LookupError:
            _add_warning(warnings, "unsupported_charset")
        except UnicodeDecodeError:
            _add_warning(warnings, "charset_decode_error")
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        _add_warning(warnings, "utf8_decode_error")
        return raw_bytes.decode("utf-8", errors="replace")


def _attachment_size(body: Mapping[str, object] | None, warnings: list[str]) -> int:
    if body is None:
        return 0
    size = body.get("size")
    if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
        return size
    if size is not None:
        _add_warning(warnings, "invalid_attachment_size")
    return 0


def _optional_string(value: object, warnings: list[str]) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value:
        return value
    _add_warning(warnings, "malformed_attachment_id")
    return None


def _string_or_empty(value: object, warning: str, warnings: list[str]) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    _add_warning(warnings, warning)
    return ""


def _label_ids(value: object, warnings: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(label, str) for label in value):
        _add_warning(warnings, "malformed_label_ids")
        return []
    return list(value)


def _actor_from_sender(sender: str, warnings: list[str]) -> dict[str, JsonValue] | None:
    if not sender:
        return None
    name, address = parseaddr(sender)
    if not address:
        _add_warning(warnings, "malformed_sender_header")
        return None
    return {"name": name, "email_address": address.lower()}


def _add_warning(warnings: list[str], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)
