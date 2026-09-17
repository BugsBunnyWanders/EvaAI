import pytest
from pydantic import SecretStr, ValidationError

from eva_ai.config import AppEnvironment, LogFormat, RelevanceProvider, Settings


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


def test_relevance_settings_have_safe_disabled_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.relevance_enabled is False
    assert settings.relevance_provider is RelevanceProvider.OPENAI
    assert settings.relevance_model == "gpt-5.6-luna"
    assert settings.relevance_body_max_chars == 4000
    assert settings.relevance_goal_limit == 20
    assert settings.relevance_situation_limit == 5
    assert settings.relevance_retry_attempts == 3
    assert settings.outbox_relay_poll_seconds == 1.0
    assert settings.openai_api_key is None


def test_enabled_openai_relevance_requires_secret() -> None:
    with pytest.raises(ValidationError, match="OpenAI API key"):
        Settings(_env_file=None, relevance_enabled=True, openai_api_key=None)


@pytest.mark.parametrize("poll_seconds", [0, -1])
def test_relevance_settings_reject_non_positive_relay_poll(poll_seconds: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, outbox_relay_poll_seconds=poll_seconds)


def test_relevance_settings_reject_incoherent_retry_bounds() -> None:
    with pytest.raises(ValidationError, match="relevance retry maximum"):
        Settings(
            _env_file=None,
            relevance_retry_initial_backoff_seconds=5,
            relevance_retry_max_backoff_seconds=2,
        )


def test_memory_settings_have_bounded_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.memory_embedding_model == "text-embedding-3-small"
    assert settings.memory_embedding_dimensions == 1536
    assert settings.memory_embedding_input_max_chars == 4000
    assert settings.memory_fact_limit == 20
    assert settings.memory_episode_candidate_limit == 50
    assert settings.memory_episode_limit == 8


def test_memory_settings_reject_dimension_drift_and_incoherent_limits() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, memory_embedding_dimensions=512)
    with pytest.raises(ValidationError, match="memory candidate limit"):
        Settings(_env_file=None, memory_episode_candidate_limit=5, memory_episode_limit=8)


def test_telegram_settings_have_safe_disabled_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.telegram_enabled is False
    assert settings.telegram_bot_token is None
    assert settings.telegram_webhook_secret is None
    assert settings.telegram_turn_topic_id == "eva-telegram-turns"
    assert settings.telegram_delivery_topic_id == "eva-telegram-delivery"
    assert settings.telegram_message_max_chars == 4000
    assert settings.conversation_history_turn_limit == 20


def test_telegram_username_is_normalized_without_becoming_identity() -> None:
    settings = Settings(_env_file=None, telegram_bot_username="@EvaPersonalBot")

    assert settings.telegram_bot_username == "EvaPersonalBot"


def test_enabled_telegram_requires_agent_pipeline_and_openai() -> None:
    with pytest.raises(ValidationError, match="requires relevance and agent"):
        Settings(
            _env_file=None,
            telegram_enabled=True,
            openai_api_key=SecretStr("test-key"),
        )

    with pytest.raises(ValidationError, match="OpenAI API key"):
        Settings(
            _env_file=None,
            telegram_enabled=True,
            relevance_enabled=True,
            agent_enabled=True,
            openai_api_key=None,
        )


def test_enabled_telegram_allows_runtime_specific_secret_injection() -> None:
    """API and worker revisions receive different Telegram secrets by design."""
    settings = Settings(
        _env_file=None,
        telegram_enabled=True,
        relevance_enabled=True,
        agent_enabled=True,
        openai_api_key=SecretStr("test-key"),
    )

    assert settings.telegram_bot_token is None
    assert settings.telegram_webhook_secret is None


def test_action_runtime_has_safe_disabled_and_bounded_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.actions_enabled is False
    assert settings.action_approval_enabled is False
    assert settings.action_executor_enabled is False
    assert settings.action_dispatch_subscription_id == "eva-action-dispatch-local"
    assert settings.action_approval_ttl_hours == 24
    assert settings.action_revision_ttl_seconds == 900
    assert settings.action_lease_seconds == 300
    assert settings.action_task_timeout_seconds == 300
    assert settings.action_task_max_attempts == 5


def test_api_can_enable_action_approvals_without_worker_processing() -> None:
    settings = Settings(_env_file=None, action_approval_enabled=True)

    assert settings.action_approval_enabled is True
    assert settings.actions_enabled is False
    assert settings.telegram_enabled is False


def test_action_approval_ttl_accepts_production_environment_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloud Run supplies every environment variable as text."""
    monkeypatch.setenv("EVA_ACTION_APPROVAL_TTL_HOURS", "24")

    settings = Settings(_env_file=None)

    assert settings.action_approval_ttl_hours == 24


def test_enabled_actions_require_complete_cloud_tasks_configuration() -> None:
    with pytest.raises(ValidationError, match="action execution configuration"):
        Settings(
            _env_file=None,
            actions_enabled=True,
            telegram_enabled=True,
            relevance_enabled=True,
            agent_enabled=True,
            openai_api_key=SecretStr("test-key"),
        )

    settings = Settings(
        _env_file=None,
        actions_enabled=True,
        telegram_enabled=True,
        relevance_enabled=True,
        agent_enabled=True,
        openai_api_key=SecretStr("test-key"),
        action_tasks_project_id="eva-project",
        action_tasks_location="asia-south1",
        action_tasks_queue_id="eva-actions",
        action_executor_url="https://executor.example/internal/actions/execute",
        action_executor_audience="https://executor.example",
        action_task_caller_service_account="caller@eva-project.iam.gserviceaccount.com",
    )
    assert settings.actions_enabled is True


def test_enabled_actions_require_telegram_approval_delivery() -> None:
    with pytest.raises(ValidationError, match="Telegram processing"):
        Settings(
            _env_file=None,
            actions_enabled=True,
            action_tasks_project_id="eva-project",
            action_tasks_location="asia-south1",
            action_tasks_queue_id="eva-actions",
            action_executor_url="https://executor.example/internal/actions/execute",
            action_executor_audience="https://executor.example",
            action_task_caller_service_account=("caller@eva-project.iam.gserviceaccount.com"),
        )


def test_action_runtime_rejects_non_24_hour_approval_and_incoherent_retry_bounds() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, action_approval_ttl_hours=12)
    with pytest.raises(ValidationError, match="action task retry maximum"):
        Settings(
            _env_file=None,
            action_task_retry_initial_backoff_seconds=20,
            action_task_retry_max_backoff_seconds=10,
        )
