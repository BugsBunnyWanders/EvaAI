from datetime import UTC, datetime
from uuid import UUID

import pytest

from eva_ai.connectors.gmail.normalizer import normalize_message
from eva_ai.connectors.types import ConnectorRecord, ConnectorStatus
from eva_ai.events.types import PrincipalType

CONNECTOR = ConnectorRecord(
    id=UUID("0191cafe-7b00-7000-8000-000000000003"),
    user_id=UUID("0191cafe-7b00-7000-8000-000000000001"),
    workspace_id=UUID("0191cafe-7b00-7000-8000-000000000002"),
    provider="gmail",
    account_identity="owner@example.com",
    granted_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    status=ConnectorStatus.ACTIVE,
    secret_reference="projects/eva/secrets/gmail/versions/1",
    connected_at=datetime(2026, 8, 30, tzinfo=UTC),
)


def test_normalize_message_maps_nested_mime_content_to_received_event() -> None:
    """Fails if recursive MIME traversal loses alternative text or attachment metadata."""
    raw_message = {
        "id": "msg-1",
        "threadId": "thread-1",
        "internalDate": "1787961600000",
        "labelIds": ["INBOX", "CATEGORY_PROMOTIONS"],
        "snippet": "A short message preview",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Eva Sender <sender@example.com>"},
                {"name": "To", "value": "owner@example.com"},
                {"name": "Subject", "value": "Welcome"},
            ],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": "SGVsbG8gRXZh"}},
                        {"mimeType": "text/html", "body": {"data": "PHA-SGVsbG8gRXZhPC9wPg"}},
                    ],
                },
                {
                    "filename": "brief.pdf",
                    "mimeType": "application/pdf",
                    "body": {"size": 42, "attachmentId": "attachment-1"},
                },
            ],
        },
    }

    event = normalize_message(raw_message, CONNECTOR, history_id="900")

    assert event.source == "gmail"
    assert event.event_type == "email.received"
    assert event.external_id == "msg-1"
    assert event.idempotency_key == f"gmail:{CONNECTOR.id}:msg-1:received"
    assert event.principal_type is PrincipalType.EXTERNAL
    assert event.principal_id == CONNECTOR.id
    assert event.correlation_keys == ["gmail-thread:thread-1"]
    assert event.occurred_at == datetime(2026, 8, 29, tzinfo=UTC)
    assert event.actor == {"name": "Eva Sender", "email_address": "sender@example.com"}
    assert event.subject == {"type": "email", "id": "msg-1"}
    assert event.payload == {
        "message_id": "msg-1",
        "thread_id": "thread-1",
        "headers": {
            "from": "Eva Sender <sender@example.com>",
            "to": "owner@example.com",
            "cc": "",
            "bcc": "",
            "reply_to": "",
            "date": "",
            "message_id": "",
            "subject": "Welcome",
        },
        "snippet": "A short message preview",
        "label_ids": ["INBOX", "CATEGORY_PROMOTIONS"],
        "plain_text": "Hello Eva",
        "html": "<p>Hello Eva</p>",
        "attachments": [
            {
                "filename": "brief.pdf",
                "mime_type": "application/pdf",
                "size": 42,
                "attachment_id": "attachment-1",
            }
        ],
    }
    assert event.metadata == {
        "connector_id": str(CONNECTOR.id),
        "gmail_thread_id": "thread-1",
        "history_id": "900",
        "normalization_schema_version": 1,
        "normalization_warnings": [],
    }
    assert event.schema_version == 1


def test_normalize_message_decodes_inline_iso_8859_1_text_without_padding() -> None:
    """Fails if inline text is treated as an attachment or declared charset is ignored."""
    raw_message = {
        "id": "msg-inline",
        "threadId": "thread-inline",
        "internalDate": "1788019200000",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "From", "value": "sender@example.com"}],
            "parts": [
                {
                    "mimeType": "text/plain; charset=ISO-8859-1",
                    "headers": [{"name": "Content-Disposition", "value": "inline"}],
                    "body": {"data": "T2zh"},
                }
            ],
        },
    }

    event = normalize_message(raw_message, CONNECTOR, history_id="901")

    assert event.payload["plain_text"] == "Ol\u00e1"
    assert event.payload["attachments"] == []
    assert event.metadata["normalization_warnings"] == []


