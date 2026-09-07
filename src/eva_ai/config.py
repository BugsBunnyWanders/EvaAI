from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, PositiveFloat, PositiveInt, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from eva_ai.relevance.types import RelevanceProvider as RelevanceProvider


class AppEnvironment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    CONSOLE = "console"
    JSON = "json"


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]
UnitInterval = Annotated[float, Field(ge=0.0, le=1.0)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="EVA_",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Eva"
    environment: AppEnvironment = AppEnvironment.LOCAL
    log_level: LogLevel = "INFO"
    log_format: LogFormat = LogFormat.CONSOLE
    database_url: SecretStr = SecretStr("postgresql+psycopg://eva:eva@localhost:5432/eva")
    pubsub_project_id: str | None = None
    pubsub_topic_id: str = "eva-events"
    gmail_topic_id: str = "eva-gmail-notifications"
    gmail_subscription_id: str = "eva-gmail-ingestion-local"
    gmail_account: str | None = None
    gmail_oauth_client_file: Path | None = None
    gmail_sync_lease_seconds: PositiveInt = 300
    gmail_pull_timeout_seconds: PositiveInt = 30
    gmail_watch_renewal_hours: PositiveInt = 24
    gmail_safety_sync_minutes: PositiveInt = 60
    gmail_request_timeout_seconds: PositiveFloat = 30.0
    gmail_retry_attempts: PositiveInt = 3
    gmail_retry_initial_backoff_seconds: PositiveFloat = 0.5
    gmail_retry_max_backoff_seconds: PositiveFloat = 8.0
    gmail_retry_jitter_ratio: UnitInterval = 0.2
    outbox_batch_limit: PositiveInt = 100
    outbox_lease_seconds: PositiveInt = 60
    processing_lease_seconds: PositiveInt = 300
    outbox_relay_poll_seconds: PositiveFloat = 1.0
    relevance_enabled: bool = False
    relevance_provider: RelevanceProvider = RelevanceProvider.OPENAI
    openai_api_key: SecretStr | None = None
    relevance_model: str = "gpt-5.6-luna"
    relevance_subscription_id: str = "eva-relevance-local"
    relevance_pull_timeout_seconds: PositiveInt = 30
    relevance_classifier_version: str = "relevance-v1"
    relevance_policy_version: str = "relevance-policy-v1"
    relevance_body_max_chars: int = Field(default=4000, ge=1, le=8000)
    relevance_goal_limit: int = Field(default=20, ge=1, le=20)
    relevance_goal_max_chars: int = Field(default=500, ge=1, le=1000)
    relevance_goal_total_chars: int = Field(default=8000, ge=1, le=10000)
    relevance_situation_limit: int = Field(default=5, ge=1, le=5)
    relevance_situation_max_chars: int = Field(default=500, ge=1, le=1000)
    relevance_situation_total_chars: int = Field(default=2500, ge=1, le=5000)
    relevance_notify_relevance: UnitInterval = 0.75
    relevance_notify_confidence: UnitInterval = 0.70
    relevance_notify_importance_or_urgency: UnitInterval = 0.65
    relevance_investigate_relevance: UnitInterval = 0.60
    relevance_investigate_confidence: UnitInterval = 0.65
    relevance_ignore_relevance: UnitInterval = 0.20
    relevance_ignore_confidence: UnitInterval = 0.80
    relevance_retry_attempts: PositiveInt = 3
    relevance_retry_initial_backoff_seconds: PositiveFloat = 2.0
    relevance_retry_max_backoff_seconds: PositiveFloat = 30.0
    relevance_retry_jitter_ratio: UnitInterval = 0.2
    relevance_ignored_sources: tuple[str, ...] = ()
    relevance_ignored_event_types: tuple[str, ...] = ()
    relevance_ignored_senders: tuple[str, ...] = ()
    relevance_ignored_labels: tuple[str, ...] = ()
    memory_embedding_model: str = "text-embedding-3-small"
    memory_embedding_dimensions: int = Field(default=1536, ge=1536, le=1536)
    memory_embedding_input_max_chars: int = Field(default=4000, ge=1, le=8000)
    memory_fact_limit: int = Field(default=20, ge=1, le=100)
    memory_fact_total_chars: int = Field(default=8000, ge=1, le=16000)
    memory_episode_candidate_limit: int = Field(default=50, ge=1, le=100)
    memory_episode_limit: int = Field(default=8, ge=1, le=20)
    memory_episode_total_chars: int = Field(default=6000, ge=1, le=12000)
    agent_enabled: bool = False
    agent_topic_id: str = "eva-agent-runs"
    agent_subscription_id: str = "eva-agent-local"
    agent_model: str = "gpt-5.6-sol"
    agent_reasoning_effort: ReasoningEffort = "medium"
    agent_version: str = "investigation-v2"
    agent_prompt_version: str = "investigation-prompt-v2"
    agent_pull_timeout_seconds: PositiveInt = 30
    agent_lease_seconds: PositiveInt = 900
    agent_max_attempts: PositiveInt = 4
    agent_retry_initial_backoff_seconds: PositiveFloat = 10.0
    agent_retry_max_backoff_seconds: PositiveFloat = 300.0
    agent_max_turns: int = Field(default=6, ge=1, le=10)
    agent_max_tool_calls: int = Field(default=4, ge=0, le=10)
    agent_tool_timeout_seconds: PositiveFloat = 30.0
    agent_thread_message_limit: int = Field(default=20, ge=1, le=20)
    agent_search_result_limit: int = Field(default=10, ge=1, le=10)
    agent_message_body_max_chars: int = Field(default=6000, ge=1, le=8000)
    telegram_enabled: bool = False
    telegram_bot_username: str | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None
    telegram_turn_topic_id: str = "eva-telegram-turns"
    telegram_turn_subscription_id: str = "eva-telegram-turns-local"
    telegram_delivery_topic_id: str = "eva-telegram-delivery"
    telegram_delivery_subscription_id: str = "eva-telegram-delivery-local"
    telegram_pairing_ttl_seconds: PositiveInt = 900
    telegram_webhook_max_bytes: int = Field(default=64_000, ge=1_024, le=1_000_000)
    telegram_pull_timeout_seconds: PositiveInt = 30
    telegram_lease_seconds: PositiveInt = 300
    telegram_max_attempts: PositiveInt = 6
    telegram_retry_initial_backoff_seconds: PositiveFloat = 2.0
    telegram_retry_max_backoff_seconds: PositiveFloat = 120.0
    telegram_message_max_chars: int = Field(default=4000, ge=1, le=4096)
    conversation_model: str = "gpt-5.6-sol"
    conversation_reasoning_effort: ReasoningEffort = "medium"
    conversation_agent_version: str = "conversation-v2"
    conversation_prompt_version: str = "conversation-prompt-v2"
    conversation_history_turn_limit: int = Field(default=20, ge=1, le=40)
    conversation_history_max_chars: int = Field(default=24_000, ge=1_000, le=50_000)
    conversation_max_turns: int = Field(default=6, ge=1, le=10)
    conversation_max_tool_calls: int = Field(default=4, ge=0, le=10)

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.upper()
        return value

    @field_validator(
        "pubsub_topic_id",
        "gmail_topic_id",
        "gmail_subscription_id",
        "relevance_model",
        "relevance_subscription_id",
        "relevance_classifier_version",
        "relevance_policy_version",
        "memory_embedding_model",
        "agent_topic_id",
        "agent_subscription_id",
        "agent_model",
        "agent_version",
        "agent_prompt_version",
        "telegram_turn_topic_id",
        "telegram_turn_subscription_id",
        "telegram_delivery_topic_id",
        "telegram_delivery_subscription_id",
        "conversation_model",
        "conversation_agent_version",
        "conversation_prompt_version",
    )
    @classmethod
    def reject_blank_topic_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("gmail_account")
    @classmethod
    def reject_blank_gmail_account(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("telegram_bot_username")
    @classmethod
    def normalize_telegram_bot_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.removeprefix("@").strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator(
        "relevance_ignored_sources",
        "relevance_ignored_event_types",
        "relevance_ignored_senders",
        "relevance_ignored_labels",
    )
    @classmethod
    def normalize_relevance_rules(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({value.strip().casefold() for value in values if value.strip()}))

    @model_validator(mode="after")
    def validate_gmail_retry_bounds(self) -> Self:
        if self.gmail_retry_max_backoff_seconds < self.gmail_retry_initial_backoff_seconds:
            raise ValueError("Gmail retry maximum must not be below its initial backoff")
        if self.relevance_retry_max_backoff_seconds < self.relevance_retry_initial_backoff_seconds:
            raise ValueError("relevance retry maximum must not be below its initial backoff")
        if self.relevance_goal_total_chars < self.relevance_goal_max_chars:
            raise ValueError("Goal total bound must not be below per-item bound")
        if self.relevance_situation_total_chars < self.relevance_situation_max_chars:
            raise ValueError("Situation total bound must not be below per-item bound")
        if self.memory_episode_candidate_limit < self.memory_episode_limit:
            raise ValueError("memory candidate limit must not be below result limit")
        if self.agent_retry_max_backoff_seconds < self.agent_retry_initial_backoff_seconds:
            raise ValueError("agent retry maximum must not be below its initial backoff")
        if self.telegram_retry_max_backoff_seconds < self.telegram_retry_initial_backoff_seconds:
            raise ValueError("Telegram retry maximum must not be below its initial backoff")
        if self.relevance_enabled and self.relevance_provider is RelevanceProvider.OPENAI:
            if self.openai_api_key is None or not self.openai_api_key.get_secret_value().strip():
                raise ValueError("OpenAI API key is required when relevance processing is enabled")
        if self.agent_enabled:
            if not self.relevance_enabled:
                raise ValueError("agent investigation requires relevance processing")
            if self.openai_api_key is None or not self.openai_api_key.get_secret_value().strip():
                raise ValueError("OpenAI API key is required when agent investigation is enabled")
        if self.telegram_enabled:
            if not self.relevance_enabled or not self.agent_enabled:
                raise ValueError("Telegram processing requires relevance and agent processing")
            if self.openai_api_key is None or not self.openai_api_key.get_secret_value().strip():
                raise ValueError("OpenAI API key is required when Telegram processing is enabled")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
