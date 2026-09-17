from collections.abc import Mapping
from typing import Never, cast
from uuid import uuid7

import pytest
from pydantic import SecretStr

from eva_ai.api.dependencies import build_action_executor_dependencies
from eva_ai.config import Settings
from eva_ai.db.session import Database
from eva_ai.events.processor import (
    EventHandler,
    EventProcessor,
    ProcessOutcome,
    ProcessResult,
    StoredEvent,
)
from eva_ai.events.publisher import InMemoryPublisher, Publisher
from eva_ai.events.types import EventAvailableMessage
from eva_ai.relevance.classifier import ScriptedRelevanceClassifier
from eva_ai.worker import (
    build_action_application_dependencies,
    build_action_dispatch_dependencies,
    build_event_processor,
    build_memory_dependencies,
    build_outbox_relay,
    build_publisher,
    build_relevance_dependencies,
    dispatch_event,
)


class RecordingProcessor:
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.messages: list[EventAvailableMessage] = []
        self.handlers: list[EventHandler] = []

    async def process(
        self,
        message: EventAvailableMessage,
        handler: EventHandler,
    ) -> ProcessResult:
        self.messages.append(message)
        self.handlers.append(handler)
        return self.result


class UnexpectedHandler:
    async def prepare(self, event: StoredEvent) -> Never:
        raise AssertionError(f"worker unexpectedly invoked handler for {event.id}")


class RecordingOutboxRelay:
    def __init__(
        self,
        database: Database,
        publisher: Publisher,
        lease_seconds: int,
    ) -> None:
        self.database = database
        self.publisher = publisher
        self.lease_seconds = lease_seconds


class RecordingEventProcessor:
    def __init__(self, database: Database, lease_seconds: int) -> None:
        self.database = database
        self.lease_seconds = lease_seconds


def fail_google_publisher_construction(project_id: str) -> Never:
    raise AssertionError(f"Google publisher must not be constructed for {project_id!r}")


def test_local_composition_uses_in_memory_publisher() -> None:
    settings = Settings(_env_file=None)

    publisher = build_publisher(settings, use_google=False)

    assert isinstance(publisher, InMemoryPublisher)


def test_google_composition_requires_project_id() -> None:
    settings = Settings(_env_file=None, pubsub_project_id=None)

    with pytest.raises(ValueError, match="EVA_PUBSUB_PROJECT_ID"):
        build_publisher(settings, use_google=True)


def test_disabled_relevance_refuses_composition() -> None:
    with pytest.raises(ValueError, match="relevance processing is disabled"):
        build_relevance_dependencies(Settings(_env_file=None), include_pull_worker=False)


def test_disabled_actions_refuse_dispatcher_and_executor_composition() -> None:
    settings = Settings(_env_file=None)

    with pytest.raises(ValueError, match="action execution is disabled"):
        build_action_dispatch_dependencies(settings)
    with pytest.raises(ValueError, match="action execution is disabled"):
        build_action_executor_dependencies(settings)


def test_action_application_composition_shares_repository_across_proposal_and_revision() -> None:
    settings = Settings(_env_file=None).model_copy(update={"actions_enabled": True})
    database = Database(settings.database_url.get_secret_value())

    dependencies = build_action_application_dependencies(database, settings)

    assert dependencies is not None
    assert dependencies.proposals._store is dependencies.repository
    assert dependencies.revisions._store is dependencies.repository
    assert dependencies.repository._notification_destination == settings.telegram_delivery_topic_id


def test_action_executor_publishes_follow_up_approvals_without_telegram_routes() -> None:
    settings = Settings(
        _env_file=None,
        actions_enabled=True,
        pubsub_project_id="eva-project",
        action_tasks_project_id="eva-project",
        action_tasks_location="asia-south1",
        action_tasks_queue_id="eva-actions",
        action_executor_url="https://executor.example/internal/actions/execute",
        action_executor_audience="https://executor.example",
        action_task_caller_service_account="caller@eva-project.iam.gserviceaccount.com",
    )

    dependencies = build_action_executor_dependencies(settings)

    assert settings.telegram_enabled is False
    assert dependencies.repository._notification_destination == settings.telegram_delivery_topic_id


