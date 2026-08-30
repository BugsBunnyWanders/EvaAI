import copy
import json
import logging
import sys
from datetime import UTC, datetime
from typing import TextIO
from uuid import UUID

from eva_ai.config import LogFormat, Settings

_SAFE_IDENTIFIER_FIELDS = (
    "event_id",
    "user_id",
    "workspace_id",
    "outbox_message_id",
    "connector_id",
    "pubsub_message_id",
    "gmail_message_id",
    "gmail_thread_id",
    "claim_id",
)
_SAFE_CATEGORY_VALUES = {
    "operation": frozenset(
        {
            "acknowledgement",
            "gmail_pull",
            "maintenance",
            "notification_decode",
            "notification_sync",
            "pull",
            "subscriber_close",
        }
    ),
    "outcome": frozenset(
        {
            "acknowledged",
            "already_handled",
            "busy",
            "failed",
            "handled",
            "negative_acknowledged",
            "published",
            "stale",
        }
    ),
    "error_category": frozenset(
        {
            "connector_connecting",
            "credential_provider_transient",
            "gmail_provider_transient",
            "internal_failure",
            "maintenance_failed",
            "malformed_notification",
            "provider_transient",
            "synchronization_busy",
            "synchronization_failed",
            "transport_failed",
            "unknown_account",
        }
    ),
}


def _safe_context(record: logging.LogRecord) -> dict[str, str]:
    context: dict[str, str] = {}
    # LogRecord extras are arbitrary; identifiers and fixed categories cross separate gates.
    for field in _SAFE_IDENTIFIER_FIELDS:
        value = getattr(record, field, None)
        if isinstance(value, str):
            context[field] = value
        elif isinstance(value, UUID):
            context[field] = str(value)
    for field, allowed_values in _SAFE_CATEGORY_VALUES.items():
        value = getattr(record, field, None)
        if isinstance(value, str) and value in allowed_values:
            context[field] = value
    return context


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_safe_context(record),
        }
        return json.dumps(payload, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        safe_record = copy.copy(record)
        safe_record.exc_info = None
        safe_record.exc_text = None
        safe_record.stack_info = None
        rendered = super().format(safe_record)
        context = _safe_context(record)
        if context:
            rendered = f"{rendered} " + " ".join(f"{key}={value}" for key, value in context.items())
        return rendered


def configure_logging(settings: Settings, stream: TextIO | None = None) -> None:
    handler = logging.StreamHandler(stream or sys.stderr)
    if settings.log_format is LogFormat.JSON:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(ConsoleFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)
