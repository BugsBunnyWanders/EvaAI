import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from openai import AsyncOpenAI

from eva_ai.agent.repository import AgentRunRepository
from eva_ai.agent.service import AgentInvestigationService
from eva_ai.agent.worker import AgentPullWorker
from eva_ai.config import Settings
from eva_ai.conversation.repository import ConversationRepository
from eva_ai.conversation.service import ConversationService
from eva_ai.conversation.worker import ConversationPullWorker
from eva_ai.db.session import Database
from eva_ai.events.outbox import OutboxRelay
from eva_ai.events.processor import EventHandler, EventProcessor, ProcessResult
from eva_ai.events.publisher import InMemoryPublisher, Publisher
from eva_ai.events.relay_worker import OutboxRelayWorker
from eva_ai.events.types import EventAvailableMessage
from eva_ai.integrations.gcp.pubsub import GooglePubSubPublisher
from eva_ai.integrations.gcp.secret_manager import GoogleSecretManagerCredentialStore
from eva_ai.integrations.gcp.subscriber import GooglePullSubscriber
from eva_ai.integrations.gmail.api import GoogleGmailClientFactory
from eva_ai.integrations.openai.agent import OpenAIAgentsInvestigationAgent
from eva_ai.integrations.openai.conversation import OpenAIAgentsConversationAgent
from eva_ai.integrations.openai.memory import OpenAIEmbeddingClient, OpenAIEmbeddingProvider
from eva_ai.integrations.openai.relevance import OpenAIClient, OpenAIRelevanceClassifier
from eva_ai.integrations.telegram.api import TelegramBotAPI
from eva_ai.memory.context import ContextBounds as MemoryContextBounds
from eva_ai.memory.context import MemoryContextBuilder
from eva_ai.memory.embedding import EmbeddingService
from eva_ai.memory.policy import MemoryPolicy
from eva_ai.memory.repository import MemoryRepository
from eva_ai.memory.service import MemoryService
from eva_ai.notifications.repository import NotificationRepository
from eva_ai.notifications.service import NotificationDeliveryService
from eva_ai.notifications.worker import NotificationDeliveryPullWorker
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


@dataclass(slots=True)
class MemoryDependencies:
    database: Database
    service: MemoryService
    context_builder: MemoryContextBuilder | None
    openai_client: AsyncOpenAI | None

    async def close(self) -> DependencyCleanupOutcome:
        interruption: BaseException | None = None
        ordinary_failure = False
        close_functions = []
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


@dataclass(slots=True)
class AgentDependencies:
    database: Database
    credential_store: GoogleSecretManagerCredentialStore
    gmail_client_factory: GoogleGmailClientFactory
    subscriber: GooglePullSubscriber
    openai_client: AsyncOpenAI
    service: AgentInvestigationService
    worker: AgentPullWorker

    async def close(self) -> DependencyCleanupOutcome:
        interruption: BaseException | None = None
        ordinary_failure = False
        for close in (
            self.subscriber.close,
            self.gmail_client_factory.close,
            self.credential_store.close,
            self.openai_client.close,
            self.database.close,
        ):
            try:
                await close()
            except asyncio.CancelledError as error:
                interruption = interruption or error
            except Exception:
                ordinary_failure = True
        return DependencyCleanupOutcome(interruption, ordinary_failure)


@dataclass(slots=True)
class ConversationDependencies:
    database: Database
    credential_store: GoogleSecretManagerCredentialStore
    gmail_client_factory: GoogleGmailClientFactory
    subscriber: GooglePullSubscriber
    openai_client: AsyncOpenAI
    service: ConversationService
    worker: ConversationPullWorker

    async def close(self) -> DependencyCleanupOutcome:
        interruption: BaseException | None = None
        ordinary_failure = False
        for close in (
            self.subscriber.close,
            self.gmail_client_factory.close,
            self.credential_store.close,
            self.openai_client.close,
            self.database.close,
        ):
            try:
                await close()
            except asyncio.CancelledError as error:
                interruption = interruption or error
            except Exception:
                ordinary_failure = True
        return DependencyCleanupOutcome(interruption, ordinary_failure)


@dataclass(slots=True)
class DeliveryDependencies:
    database: Database
    subscriber: GooglePullSubscriber
    telegram: TelegramBotAPI
    service: NotificationDeliveryService
    worker: NotificationDeliveryPullWorker

    async def close(self) -> DependencyCleanupOutcome:
        interruption: BaseException | None = None
        ordinary_failure = False
        for close in (self.subscriber.close, self.telegram.close, self.database.close):
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
        agent_runs=AgentRunRepository(database) if settings.agent_enabled else None,
        agent_destination=settings.agent_topic_id,
        agent_model=settings.agent_model,
        agent_version=settings.agent_version,
        agent_prompt_version=settings.agent_prompt_version,
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


