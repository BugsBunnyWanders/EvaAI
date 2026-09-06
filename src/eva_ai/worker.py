import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from openai import AsyncOpenAI

from eva_ai.config import Settings
from eva_ai.db.session import Database
from eva_ai.events.outbox import OutboxRelay
from eva_ai.events.processor import EventHandler, EventProcessor, ProcessResult
from eva_ai.events.publisher import InMemoryPublisher, Publisher
from eva_ai.events.relay_worker import OutboxRelayWorker
from eva_ai.events.types import EventAvailableMessage
from eva_ai.integrations.gcp.pubsub import GooglePubSubPublisher
from eva_ai.integrations.gcp.subscriber import GooglePullSubscriber
from eva_ai.integrations.openai.relevance import OpenAIClient, OpenAIRelevanceClassifier
from eva_ai.relevance.classifier import RelevanceClassifier, RelevanceClassifierRunner
from eva_ai.relevance.context import ContextBounds, RelevanceContextBuilder
from eva_ai.relevance.filters import RelevanceRuleSet, StaticRelevanceRuleProvider
from eva_ai.relevance.policy import RelevanceRoutingPolicy, RoutingThresholds
from eva_ai.relevance.repository import RelevanceRepository
from eva_ai.relevance.service import RelevanceEventHandler, RelevanceService
from eva_ai.relevance.worker import RelevancePullWorker
from eva_ai.situations.repository import SituationRepository


@dataclass(frozen=True, slots=True)
class DependencyCleanupOutcome:
    interruption: BaseException | None = None
    ordinary_failure: bool = False


@dataclass(slots=True)
class EventRelayDependencies:
    database: Database
    publisher: GooglePubSubPublisher
    relay: OutboxRelay
    worker: OutboxRelayWorker

    async def close(self) -> DependencyCleanupOutcome:
        try:
            await self.database.close()
        except asyncio.CancelledError as error:
            return DependencyCleanupOutcome(interruption=error)
        except Exception:
            return DependencyCleanupOutcome(ordinary_failure=True)
        return DependencyCleanupOutcome()


@dataclass(slots=True)
class RelevanceDependencies:
    database: Database
    handler: RelevanceEventHandler
    service: RelevanceService
    subscriber: GooglePullSubscriber | None
    worker: RelevancePullWorker | None
    openai_client: AsyncOpenAI | None

    async def close(self) -> DependencyCleanupOutcome:
        interruption: BaseException | None = None
        ordinary_failure = False
        close_functions = []
        if self.subscriber is not None:
            close_functions.append(self.subscriber.close)
        if self.openai_client is not None:
            close_functions.append(self.openai_client.close)
        close_functions.append(self.database.close)
        for close in close_functions:
            try:
                await close()
            except asyncio.CancelledError as error:
                interruption = interruption or error
            except Exception:
                ordinary_failure = True
        return DependencyCleanupOutcome(interruption, ordinary_failure)


def build_publisher(settings: Settings, *, use_google: bool) -> Publisher:
    if not use_google:
        return InMemoryPublisher()
    if settings.pubsub_project_id is None or not settings.pubsub_project_id.strip():
        raise ValueError("EVA_PUBSUB_PROJECT_ID is required for Google Pub/Sub")
    return GooglePubSubPublisher(settings.pubsub_project_id)


def build_outbox_relay(
    database: Database,
    settings: Settings,
    publisher: Publisher,
) -> OutboxRelay:
    return OutboxRelay(database, publisher, settings.outbox_lease_seconds)


def build_event_processor(database: Database, settings: Settings) -> EventProcessor:
    return EventProcessor(database, settings.processing_lease_seconds)


async def dispatch_event(
    processor: EventProcessor,
    raw_message: Mapping[str, object] | bytes | str,
    handler: EventHandler,
) -> ProcessResult:
    if isinstance(raw_message, bytes | str):
        message = EventAvailableMessage.model_validate_json(raw_message)
    else:
        message = EventAvailableMessage.model_validate(raw_message)
    return await processor.process(message, handler)


