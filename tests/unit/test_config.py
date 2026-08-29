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