def build_memory_dependencies(settings: Settings, *, include_embedding: bool) -> MemoryDependencies:
    api_key = settings.openai_api_key
    if include_embedding and (api_key is None or not api_key.get_secret_value().strip()):
        # Validate provider configuration before allocating resources that a failed build cannot
        # return to its caller for cleanup.
        raise ValueError("OpenAI configuration is incomplete")
    database = Database(settings.database_url.get_secret_value())
    repository = MemoryRepository(database)
    openai_client = None
    embedding = None
    context_builder = None
    if include_embedding:
        assert api_key is not None
        openai_client = AsyncOpenAI(api_key=api_key.get_secret_value())
        provider = OpenAIEmbeddingProvider(
            cast(OpenAIEmbeddingClient, openai_client),
            settings.memory_embedding_model,
            settings.memory_embedding_dimensions,
        )
        embedding = EmbeddingService(
            provider,
            settings.memory_embedding_model,
            settings.memory_embedding_dimensions,
            settings.memory_embedding_input_max_chars,
            _utc_now,
        )
        context_builder = MemoryContextBuilder(
            repository,
            embedding,
            MemoryContextBounds(
                fact_limit=settings.memory_fact_limit,
                fact_total_chars=settings.memory_fact_total_chars,
                episode_candidate_limit=settings.memory_episode_candidate_limit,
                episode_limit=settings.memory_episode_limit,
                episode_total_chars=settings.memory_episode_total_chars,
                query_max_chars=settings.memory_embedding_input_max_chars,
            ),
            _utc_now,
        )
    service = MemoryService(
        repository,
        MemoryPolicy(),
        embedding,
        episode_candidate_limit=settings.memory_episode_candidate_limit,
        episode_limit=settings.memory_episode_limit,
        clock=_utc_now,
    )
    return MemoryDependencies(database, service, context_builder, openai_client)


def build_agent_dependencies(settings: Settings) -> AgentDependencies:
    if not settings.agent_enabled:
        raise ValueError("agent investigation is disabled")
    project_id = _required_pubsub_project(settings)
    api_key = settings.openai_api_key
    if api_key is None or not api_key.get_secret_value().strip():
        raise ValueError("OpenAI configuration is incomplete")
    database = Database(settings.database_url.get_secret_value())
    openai_client = AsyncOpenAI(api_key=api_key.get_secret_value())
    memory_repository = MemoryRepository(database)
    embedding = EmbeddingService(
        OpenAIEmbeddingProvider(
            cast(OpenAIEmbeddingClient, openai_client),
            settings.memory_embedding_model,
            settings.memory_embedding_dimensions,
        ),
        settings.memory_embedding_model,
        settings.memory_embedding_dimensions,
        settings.memory_embedding_input_max_chars,
        _utc_now,
    )
    context_builder = MemoryContextBuilder(
        memory_repository,
        embedding,
        MemoryContextBounds(
            fact_limit=settings.memory_fact_limit,
            fact_total_chars=settings.memory_fact_total_chars,
            episode_candidate_limit=settings.memory_episode_candidate_limit,
            episode_limit=settings.memory_episode_limit,
            episode_total_chars=settings.memory_episode_total_chars,
            query_max_chars=settings.memory_embedding_input_max_chars,
        ),
        _utc_now,
    )
    credential_store = GoogleSecretManagerCredentialStore(project_id)
    gmail_client_factory = GoogleGmailClientFactory(
        request_timeout_seconds=settings.gmail_request_timeout_seconds,
        retry_attempts=settings.gmail_retry_attempts,
        retry_initial_backoff_seconds=settings.gmail_retry_initial_backoff_seconds,
        retry_max_backoff_seconds=settings.gmail_retry_max_backoff_seconds,
        retry_jitter_ratio=settings.gmail_retry_jitter_ratio,
    )
    agent = OpenAIAgentsInvestigationAgent(
        openai_client,
        model=settings.agent_model,
        reasoning_effort=settings.agent_reasoning_effort,
        max_turns=settings.agent_max_turns,
        max_tool_calls=settings.agent_max_tool_calls,
        tool_timeout_seconds=settings.agent_tool_timeout_seconds,
    )
    service = AgentInvestigationService(
        runs=AgentRunRepository(
            database,
            notification_destination=(
                settings.telegram_delivery_topic_id if settings.telegram_enabled else None
            ),
        ),
        context_builder=context_builder,
        credential_store=credential_store,
        gmail_clients=gmail_client_factory,
        agent=agent,
        lease_seconds=settings.agent_lease_seconds,
        max_attempts=settings.agent_max_attempts,
        retry_initial_backoff_seconds=settings.agent_retry_initial_backoff_seconds,
        retry_max_backoff_seconds=settings.agent_retry_max_backoff_seconds,
        thread_message_limit=settings.agent_thread_message_limit,
        search_result_limit=settings.agent_search_result_limit,
        body_max_chars=settings.agent_message_body_max_chars,
    )
    subscriber = GooglePullSubscriber(project_id, settings.agent_subscription_id)
    worker = AgentPullWorker(subscriber, service, settings.agent_pull_timeout_seconds)
    return AgentDependencies(
        database=database,
        credential_store=credential_store,
        gmail_client_factory=gmail_client_factory,
        subscriber=subscriber,
        openai_client=openai_client,
        service=service,
        worker=worker,
    )


