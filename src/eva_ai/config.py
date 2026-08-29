from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
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
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://eva:eva@localhost:5432/eva"
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.upper()
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