def build_event_relay_dependencies(settings: Settings) -> EventRelayDependencies:
    project_id = _required_pubsub_project(settings)
    database = Database(settings.database_url.get_secret_value())
    publisher = GooglePubSubPublisher(project_id)
    relay = OutboxRelay(database, publisher, settings.outbox_lease_seconds)
    worker = OutboxRelayWorker(
        relay,
        batch_limit=settings.outbox_batch_limit,
        poll_seconds=settings.outbox_relay_poll_seconds,
    )
    return EventRelayDependencies(database, publisher, relay, worker)


def build_relevance_dependencies(
    settings: Settings,
    *,
    include_pull_worker: bool,
    classifier: RelevanceClassifier | None = None,
) -> RelevanceDependencies:
    if not settings.relevance_enabled:
        raise ValueError("relevance processing is disabled")
    database = Database(settings.database_url.get_secret_value())
    repository = RelevanceRepository(database)
    openai_client: AsyncOpenAI | None = None
    if classifier is None:
        api_key = settings.openai_api_key
        if api_key is None or not api_key.get_secret_value().strip():
            raise ValueError("OpenAI configuration is incomplete")
        openai_client = AsyncOpenAI(api_key=api_key.get_secret_value())
        classifier = OpenAIRelevanceClassifier(
            cast(OpenAIClient, openai_client), settings.relevance_model
        )
    runner = RelevanceClassifierRunner(
        classifier=classifier,
        attempts=repository,
        provider=settings.relevance_provider.value,
        model=settings.relevance_model,
        classifier_version=settings.relevance_classifier_version,
        max_attempts=settings.relevance_retry_attempts,
        initial_backoff_seconds=settings.relevance_retry_initial_backoff_seconds,
        max_backoff_seconds=settings.relevance_retry_max_backoff_seconds,
        jitter_ratio=settings.relevance_retry_jitter_ratio,
    )
    context = RelevanceContextBuilder(
        repository,
        ContextBounds(
            body_max_chars=settings.relevance_body_max_chars,
            goal_limit=settings.relevance_goal_limit,
            goal_max_chars=settings.relevance_goal_max_chars,
            goal_total_chars=settings.relevance_goal_total_chars,
            situation_limit=settings.relevance_situation_limit,
            situation_max_chars=settings.relevance_situation_max_chars,
            situation_total_chars=settings.relevance_situation_total_chars,
        ),
    )
    policy = RelevanceRoutingPolicy(
        RoutingThresholds(
            notify_relevance=settings.relevance_notify_relevance,
            notify_confidence=settings.relevance_notify_confidence,
            notify_importance_or_urgency=settings.relevance_notify_importance_or_urgency,
            investigate_relevance=settings.relevance_investigate_relevance,
            investigate_confidence=settings.relevance_investigate_confidence,
            ignore_relevance=settings.relevance_ignore_relevance,
            ignore_confidence=settings.relevance_ignore_confidence,
        ),
        settings.relevance_policy_version,
    )
    handler = RelevanceEventHandler(
        signals=repository,
        situations=SituationRepository(database),
        rules=StaticRelevanceRuleProvider(
            RelevanceRuleSet(
                ignored_sources=settings.relevance_ignored_sources,
                ignored_event_types=settings.relevance_ignored_event_types,
                ignored_senders=settings.relevance_ignored_senders,
                ignored_labels=settings.relevance_ignored_labels,
            )
        ),
        context_builder=context,
        classifier=runner,
        policy=policy,
        classifier_version=settings.relevance_classifier_version,
    )
    service = RelevanceService(database, repository, handler)
    subscriber = None
    worker = None
    if include_pull_worker:
        project_id = _required_pubsub_project(settings)
        subscriber = GooglePullSubscriber(project_id, settings.relevance_subscription_id)
        worker = RelevancePullWorker(
            subscriber,
            EventProcessor(database, settings.processing_lease_seconds),
            handler,
            settings.relevance_pull_timeout_seconds,
        )
    return RelevanceDependencies(database, handler, service, subscriber, worker, openai_client)


def _required_pubsub_project(settings: Settings) -> str:
    project_id = settings.pubsub_project_id
    if project_id is None or not project_id.strip():
        raise ValueError("Pub/Sub project configuration is incomplete")
    return project_id.strip()
