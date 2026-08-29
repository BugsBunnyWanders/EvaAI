import io
import json
import logging

from eva_ai.config import LogFormat, Settings
from eva_ai.logging import JsonFormatter, configure_logging


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


def test_configure_logging_replaces_root_handlers() -> None:
    stream = io.StringIO()
    settings = Settings(log_level="WARNING", log_format=LogFormat.JSON, _env_file=None)

    configure_logging(settings, stream=stream)
    logging.getLogger("eva.test").warning("careful")

    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert len(root.handlers) == 1
    assert json.loads(stream.getvalue())["message"] == "careful"
