from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import PositiveInt, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    CONSOLE = "console"
    JSON = "json"


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


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
    outbox_batch_limit: PositiveInt = 100
    outbox_lease_seconds: PositiveInt = 60
    processing_lease_seconds: PositiveInt = 300

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.upper()
        return value

    @field_validator("pubsub_topic_id", "gmail_topic_id", "gmail_subscription_id")
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