def test_normalize_message_decodes_single_part_plain_text() -> None:
    """Fails if a non-multipart Gmail body is omitted from readable content."""
    raw_message = {
        "id": "msg-single",
        "threadId": "thread-single",
        "internalDate": "1788019200000",
        "payload": {"mimeType": "text/plain", "body": {"data": "SGVsbG8gRXZh"}},
    }

    event = normalize_message(raw_message, CONNECTOR, history_id="902")

    assert event.payload["plain_text"] == "Hello Eva"
    assert event.payload["html"] == ""


def test_normalize_message_keeps_named_and_identified_text_parts_as_attachment_metadata() -> None:
    """Fails if text attachments without an attachment disposition become readable bodies."""
    raw_message = {
        "id": "msg-text-attachments",
        "threadId": "thread-text-attachments",
        "internalDate": "1788019200000",
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "filename": "notes.txt",
                    "mimeType": "text/plain",
                    "body": {"data": "U2hvdWxkIG5vdCBiZSBib2R5", "size": 18},
                },
                {
                    "mimeType": "text/html",
                    "headers": [{"name": "Content-Disposition", "value": "inline"}],
                    "body": {
                        "data": "PHA-U2hvdWxkIG5vdCBiZSBib2R5PC9wPg",
                        "size": 25,
                        "attachmentId": "attachment-html-1",
                    },
                },
            ],
        },
    }

    event = normalize_message(raw_message, CONNECTOR, history_id="903")

    assert event.payload["plain_text"] == ""
    assert event.payload["html"] == ""
    assert event.payload["attachments"] == [
        {"filename": "notes.txt", "mime_type": "text/plain", "size": 18, "attachment_id": None},
        {
            "filename": "",
            "mime_type": "text/html",
            "size": 25,
            "attachment_id": "attachment-html-1",
        },
    ]


def test_normalize_message_uses_content_free_warnings_for_recoverable_body_defects() -> None:
    """Fails if invalid base64 or charset/header recovery exposes provider content."""
    raw_message = {
        "id": "msg-defects",
        "threadId": "thread-defects",
        "internalDate": "1788019200000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "From"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": "%%%"}},
                {
                    "mimeType": "text/plain; charset=unknown-charset",
                    "body": {"data": "SGVsbG8"},
                },
            ],
        },
    }

    event = normalize_message(raw_message, CONNECTOR, history_id="904")

    assert event.payload["plain_text"] == "Hello"
    assert event.metadata["normalization_warnings"] == [
        "malformed_headers",
        "invalid_base64url",
        "unsupported_charset",
    ]


@pytest.mark.parametrize("encoded", ["+w", "/w"])
def test_normalize_message_rejects_standard_base64_alphabet(encoded: str) -> None:
    """Fails if standard Base64 characters are accepted in Gmail base64url data."""
    raw_message = {
        "id": "msg-standard-base64",
        "threadId": "thread-standard-base64",
        "internalDate": "1788019200000",
        "payload": {"mimeType": "text/plain", "body": {"data": encoded}},
    }

    event = normalize_message(raw_message, CONNECTOR, history_id="905")

    assert event.payload["plain_text"] == ""
    assert event.payload["html"] == ""
    assert event.metadata["normalization_warnings"] == ["invalid_base64url"]


def test_normalize_message_keeps_text_attachments_out_of_readable_body() -> None:
    """Fails if a text attachment is mistaken for an inline message body."""
    raw_message = {
        "id": "msg-attachment",
        "threadId": "thread-attachment",
        "internalDate": "1788019200000",
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "filename": "notes.txt",
                    "mimeType": "text/plain",
                    "headers": [{"name": "Content-Disposition", "value": "attachment"}],
                    "body": {
                        "data": "SG9sZCBiYWNr",
                        "size": 9,
                        "attachmentId": "attachment-text-1",
                    },
                }
            ],
        },
    }

    event = normalize_message(raw_message, CONNECTOR, history_id="902")

    assert event.payload["plain_text"] == ""
    assert event.payload["attachments"] == [
        {
            "filename": "notes.txt",
            "mime_type": "text/plain",
            "size": 9,
            "attachment_id": "attachment-text-1",
        }
    ]


@pytest.mark.parametrize("field", ["id", "threadId", "internalDate"])
def test_normalize_message_requires_provider_identifiers(field: str) -> None:
    """Fails if unaddressable messages become events with unstable identity or timestamps."""
    raw_message = {
        "id": "msg-1",
        "threadId": "thread-1",
        "internalDate": "1788048000000",
        "payload": {"mimeType": "text/plain", "body": {"data": "SGVsbG8"}},
    }
    raw_message.pop(field)

    with pytest.raises(ValueError):
        normalize_message(raw_message, CONNECTOR, history_id="900")
