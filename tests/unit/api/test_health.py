from fastapi.testclient import TestClient

from eva_ai.api.dependencies import get_database
from eva_ai.config import Settings
from eva_ai.main import create_app


class PassingProbe:
    async def ping(self) -> None:
        return None


class FailingProbe:
    async def ping(self) -> None:
        raise RuntimeError("postgresql+psycopg://secret:secret@database/eva")


def test_liveness_does_not_require_database() -> None:
    application = create_app(Settings(_env_file=None))

    with TestClient(application) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_ready_when_database_responds() -> None:
    application = create_app(Settings(_env_file=None))
    application.dependency_overrides[get_database] = PassingProbe

    with TestClient(application) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readiness_returns_safe_failure_body() -> None:
    application = create_app(Settings(_env_file=None))
    application.dependency_overrides[get_database] = FailingProbe

    with TestClient(application) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "secret" not in response.text
