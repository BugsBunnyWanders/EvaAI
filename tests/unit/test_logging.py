import io
import json
import logging
import sys
from types import TracebackType
from uuid import UUID, uuid7

from eva_ai.config import LogFormat, Settings
from eva_ai.logging import JsonFormatter, configure_logging


class SecretPayload:
    def __str__(self) -> str:
        return "payload-secret"


def safe_context() -> dict[str, UUID | str]:
    return {
        "event_id": uuid7(),
        "user_id": uuid7(),
        "workspace_id": uuid7(),
        "outbox_message_id": uuid7(),
        "claim_id": uuid7(),
        "outcome": "published",
    }


def secret_exception_info() -> tuple[type[BaseException], BaseException, TracebackType]:
    try:
        raise RuntimeError("postgresql://eva:secret@db/eva?token=top-secret")
    except RuntimeError:
        error_type, error, traceback = sys.exc_info()
        assert error_type is not None and error is not None and traceback is not None
        return error_type, error, traceback


def test_json_formatter_emits_cloud_friendly_fields() -> None:
    record = logging.LogRecord(
        name="eva.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="service ready",
        args=(),
        exc_info=None,
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["severity"] == "INFO"
    assert payload["logger"] == "eva.test"
    assert payload["message"] == "service ready"
    assert payload["timestamp"].endswith("Z")


def test_json_formatter_renders_only_allowlisted_context() -> None:
    context = safe_context()
    record = logging.LogRecord(
        name="eva.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="publish failed",
        args=(),
        exc_info=secret_exception_info(),
    )
    record.__dict__.update(
        context
        | {
            "payload": SecretPayload(),
            "credentials": "credential-secret",
        }
    )

    rendered = JsonFormatter().format(record)
    payload = json.loads(rendered)

    assert payload == {
        "timestamp": payload["timestamp"],
        "severity": "ERROR",
        "logger": "eva.test",
        "message": "publish failed",
        **{key: str(value) for key, value in context.items()},
    }
    assert "payload-secret" not in rendered
    assert "credential-secret" not in rendered
    assert "top-secret" not in rendered


def test_console_formatter_renders_only_allowlisted_context() -> None:
    stream = io.StringIO()
    settings = Settings(log_level="INFO", log_format=LogFormat.CONSOLE, _env_file=None)
    context = safe_context()
    configure_logging(settings, stream=stream)

    logging.getLogger("eva.test").error(
        "publish failed",
        extra=context
        | {
            "payload": SecretPayload(),
            "credentials": "credential-secret",
        },
        exc_info=secret_exception_info(),
    )

    rendered = stream.getvalue()
    for key, value in context.items():
        assert f"{key}={value}" in rendered
    assert "payload-secret" not in rendered
    assert "credential-secret" not in rendered
    assert "top-secret" not in rendered


def test_configure_logging_replaces_root_handlers() -> None:
    stream = io.StringIO()
    settings = Settings(log_level="WARNING", log_format=LogFormat.JSON, _env_file=None)

    configure_logging(settings, stream=stream)
    logging.getLogger("eva.test").warning("careful")

    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert len(root.handlers) == 1
    assert json.loads(stream.getvalue())["message"] == "careful"