def build_conversation_dependencies(settings: Settings) -> ConversationDependencies:
    if not settings.telegram_enabled:
        raise ValueError("Telegram conversation is disabled")
    project_id = _required_pubsub_project(settings)
    api_key = settings.openai_api_key
    if api_key is None or not api_key.get_secret_value().strip():
        raise ValueError("OpenAI configuration is incomplete")
    database = Database(settings.database_url.get_secret_value())
    openai_client = AsyncOpenAI(api_key=api_key.get_secret_value())
    memory_repository = MemoryRepository(database)
    embedding = EmbeddingService(
        OpenAIEmbeddingProvider(
            cast(OpenAIEmbeddingClient, openai_client),
            settings.memory_embedding_model,
            settings.memory_embedding_dimensions,
        ),
        settings.memory_embedding_model,
        settings.memory_embedding_dimensions,
        settings.memory_embedding_input_max_chars,
        _utc_now,
    )
    context_builder = MemoryContextBuilder(
        memory_repository,
        embedding,
        MemoryContextBounds(
            fact_limit=settings.memory_fact_limit,
            fact_total_chars=settings.memory_fact_total_chars,
            episode_candidate_limit=settings.memory_episode_candidate_limit,
            episode_limit=settings.memory_episode_limit,
            episode_total_chars=settings.memory_episode_total_chars,
            query_max_chars=settings.memory_embedding_input_max_chars,
        ),
        _utc_now,
    )
    credential_store = GoogleSecretManagerCredentialStore(project_id)
    gmail_client_factory = GoogleGmailClientFactory(
        request_timeout_seconds=settings.gmail_request_timeout_seconds,
        retry_attempts=settings.gmail_retry_attempts,
        retry_initial_backoff_seconds=settings.gmail_retry_initial_backoff_seconds,
        retry_max_backoff_seconds=settings.gmail_retry_max_backoff_seconds,
        retry_jitter_ratio=settings.gmail_retry_jitter_ratio,
    )
    agent = OpenAIAgentsConversationAgent(
        openai_client,
        model=settings.conversation_model,
        reasoning_effort=settings.conversation_reasoning_effort,
        max_turns=settings.conversation_max_turns,
        max_tool_calls=settings.conversation_max_tool_calls,
        tool_timeout_seconds=settings.agent_tool_timeout_seconds,
    )
    service = ConversationService(
        conversations=ConversationRepository(database, settings.telegram_delivery_topic_id),
        context_builder=context_builder,
        credential_store=credential_store,
        gmail_clients=gmail_client_factory,
        agent=agent,
        agent_version=settings.conversation_agent_version,
        lease_seconds=settings.telegram_lease_seconds,
        max_attempts=settings.telegram_max_attempts,
        retry_initial_backoff_seconds=settings.telegram_retry_initial_backoff_seconds,
        retry_max_backoff_seconds=settings.telegram_retry_max_backoff_seconds,
        history_turn_limit=settings.conversation_history_turn_limit,
        history_max_chars=settings.conversation_history_max_chars,
        thread_message_limit=settings.agent_thread_message_limit,
        search_result_limit=settings.agent_search_result_limit,
        body_max_chars=settings.agent_message_body_max_chars,
    )
    subscriber = GooglePullSubscriber(project_id, settings.telegram_turn_subscription_id)
    worker = ConversationPullWorker(subscriber, service, settings.telegram_pull_timeout_seconds)
    return ConversationDependencies(
        database=database,
        credential_store=credential_store,
        gmail_client_factory=gmail_client_factory,
        subscriber=subscriber,
        openai_client=openai_client,
        service=service,
        worker=worker,
    )


def build_delivery_dependencies(settings: Settings) -> DeliveryDependencies:
    if not settings.telegram_enabled:
        raise ValueError("Telegram delivery is disabled")
    project_id = _required_pubsub_project(settings)
    token = settings.telegram_bot_token
    if token is None or not token.get_secret_value().strip():
        raise ValueError("Telegram bot token is unavailable")
    database = Database(settings.database_url.get_secret_value())
    subscriber = GooglePullSubscriber(project_id, settings.telegram_delivery_subscription_id)
    telegram = TelegramBotAPI(
        token.get_secret_value(), timeout_seconds=settings.gmail_request_timeout_seconds
    )
    service = NotificationDeliveryService(
        notifications=NotificationRepository(database),
        telegram=telegram,
        lease_seconds=settings.telegram_lease_seconds,
        max_attempts=settings.telegram_max_attempts,
        retry_initial_backoff_seconds=settings.telegram_retry_initial_backoff_seconds,
        retry_max_backoff_seconds=settings.telegram_retry_max_backoff_seconds,
    )
    worker = NotificationDeliveryPullWorker(
        subscriber, service, settings.telegram_pull_timeout_seconds
    )
    return DeliveryDependencies(database, subscriber, telegram, service, worker)


def _required_pubsub_project(settings: Settings) -> str:
    project_id = settings.pubsub_project_id
    if project_id is None or not project_id.strip():
        raise ValueError("Pub/Sub project configuration is incomplete")
    return project_id.strip()


def _utc_now() -> datetime:
    return datetime.now(UTC)