async def test_memory_composition_without_embeddings_does_not_require_openai() -> None:
    dependencies = build_memory_dependencies(
        Settings(_env_file=None, openai_api_key=None), include_embedding=False
    )

    assert dependencies.openai_client is None
    assert dependencies.context_builder is None
    await dependencies.close()


def test_embedding_memory_composition_requires_openai_key() -> None:
    with pytest.raises(ValueError, match="OpenAI configuration is incomplete"):
        build_memory_dependencies(
            Settings(_env_file=None, openai_api_key=None), include_embedding=True
        )


async def test_embedding_memory_composition_builds_context_runtime() -> None:
    dependencies = build_memory_dependencies(
        Settings(_env_file=None, openai_api_key=SecretStr("test-key")),
        include_embedding=True,
    )

    assert dependencies.openai_client is not None
    assert dependencies.context_builder is not None
    await dependencies.close()


async def test_injected_classifier_builds_database_only_relevance_runtime() -> None:
    settings = Settings(_env_file=None).model_copy(update={"relevance_enabled": True})

    dependencies = build_relevance_dependencies(
        settings,
        include_pull_worker=False,
        classifier=ScriptedRelevanceClassifier(()),
    )

    assert dependencies.openai_client is None
    assert dependencies.subscriber is None
    assert dependencies.worker is None
    await dependencies.close()


@pytest.mark.parametrize("project_id", ["", "   "])
def test_google_composition_rejects_blank_project_before_adapter_construction(
    project_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, pubsub_project_id=project_id)
    monkeypatch.setattr(
        "eva_ai.worker.GooglePubSubPublisher",
        fail_google_publisher_construction,
    )

    with pytest.raises(ValueError, match="EVA_PUBSUB_PROJECT_ID"):
        build_publisher(settings, use_google=True)


def test_outbox_relay_composition_passes_collaborators_and_outbox_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, outbox_lease_seconds=17, processing_lease_seconds=23)
    database = Database(settings.database_url.get_secret_value())
    publisher = InMemoryPublisher()
    monkeypatch.setattr("eva_ai.worker.OutboxRelay", RecordingOutboxRelay)

    relay = cast(
        RecordingOutboxRelay,
        build_outbox_relay(database, settings, publisher),
    )

    assert relay.database is database
    assert relay.publisher is publisher
    assert relay.lease_seconds == 17


def test_event_processor_composition_passes_database_and_processing_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, outbox_lease_seconds=17, processing_lease_seconds=23)
    database = Database(settings.database_url.get_secret_value())
    monkeypatch.setattr("eva_ai.worker.EventProcessor", RecordingEventProcessor)

    processor = cast(
        RecordingEventProcessor,
        build_event_processor(database, settings),
    )

    assert processor.database is database
    assert processor.lease_seconds == 23


@pytest.mark.parametrize("raw_format", ["mapping", "bytes", "string"])
@pytest.mark.asyncio
async def test_dispatch_validates_raw_message_before_one_processing_call(
    raw_format: str,
) -> None:
    message = EventAvailableMessage(
        outbox_message_id=uuid7(),
        event_id=uuid7(),
        user_id=uuid7(),
        workspace_id=uuid7(),
        event_type="test.created",
        schema_version=1,
    )
    raw_message: Mapping[str, object] | bytes | str
    if raw_format == "mapping":
        raw_message = message.model_dump(mode="json")
    elif raw_format == "bytes":
        raw_message = message.model_dump_json().encode("utf-8")
    else:
        raw_message = message.model_dump_json()
    expected = ProcessResult(message.event_id, ProcessOutcome.HANDLED)
    processor = RecordingProcessor(expected)
    handler = UnexpectedHandler()

    result = await dispatch_event(cast(EventProcessor, processor), raw_message, handler)

    assert processor.messages == [message]
    assert processor.handlers == [handler]
    assert result == expected
