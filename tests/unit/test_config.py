import pytest
from pydantic import SecretStr, ValidationError

from eva_ai.config import AppEnvironment, LogFormat, Settings


def test_settings_have_safe_local_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "EVA_APP_NAME",
        "EVA_ENVIRONMENT",
        "EVA_LOG_LEVEL",
        "EVA_LOG_FORMAT",
        "EVA_DATABASE_URL",
    ):
        monkeypatch.delenv(variable, raising=False)

    settings = Settings(_env_file=None)

    assert settings.app_name == "Eva"
    assert settings.environment is AppEnvironment.LOCAL
    assert settings.log_level == "INFO"
    assert settings.log_format is LogFormat.CONSOLE
    assert isinstance(settings.database_url, SecretStr)
    assert "eva:eva" not in str(settings.database_url)


def test_settings_read_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVA_ENVIRONMENT", "production")
    monkeypatch.setenv("EVA_LOG_FORMAT", "json")
    monkeypatch.setenv("EVA_LOG_LEVEL", "warning")

    settings = Settings(_env_file=None)

    assert settings.environment is AppEnvironment.PRODUCTION
    assert settings.log_format is LogFormat.JSON
    assert settings.log_level == "WARNING"


def test_settings_reject_unknown_log_level() -> None:
    with pytest.raises(ValidationError):
        Settings(log_level="VERBOSE", _env_file=None)


def test_event_backbone_settings_have_safe_local_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.pubsub_project_id is None
    assert settings.pubsub_topic_id == "eva-events"
    assert settings.outbox_batch_limit == 100
    assert settings.outbox_lease_seconds == 60
    assert settings.processing_lease_seconds == 300


def test_event_backbone_settings_reject_non_positive_limits() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, outbox_batch_limit=0)


@pytest.mark.parametrize("topic_id", ["", "   "])
def test_event_backbone_settings_reject_blank_topic_id(topic_id: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, pubsub_topic_id=topic_id)


def test_gmail_settings_have_safe_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.gmail_topic_id == "eva-gmail-notifications"
    assert settings.gmail_subscription_id == "eva-gmail-ingestion-local"
    assert settings.gmail_account is None
    assert settings.gmail_oauth_client_file is None
    assert settings.gmail_sync_lease_seconds == 300
    assert settings.gmail_pull_timeout_seconds == 30
    assert settings.gmail_watch_renewal_hours == 24
    assert settings.gmail_safety_sync_minutes == 60
    assert settings.gmail_request_timeout_seconds == 30.0
    assert settings.gmail_retry_attempts == 3
    assert settings.gmail_retry_initial_backoff_seconds == 0.5
    assert settings.gmail_retry_max_backoff_seconds == 8.0
    assert settings.gmail_retry_jitter_ratio == 0.2


def test_gmail_settings_reject_retry_maximum_below_initial_backoff() -> None:
    """Fails if operator settings can make the exponential retry envelope incoherent."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            gmail_retry_initial_backoff_seconds=2.0,
            gmail_retry_max_backoff_seconds=1.0,
        )
