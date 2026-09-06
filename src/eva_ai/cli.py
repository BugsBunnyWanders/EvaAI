import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import TextIO, cast
from uuid import UUID, uuid7

from pydantic import BaseModel, JsonValue

from eva_ai.config import Settings, get_settings
from eva_ai.connectors.gmail.bootstrap import ConnectGmail, GmailBootstrapService
from eva_ai.connectors.gmail.maintenance import GmailMaintenanceService, MaintenanceSummary
from eva_ai.connectors.gmail.sync import (
    GmailRecoveryService,
    GmailSyncService,
    SyncStatus,
)
from eva_ai.connectors.gmail.worker import GmailPullWorker
from eva_ai.connectors.repository import ConnectorRepository
from eva_ai.connectors.types import ConnectorStatus
from eva_ai.db.session import Database
from eva_ai.events.service import EventService
from eva_ai.goals import (
    GoalDraft,
    GoalMode,
    GoalRepository,
    GoalService,
    GoalStatus,
    GoalUpdate,
    JsonObject,
)
from eva_ai.integrations.gcp.secret_manager import GoogleSecretManagerCredentialStore
from eva_ai.integrations.gcp.subscriber import GooglePullSubscriber
from eva_ai.integrations.gmail.api import GoogleGmailClientFactory
from eva_ai.integrations.gmail.oauth import GoogleDesktopOAuthAuthorizer
from eva_ai.local_scope import LocalScope, create_local_scope, local_scope_exists
from eva_ai.logging import configure_logging
from eva_ai.memory.context import serialize_context
from eva_ai.memory.policy import MemoryPolicy
from eva_ai.memory.repository import MemoryRepository
from eva_ai.memory.service import MemoryService
from eva_ai.memory.types import (
    AgentWorkingContext,
    EpisodicMemoryDraft,
    EpisodicMemoryStatus,
    MemoryEpisodeType,
    MemoryFactDraft,
    MemoryFactStatus,
    MemoryScopeType,
    MemorySourceType,
)
from eva_ai.relevance.repository import RelevanceRepository
from eva_ai.relevance.types import ReevaluateEvent
from eva_ai.situations import SituationLifecycle, SituationRepository, SituationService
from eva_ai.worker import (
    MemoryDependencies,
    build_event_relay_dependencies,
    build_memory_dependencies,
    build_relevance_dependencies,
)

ScopeCreateCommand = Callable[[str, str], Awaitable[None]]
GmailConnectCommand = Callable[[UUID, UUID], Awaitable[None]]
GmailSyncCommand = Callable[[UUID], Awaitable[None]]
NoArgumentCommand = Callable[[], Awaitable[None]]
GoalCreateCommand = Callable[[GoalDraft], Awaitable[None]]
GoalListCommand = Callable[[UUID, UUID, tuple[GoalStatus, ...], int], Awaitable[None]]
GoalShowCommand = Callable[[UUID, UUID, UUID], Awaitable[None]]
GoalUpdateCommand = Callable[[GoalUpdate], Awaitable[None]]
SituationListCommand = Callable[[UUID, UUID, tuple[SituationLifecycle, ...], int], Awaitable[None]]
SituationShowCommand = Callable[[UUID, UUID, UUID], Awaitable[None]]
RelevanceShowCommand = Callable[[UUID, UUID, UUID], Awaitable[None]]
RelevanceHistoryCommand = Callable[[UUID, UUID, UUID], Awaitable[None]]
RelevanceReevaluateCommand = Callable[[UUID, UUID, UUID, str, UUID], Awaitable[None]]
RelevanceBackfillCommand = Callable[[UUID, UUID, int], Awaitable[None]]
MemoryFactPutCommand = Callable[[MemoryFactDraft], Awaitable[None]]
MemoryFactListCommand = Callable[[UUID, UUID, tuple[MemoryFactStatus, ...], int], Awaitable[None]]
MemoryRecordCommand = Callable[[UUID, UUID, UUID], Awaitable[None]]
MemoryEpisodeCreateCommand = Callable[[EpisodicMemoryDraft], Awaitable[None]]
MemoryEpisodeListCommand = Callable[
    [UUID, UUID, tuple[EpisodicMemoryStatus, ...], int], Awaitable[None]
]
MemoryEpisodeSearchCommand = Callable[[UUID, UUID, str, int], Awaitable[None]]
ContextBuildCommand = Callable[[UUID, UUID, UUID, str | None], Awaitable[None]]
DatabaseFactory = Callable[[str], Database]
DependencyBuilder = Callable[[Settings], "GmailDependencies"]
MemoryDependencyBuilder = Callable[..., MemoryDependencies]
ScopeCreator = Callable[..., Awaitable[LocalScope]]
ScopeValidator = Callable[[Database, UUID, UUID], Awaitable[bool]]
Clock = Callable[[], datetime]
_LOGGER = logging.getLogger(__name__)


class CliValidationError(ValueError):
    """A fixed, content-free local command validation failure."""


class CliResourceError(RuntimeError):
    """A fixed, content-free local resource cleanup failure."""


@dataclass(frozen=True, slots=True)
class CleanupOutcome:
    interruption: BaseException | None = None
    ordinary_failure: bool = False


async def _unavailable_command(*arguments: object) -> None:
    raise CliValidationError("Command is unavailable")


@dataclass(frozen=True, slots=True)
class CommandFunctions:
    scope_create: ScopeCreateCommand
    gmail_connect: GmailConnectCommand
    gmail_sync: GmailSyncCommand
    gmail_pull: NoArgumentCommand
    gmail_maintain: NoArgumentCommand
    goal_create: GoalCreateCommand
    goal_list: GoalListCommand
    goal_show: GoalShowCommand
    goal_update: GoalUpdateCommand
    situation_list: SituationListCommand
    situation_show: SituationShowCommand
    events_relay: NoArgumentCommand = _unavailable_command
    relevance_pull: NoArgumentCommand = _unavailable_command
    relevance_show: RelevanceShowCommand = _unavailable_command
    relevance_history: RelevanceHistoryCommand = _unavailable_command
    relevance_reevaluate: RelevanceReevaluateCommand = _unavailable_command
    relevance_backfill: RelevanceBackfillCommand = _unavailable_command
    memory_fact_put: MemoryFactPutCommand = _unavailable_command
    memory_fact_list: MemoryFactListCommand = _unavailable_command
    memory_fact_show: MemoryRecordCommand = _unavailable_command
    memory_fact_retract: MemoryRecordCommand = _unavailable_command
    memory_episode_create: MemoryEpisodeCreateCommand = _unavailable_command
    memory_episode_list: MemoryEpisodeListCommand = _unavailable_command
    memory_episode_show: MemoryRecordCommand = _unavailable_command
    memory_episode_retract: MemoryRecordCommand = _unavailable_command
    memory_episode_search: MemoryEpisodeSearchCommand = _unavailable_command
    context_build: ContextBuildCommand = _unavailable_command


@dataclass(slots=True)
class GmailDependencies:
    database: Database
    credential_store: GoogleSecretManagerCredentialStore
    client_factory: GoogleGmailClientFactory
    subscriber: GooglePullSubscriber
    bootstrap: GmailBootstrapService
    sync_service: GmailSyncService
    maintenance: GmailMaintenanceService
    worker: GmailPullWorker

    async def close(self) -> CleanupOutcome:
        interruption: BaseException | None = None
        ordinary_failure = False
        # Each close is independently attempted so one provider cannot strand later resources.
        for close in (
            self.subscriber.close,
            self.client_factory.close,
            self.credential_store.close,
            self.database.close,
        ):
            try:
                await close()
            except asyncio.CancelledError as error:
                if interruption is None:
                    interruption = error
            except Exception:
                ordinary_failure = True
            except BaseException as error:
                if interruption is None:
                    interruption = error
        return CleanupOutcome(interruption, ordinary_failure)


def build_gmail_dependencies(settings: Settings) -> GmailDependencies:
    project_id = _required_project_id(settings)
    topic_name = _qualified_topic(project_id, settings.gmail_topic_id)
    database = Database(settings.database_url.get_secret_value())
    repository = ConnectorRepository(database)
    credential_store = GoogleSecretManagerCredentialStore(project_id)
    client_factory = GoogleGmailClientFactory(
        request_timeout_seconds=settings.gmail_request_timeout_seconds,
        retry_attempts=settings.gmail_retry_attempts,
        retry_initial_backoff_seconds=settings.gmail_retry_initial_backoff_seconds,
        retry_max_backoff_seconds=settings.gmail_retry_max_backoff_seconds,
        retry_jitter_ratio=settings.gmail_retry_jitter_ratio,
    )
    authorizer = GoogleDesktopOAuthAuthorizer()
    event_service = EventService(database, settings.pubsub_topic_id)
    clock = _utc_now
    watch_interval = timedelta(hours=settings.gmail_watch_renewal_hours)
    safety_interval = timedelta(minutes=settings.gmail_safety_sync_minutes)
    recovery = GmailRecoveryService(
        repository,
        event_service,
        topic_name,
        safety_sync_interval=safety_interval,
        watch_renewal_interval=watch_interval,
    )
    sync_service = GmailSyncService(
        repository,
        credential_store,
        client_factory,
        event_service,
        clock,
        settings.gmail_sync_lease_seconds,
        recovery,
        safety_sync_interval=safety_interval,
    )
    maintenance = GmailMaintenanceService(
        repository,
        credential_store,
        client_factory,
        sync_service,
        topic_name,
        settings.gmail_sync_lease_seconds,
        watch_renewal_interval=watch_interval,
        safety_sync_interval=safety_interval,
    )
    subscriber = GooglePullSubscriber(
        project_id,
        _required_subscription_id(settings),
    )
    worker = GmailPullWorker(
        subscriber,
        sync_service,
        maintenance,
        clock,
        settings.gmail_pull_timeout_seconds,
    )
    bootstrap = GmailBootstrapService(
        repository,
        authorizer,
        credential_store,
        client_factory,
        clock,
        watch_renewal_interval=watch_interval,
        safety_sync_interval=safety_interval,
    )
    return GmailDependencies(
        database=database,
        credential_store=credential_store,
        client_factory=client_factory,
        subscriber=subscriber,
        bootstrap=bootstrap,
        sync_service=sync_service,
        maintenance=maintenance,
        worker=worker,
    )


async def scope_create_command(
    display_name: str,
    workspace_name: str,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    scope_creator: ScopeCreator = create_local_scope,
    stdout: TextIO | None = None,
) -> None:
    database = database_factory(settings.database_url.get_secret_value())
    primary_failure: BaseException | None = None
    scope: LocalScope | None = None
    try:
        scope = await scope_creator(
            database,
            display_name=display_name,
            workspace_name=workspace_name,
        )
    except BaseException as error:
        primary_failure = error
    cleanup_failed = await _close_database(database)
    _raise_after_cleanup(primary_failure, cleanup_failed)
    assert scope is not None
    output = stdout or sys.stdout
    print(scope.user_id, file=output)
    print(scope.workspace_id, file=output)


async def gmail_connect_command(
    user_id: UUID,
    workspace_id: UUID,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    scope_validator: ScopeValidator = local_scope_exists,
    dependency_builder: DependencyBuilder = build_gmail_dependencies,
    stdout: TextIO | None = None,
) -> None:
    project_id, account, client_file = _connect_configuration(settings)
    validation_database = database_factory(settings.database_url.get_secret_value())
    primary_failure: BaseException | None = None
    scope_is_valid = False
    try:
        scope_is_valid = await scope_validator(validation_database, user_id, workspace_id)
    except BaseException as error:
        primary_failure = error
    cleanup_failed = await _close_database(validation_database)
    _raise_after_cleanup(primary_failure, cleanup_failed)
    if not scope_is_valid:
        raise CliValidationError("User and Workspace scope is unavailable")

    dependencies = dependency_builder(settings)
    connector = None
    primary_failure = None
    try:
        connector = await dependencies.bootstrap.connect(
            ConnectGmail(
                user_id=user_id,
                workspace_id=workspace_id,
                expected_identity=account,
                client_file=client_file,
                topic_name=_qualified_topic(project_id, settings.gmail_topic_id),
            )
        )
    except BaseException as error:
        primary_failure = error
    cleanup_failed = await dependencies.close()
    _raise_after_cleanup(primary_failure, cleanup_failed)
    assert connector is not None
    if connector.status is not ConnectorStatus.ACTIVE:
        raise CliValidationError("Gmail connection did not become active")
    print(connector.id, file=stdout or sys.stdout)


async def gmail_sync_command(
    connector_id: UUID,
    *,
    settings: Settings,
    dependency_builder: DependencyBuilder = build_gmail_dependencies,
) -> None:
    _validate_runtime_configuration(settings, require_subscription=False)
    dependencies = dependency_builder(settings)
    primary_failure: BaseException | None = None
    result = None
    try:
        result = await dependencies.sync_service.sync_connector(connector_id)
    except BaseException as error:
        primary_failure = error
    cleanup_failed = await dependencies.close()
    _raise_after_cleanup(primary_failure, cleanup_failed)
    if result is None or result.status is not SyncStatus.SYNCED:
        raise CliValidationError("Gmail synchronization did not complete")


async def gmail_pull_command(
    *,
    settings: Settings,
    dependency_builder: DependencyBuilder = build_gmail_dependencies,
) -> None:
    _validate_runtime_configuration(settings, require_subscription=True)
    dependencies = dependency_builder(settings)
    primary_failure: BaseException | None = None
    try:
        await dependencies.worker.run_forever()
    except BaseException as error:
        primary_failure = error
    cleanup_failed = await dependencies.close()
    _raise_after_cleanup(primary_failure, cleanup_failed)


async def gmail_maintain_command(
    *,
    settings: Settings,
    dependency_builder: DependencyBuilder = build_gmail_dependencies,
    clock: Clock | None = None,
) -> None:
    _validate_runtime_configuration(settings, require_subscription=False)
    dependencies = dependency_builder(settings)
    primary_failure: BaseException | None = None
    summary: MaintenanceSummary | None = None
    try:
        summary = await dependencies.maintenance.run_due((clock or _utc_now)())
    except BaseException as error:
        primary_failure = error
    cleanup_failed = await dependencies.close()
    _raise_after_cleanup(primary_failure, cleanup_failed)
    if summary is None or summary.failed > 0:
        raise CliValidationError("Gmail maintenance reported failures")


async def goal_create_command(
    command: GoalDraft,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    stdout: TextIO | None = None,
) -> None:
    async def create(database: Database) -> BaseModel:
        return await GoalService(GoalRepository(database)).create_explicit(command)

    _write_json(await _run_database_operation(settings, database_factory, create), stdout)


async def goal_list_command(
    user_id: UUID,
    workspace_id: UUID,
    statuses: tuple[GoalStatus, ...],
    limit: int,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    stdout: TextIO | None = None,
) -> None:
    async def list_goals(database: Database) -> tuple[BaseModel, ...]:
        return await GoalService(GoalRepository(database)).list(
            user_id=user_id,
            workspace_id=workspace_id,
            statuses=statuses,
            limit=limit,
        )

    records = await _run_database_operation(settings, database_factory, list_goals)
    _write_json({"count": len(records), "items": records}, stdout)


async def goal_show_command(
    user_id: UUID,
    workspace_id: UUID,
    goal_id: UUID,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    stdout: TextIO | None = None,
) -> None:
    async def show(database: Database) -> BaseModel:
        return await GoalService(GoalRepository(database)).get(
            user_id=user_id,
            workspace_id=workspace_id,
            goal_id=goal_id,
        )

    _write_json(await _run_database_operation(settings, database_factory, show), stdout)


async def goal_update_command(
    command: GoalUpdate,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    stdout: TextIO | None = None,
) -> None:
    async def update(database: Database) -> BaseModel:
        return await GoalService(GoalRepository(database)).update(command)

    _write_json(await _run_database_operation(settings, database_factory, update), stdout)


async def situation_list_command(
    user_id: UUID,
    workspace_id: UUID,
    lifecycles: tuple[SituationLifecycle, ...],
    limit: int,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    stdout: TextIO | None = None,
) -> None:
    async def list_situations(database: Database) -> tuple[BaseModel, ...]:
        return await SituationService(SituationRepository(database)).list(
            user_id=user_id,
            workspace_id=workspace_id,
            lifecycles=lifecycles,
            limit=limit,
        )

    records = await _run_database_operation(settings, database_factory, list_situations)
    _write_json({"count": len(records), "items": records}, stdout)


async def situation_show_command(
    user_id: UUID,
    workspace_id: UUID,
    situation_id: UUID,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    stdout: TextIO | None = None,
) -> None:
    async def show(database: Database) -> dict[str, object]:
        repository = SituationRepository(database)
        situation = await SituationService(repository).get(
            user_id=user_id,
            workspace_id=workspace_id,
            situation_id=situation_id,
        )
        # Projection records expose linkage metadata without leaking the raw Event payload.
        event_links = await repository.list_events(
            user_id=user_id,
            workspace_id=workspace_id,
            situation_id=situation_id,
        )
        goal_links = await repository.list_goals(
            user_id=user_id,
            workspace_id=workspace_id,
            situation_id=situation_id,
        )
        return {
            "event_links": event_links,
            "goal_links": goal_links,
            "situation": situation,
        }

    _write_json(await _run_database_operation(settings, database_factory, show), stdout)


async def events_relay_command(*, settings: Settings) -> None:
    dependencies = build_event_relay_dependencies(settings)
    primary_failure: BaseException | None = None
    try:
        await dependencies.worker.run_forever()
    except BaseException as error:
        primary_failure = error
    cleanup = await dependencies.close()
    _raise_after_cleanup(
        primary_failure, CleanupOutcome(cleanup.interruption, cleanup.ordinary_failure)
    )


async def relevance_pull_command(*, settings: Settings) -> None:
    dependencies = build_relevance_dependencies(settings, include_pull_worker=True)
    if dependencies.worker is None:
        raise CliResourceError("Relevance pull worker is unavailable")
    primary_failure: BaseException | None = None
    try:
        await dependencies.worker.run_forever()
    except BaseException as error:
        primary_failure = error
    cleanup = await dependencies.close()
    _raise_after_cleanup(
        primary_failure, CleanupOutcome(cleanup.interruption, cleanup.ordinary_failure)
    )


async def relevance_show_command(
    user_id: UUID,
    workspace_id: UUID,
    event_id: UUID,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    stdout: TextIO | None = None,
) -> None:
    async def show(database: Database) -> BaseModel | None:
        return await RelevanceRepository(database).get_current(
            event_id=event_id, user_id=user_id, workspace_id=workspace_id
        )

    _write_json(await _run_database_operation(settings, database_factory, show), stdout)


async def relevance_history_command(
    user_id: UUID,
    workspace_id: UUID,
    event_id: UUID,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    stdout: TextIO | None = None,
) -> None:
    async def history(database: Database) -> dict[str, object]:
        repository = RelevanceRepository(database)
        signals = await repository.history(
            event_id=event_id, user_id=user_id, workspace_id=workspace_id
        )
        attempts = await repository.list_attempts(
            event_id=event_id, user_id=user_id, workspace_id=workspace_id
        )
        current = next((item for item in reversed(signals) if item.is_current), None)
        return {
            "attempts": attempts,
            "current_signal_id": current.id if current is not None else None,
            "signals": signals,
        }

    _write_json(await _run_database_operation(settings, database_factory, history), stdout)


async def relevance_reevaluate_command(
    user_id: UUID,
    workspace_id: UUID,
    event_id: UUID,
    reason: str,
    evaluation_key: UUID,
    *,
    settings: Settings,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    print(f"eva: relevance evaluation {evaluation_key}", file=stderr or sys.stderr)
    dependencies = build_relevance_dependencies(settings, include_pull_worker=False)
    primary_failure: BaseException | None = None
    result = None
    try:
        result = await dependencies.service.reevaluate(
            ReevaluateEvent(
                event_id=event_id,
                user_id=user_id,
                workspace_id=workspace_id,
                evaluation_key=evaluation_key,
                reason=reason,
                requested_at=_utc_now(),
            )
        )
    except BaseException as error:
        primary_failure = error
    cleanup = await dependencies.close()
    _raise_after_cleanup(
        primary_failure, CleanupOutcome(cleanup.interruption, cleanup.ordinary_failure)
    )
    assert result is not None
    _write_json(result, stdout)


async def relevance_backfill_command(
    user_id: UUID,
    workspace_id: UUID,
    limit: int,
    *,
    settings: Settings,
    stdout: TextIO | None = None,
) -> None:
    dependencies = build_relevance_dependencies(settings, include_pull_worker=False)
    primary_failure: BaseException | None = None
    result = None
    try:
        result = await dependencies.service.backfill(
            user_id=user_id, workspace_id=workspace_id, limit=limit
        )
    except BaseException as error:
        primary_failure = error
    cleanup = await dependencies.close()
    _raise_after_cleanup(
        primary_failure, CleanupOutcome(cleanup.interruption, cleanup.ordinary_failure)
    )
    assert result is not None
    _write_json(result, stdout)


def _memory_service(database: Database) -> MemoryService:
    return MemoryService(MemoryRepository(database), MemoryPolicy())


async def memory_fact_put_command(
    command: MemoryFactDraft,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    stdout: TextIO | None = None,
) -> None:
    async def put(database: Database) -> BaseModel:
        return await _memory_service(database).put_fact(command)

    _write_json(await _run_database_operation(settings, database_factory, put), stdout)


async def memory_fact_list_command(
    user_id: UUID,
    workspace_id: UUID,
    statuses: tuple[MemoryFactStatus, ...],
    limit: int,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    stdout: TextIO | None = None,
) -> None:
    async def list_facts(database: Database) -> tuple[BaseModel, ...]:
        return await _memory_service(database).list_facts(
            user_id=user_id,
            workspace_id=workspace_id,
            statuses=statuses,
            limit=limit,
        )

    records = await _run_database_operation(settings, database_factory, list_facts)
    _write_json({"count": len(records), "items": records}, stdout)


async def memory_fact_show_command(
    user_id: UUID,
    workspace_id: UUID,
    memory_id: UUID,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    stdout: TextIO | None = None,
) -> None:
    async def show(database: Database) -> BaseModel:
        return await _memory_service(database).get_fact(
            user_id=user_id,
            workspace_id=workspace_id,
            memory_id=memory_id,
        )

    _write_json(await _run_database_operation(settings, database_factory, show), stdout)


async def memory_fact_retract_command(
    user_id: UUID,
    workspace_id: UUID,
    memory_id: UUID,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    stdout: TextIO | None = None,
) -> None:
    async def retract(database: Database) -> BaseModel:
        return await _memory_service(database).retract_fact(
            user_id=user_id,
            workspace_id=workspace_id,
            memory_id=memory_id,
        )

    _write_json(await _run_database_operation(settings, database_factory, retract), stdout)


async def memory_episode_create_command(
    command: EpisodicMemoryDraft,
    *,
    settings: Settings,
    dependency_builder: MemoryDependencyBuilder = build_memory_dependencies,
    stdout: TextIO | None = None,
) -> None:
    async def create(dependencies: MemoryDependencies) -> BaseModel:
        return await dependencies.service.create_episode(command)

    result = await _run_memory_operation(settings, dependency_builder, create)
    _write_json(result, stdout)


async def memory_episode_list_command(
    user_id: UUID,
    workspace_id: UUID,
    statuses: tuple[EpisodicMemoryStatus, ...],
    limit: int,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    stdout: TextIO | None = None,
) -> None:
    async def list_episodes(database: Database) -> tuple[BaseModel, ...]:
        return await _memory_service(database).list_episodes(
            user_id=user_id,
            workspace_id=workspace_id,
            statuses=statuses,
            limit=limit,
        )

    records = await _run_database_operation(settings, database_factory, list_episodes)
    _write_json({"count": len(records), "items": records}, stdout)


async def memory_episode_show_command(
    user_id: UUID,
    workspace_id: UUID,
    memory_id: UUID,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    stdout: TextIO | None = None,
) -> None:
    async def show(database: Database) -> BaseModel:
        return await _memory_service(database).get_episode(
            user_id=user_id,
            workspace_id=workspace_id,
            memory_id=memory_id,
        )

    _write_json(await _run_database_operation(settings, database_factory, show), stdout)


async def memory_episode_retract_command(
    user_id: UUID,
    workspace_id: UUID,
    memory_id: UUID,
    *,
    settings: Settings,
    database_factory: DatabaseFactory = Database,
    stdout: TextIO | None = None,
) -> None:
    async def retract(database: Database) -> BaseModel:
        return await _memory_service(database).retract_episode(
            user_id=user_id,
            workspace_id=workspace_id,
            memory_id=memory_id,
        )

    _write_json(await _run_database_operation(settings, database_factory, retract), stdout)


async def memory_episode_search_command(
    user_id: UUID,
    workspace_id: UUID,
    query: str,
    limit: int,
    *,
    settings: Settings,
    dependency_builder: MemoryDependencyBuilder = build_memory_dependencies,
    stdout: TextIO | None = None,
) -> None:
    async def search(dependencies: MemoryDependencies) -> tuple[BaseModel, ...]:
        return await dependencies.service.search_episodes(
            user_id=user_id,
            workspace_id=workspace_id,
            query=query,
            limit=limit,
        )

    records = await _run_memory_operation(settings, dependency_builder, search)
    _write_json({"count": len(records), "items": records}, stdout)


async def context_build_command(
    user_id: UUID,
    workspace_id: UUID,
    situation_id: UUID,
    focus: str | None,
    *,
    settings: Settings,
    dependency_builder: MemoryDependencyBuilder = build_memory_dependencies,
    stdout: TextIO | None = None,
) -> None:
    async def build(dependencies: MemoryDependencies) -> AgentWorkingContext:
        if dependencies.context_builder is None:
            raise CliResourceError("Memory context builder is unavailable")
        return await dependencies.context_builder.build_for_situation(
            user_id=user_id,
            workspace_id=workspace_id,
            situation_id=situation_id,
            focus=focus,
        )

    context = await _run_memory_operation(settings, dependency_builder, build)
    # Context has a canonical serializer so its digest and rendered shape remain reproducible.
    print(serialize_context(context), file=stdout or sys.stdout)


def build_command_functions(settings: Settings) -> CommandFunctions:
    return CommandFunctions(
        scope_create=partial(scope_create_command, settings=settings),
        gmail_connect=partial(gmail_connect_command, settings=settings),
        gmail_sync=partial(gmail_sync_command, settings=settings),
        gmail_pull=partial(gmail_pull_command, settings=settings),
        gmail_maintain=partial(gmail_maintain_command, settings=settings),
        goal_create=partial(goal_create_command, settings=settings),
        goal_list=partial(goal_list_command, settings=settings),
        goal_show=partial(goal_show_command, settings=settings),
        goal_update=partial(goal_update_command, settings=settings),
        situation_list=partial(situation_list_command, settings=settings),
        situation_show=partial(situation_show_command, settings=settings),
        events_relay=partial(events_relay_command, settings=settings),
        relevance_pull=partial(relevance_pull_command, settings=settings),
        relevance_show=partial(relevance_show_command, settings=settings),
        relevance_history=partial(relevance_history_command, settings=settings),
        relevance_reevaluate=partial(relevance_reevaluate_command, settings=settings),
        relevance_backfill=partial(relevance_backfill_command, settings=settings),
        memory_fact_put=partial(memory_fact_put_command, settings=settings),
        memory_fact_list=partial(memory_fact_list_command, settings=settings),
        memory_fact_show=partial(memory_fact_show_command, settings=settings),
        memory_fact_retract=partial(memory_fact_retract_command, settings=settings),
        memory_episode_create=partial(memory_episode_create_command, settings=settings),
        memory_episode_list=partial(memory_episode_list_command, settings=settings),
        memory_episode_show=partial(memory_episode_show_command, settings=settings),
        memory_episode_retract=partial(memory_episode_retract_command, settings=settings),
        memory_episode_search=partial(memory_episode_search_command, settings=settings),
        context_build=partial(context_build_command, settings=settings),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eva")
    commands = parser.add_subparsers(dest="area", required=True)

    scope = commands.add_parser("scope")
    scope_commands = scope.add_subparsers(dest="scope_command", required=True)
    scope_create = scope_commands.add_parser("create")
    scope_create.add_argument("--display-name", required=True)
    scope_create.add_argument("--workspace-name", required=True)

    gmail = commands.add_parser("gmail")
    gmail_commands = gmail.add_subparsers(dest="gmail_command", required=True)
    gmail_connect = gmail_commands.add_parser("connect")
    gmail_connect.add_argument("--user-id", required=True, type=_parse_uuid)
    gmail_connect.add_argument("--workspace-id", required=True, type=_parse_uuid)
    gmail_sync = gmail_commands.add_parser("sync")
    gmail_sync.add_argument("--connector-id", required=True, type=_parse_uuid)
    gmail_commands.add_parser("pull")
    gmail_commands.add_parser("maintain")

    events = commands.add_parser("events")
    events_commands = events.add_subparsers(dest="events_command", required=True)
    events_commands.add_parser("relay")

    relevance = commands.add_parser("relevance")
    relevance_commands = relevance.add_subparsers(dest="relevance_command", required=True)
    relevance_commands.add_parser("pull")
    relevance_show = relevance_commands.add_parser("show")
    _add_scope_arguments(relevance_show)
    relevance_show.add_argument("--event-id", required=True, type=_parse_uuid)
    relevance_history = relevance_commands.add_parser("history")
    _add_scope_arguments(relevance_history)
    relevance_history.add_argument("--event-id", required=True, type=_parse_uuid)
    relevance_reevaluate = relevance_commands.add_parser("reevaluate")
    _add_scope_arguments(relevance_reevaluate)
    relevance_reevaluate.add_argument("--event-id", required=True, type=_parse_uuid)
    relevance_reevaluate.add_argument("--reason", required=True)
    relevance_reevaluate.add_argument("--idempotency-key", type=_parse_uuid, default=None)
    relevance_backfill = relevance_commands.add_parser("backfill")
    _add_scope_arguments(relevance_backfill)
    relevance_backfill.add_argument("--limit", type=_parse_limit, default=50)

    goal = commands.add_parser("goal")
    goal_commands = goal.add_subparsers(dest="goal_command", required=True)
    goal_create = goal_commands.add_parser("create")
    _add_scope_arguments(goal_create)
    goal_create.add_argument("--title", required=True)
    goal_create.add_argument("--objective", required=True)
    goal_create.add_argument("--domain", required=True)
    goal_create.add_argument("--mode", required=True, type=GoalMode, choices=list(GoalMode))
    goal_create.add_argument("--priority", type=int, default=50, choices=range(0, 101))
    goal_create.add_argument("--success-criterion", action="append", default=[])
    goal_create.add_argument("--constraints-json", type=_parse_json_object, default={})
    goal_create.add_argument("--parent-goal-id", type=_parse_uuid)

    goal_list = goal_commands.add_parser("list")
    _add_scope_arguments(goal_list)
    goal_list.add_argument("--status", action="append", type=GoalStatus, default=[])
    goal_list.add_argument("--limit", type=_parse_limit, default=50)

    goal_show = goal_commands.add_parser("show")
    _add_scope_arguments(goal_show)
    goal_show.add_argument("--goal-id", required=True, type=_parse_uuid)

    goal_update = goal_commands.add_parser("update")
    _add_scope_arguments(goal_update)
    goal_update.add_argument("--goal-id", required=True, type=_parse_uuid)
    goal_update.add_argument("--title")
    goal_update.add_argument("--objective")
    goal_update.add_argument("--domain")
    goal_update.add_argument("--mode", type=GoalMode, choices=list(GoalMode))
    goal_update.add_argument("--priority", type=int, choices=range(0, 101))
    goal_update.add_argument("--success-criterion", action="append")
    goal_update.add_argument("--constraints-json", type=_parse_json_object)
    parent = goal_update.add_mutually_exclusive_group()
    parent.add_argument("--parent-goal-id", type=_parse_uuid)
    parent.add_argument("--clear-parent", action="store_true")
    goal_update.add_argument("--status", type=GoalStatus, choices=list(GoalStatus))

    situation = commands.add_parser("situation")
    situation_commands = situation.add_subparsers(dest="situation_command", required=True)
    situation_list = situation_commands.add_parser("list")
    _add_scope_arguments(situation_list)
    situation_list.add_argument(
        "--lifecycle",
        action="append",
        type=SituationLifecycle,
        default=[],
    )
    situation_list.add_argument("--limit", type=_parse_limit, default=50)
    situation_show = situation_commands.add_parser("show")
    _add_scope_arguments(situation_show)
    situation_show.add_argument("--situation-id", required=True, type=_parse_uuid)

    memory = commands.add_parser("memory")
    memory_kinds = memory.add_subparsers(dest="memory_kind", required=True)

    fact = memory_kinds.add_parser("fact")
    fact_commands = fact.add_subparsers(dest="memory_command", required=True)
    fact_put = fact_commands.add_parser("put")
    _add_scope_arguments(fact_put)
    fact_put.add_argument("--namespace", required=True)
    fact_put.add_argument("--key", required=True)
    fact_put.add_argument("--value-json", required=True, type=_parse_json_value)
    fact_put.add_argument(
        "--scope-type", required=True, type=MemoryScopeType, choices=list(MemoryScopeType)
    )
    fact_put.add_argument("--scope-id", required=True, type=_parse_uuid)
    fact_put.add_argument(
        "--source-type",
        type=MemorySourceType,
        choices=list(MemorySourceType),
        default=MemorySourceType.USER_EXPLICIT,
    )
    fact_put.add_argument("--source-ref", required=True)
    fact_put.add_argument("--confidence", required=True, type=Decimal)
    fact_put.add_argument("--idempotency-key", required=True)
    fact_put.add_argument("--valid-from", type=_parse_datetime)
    fact_put.add_argument("--valid-until", type=_parse_datetime)

    fact_list = fact_commands.add_parser("list")
    _add_scope_arguments(fact_list)
    fact_list.add_argument("--status", action="append", type=MemoryFactStatus, default=[])
    fact_list.add_argument("--limit", type=_parse_limit, default=50)
    for command_name in ("show", "retract"):
        fact_record = fact_commands.add_parser(command_name)
        _add_scope_arguments(fact_record)
        fact_record.add_argument("--memory-id", required=True, type=_parse_uuid)

    episode = memory_kinds.add_parser("episode")
    episode_commands = episode.add_subparsers(dest="memory_command", required=True)
    episode_create = episode_commands.add_parser("create")
    _add_scope_arguments(episode_create)
    episode_create.add_argument(
        "--type", required=True, type=MemoryEpisodeType, choices=list(MemoryEpisodeType)
    )
    episode_create.add_argument("--summary", required=True)
    episode_create.add_argument("--entity", action="append", default=[])
    episode_create.add_argument("--goal-id", action="append", type=_parse_uuid, default=[])
    episode_create.add_argument("--situation-id", type=_parse_uuid)
    episode_create.add_argument("--importance", required=True, type=Decimal)
    episode_create.add_argument("--confidence", required=True, type=Decimal)
    episode_create.add_argument(
        "--source-type",
        type=MemorySourceType,
        choices=list(MemorySourceType),
        default=MemorySourceType.USER_EXPLICIT,
    )
    episode_create.add_argument("--source-ref", required=True)
    episode_create.add_argument("--idempotency-key", required=True)
    episode_create.add_argument("--occurred-at", type=_parse_datetime)

    episode_list = episode_commands.add_parser("list")
    _add_scope_arguments(episode_list)
    episode_list.add_argument("--status", action="append", type=EpisodicMemoryStatus, default=[])
    episode_list.add_argument("--limit", type=_parse_limit, default=50)
    for command_name in ("show", "retract"):
        episode_record = episode_commands.add_parser(command_name)
        _add_scope_arguments(episode_record)
        episode_record.add_argument("--memory-id", required=True, type=_parse_uuid)
    episode_search = episode_commands.add_parser("search")
    _add_scope_arguments(episode_search)
    episode_search.add_argument("--query", required=True)
    episode_search.add_argument("--limit", type=_parse_limit, default=8)

    context = commands.add_parser("context")
    context_commands = context.add_subparsers(dest="context_command", required=True)
    context_build = context_commands.add_parser("build")
    _add_scope_arguments(context_build)
    context_build.add_argument("--situation-id", required=True, type=_parse_uuid)
    context_build.add_argument("--focus")
    return parser


def main(
    argv: Sequence[str] | None = None,
    command_functions: CommandFunctions | None = None,
    *,
    settings_factory: Callable[[], Settings] = get_settings,
) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if command_functions is None:
            settings = settings_factory()
            configure_logging(settings)
            command_functions = build_command_functions(settings)
        asyncio.run(_dispatch(arguments, command_functions))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        # Debug diagnostics use only parser-controlled context and the exception class;
        # provider/database messages may contain secrets and are never rendered.
        _LOGGER.debug(
            "CLI command failed area=%s error_type=%s",
            arguments.area,
            type(error).__name__,
        )
        print("eva: command failed", file=sys.stderr)
        return 1
    return 0


async def _dispatch(arguments: argparse.Namespace, commands: CommandFunctions) -> None:
    if arguments.area == "scope" and arguments.scope_command == "create":
        await commands.scope_create(arguments.display_name, arguments.workspace_name)
    elif arguments.area == "gmail" and arguments.gmail_command == "connect":
        await commands.gmail_connect(arguments.user_id, arguments.workspace_id)
    elif arguments.area == "gmail" and arguments.gmail_command == "sync":
        await commands.gmail_sync(arguments.connector_id)
    elif arguments.area == "gmail" and arguments.gmail_command == "pull":
        await commands.gmail_pull()
    elif arguments.area == "gmail" and arguments.gmail_command == "maintain":
        await commands.gmail_maintain()
    elif arguments.area == "events" and arguments.events_command == "relay":
        await commands.events_relay()
    elif arguments.area == "relevance" and arguments.relevance_command == "pull":
        await commands.relevance_pull()
    elif arguments.area == "relevance" and arguments.relevance_command == "show":
        await commands.relevance_show(arguments.user_id, arguments.workspace_id, arguments.event_id)
    elif arguments.area == "relevance" and arguments.relevance_command == "history":
        await commands.relevance_history(
            arguments.user_id, arguments.workspace_id, arguments.event_id
        )
    elif arguments.area == "relevance" and arguments.relevance_command == "reevaluate":
        await commands.relevance_reevaluate(
            arguments.user_id,
            arguments.workspace_id,
            arguments.event_id,
            arguments.reason,
            arguments.idempotency_key or uuid7(),
        )
    elif arguments.area == "relevance" and arguments.relevance_command == "backfill":
        await commands.relevance_backfill(
            arguments.user_id, arguments.workspace_id, arguments.limit
        )
    elif arguments.area == "goal" and arguments.goal_command == "create":
        await commands.goal_create(
            GoalDraft(
                user_id=arguments.user_id,
                workspace_id=arguments.workspace_id,
                title=arguments.title,
                objective=arguments.objective,
                domain=arguments.domain,
                mode=arguments.mode,
                priority=arguments.priority,
                success_criteria=tuple(arguments.success_criterion),
                constraints=arguments.constraints_json,
                parent_goal_id=arguments.parent_goal_id,
            )
        )
    elif arguments.area == "goal" and arguments.goal_command == "list":
        await commands.goal_list(
            arguments.user_id,
            arguments.workspace_id,
            tuple(arguments.status),
            arguments.limit,
        )
    elif arguments.area == "goal" and arguments.goal_command == "show":
        await commands.goal_show(arguments.user_id, arguments.workspace_id, arguments.goal_id)
    elif arguments.area == "goal" and arguments.goal_command == "update":
        await commands.goal_update(
            GoalUpdate(
                user_id=arguments.user_id,
                workspace_id=arguments.workspace_id,
                goal_id=arguments.goal_id,
                title=arguments.title,
                objective=arguments.objective,
                domain=arguments.domain,
                mode=arguments.mode,
                priority=arguments.priority,
                success_criteria=(
                    tuple(arguments.success_criterion)
                    if arguments.success_criterion is not None
                    else None
                ),
                constraints=arguments.constraints_json,
                parent_goal_id=arguments.parent_goal_id,
                clear_parent=arguments.clear_parent,
                status=arguments.status,
            )
        )
    elif arguments.area == "situation" and arguments.situation_command == "list":
        await commands.situation_list(
            arguments.user_id,
            arguments.workspace_id,
            tuple(arguments.lifecycle),
            arguments.limit,
        )
    elif arguments.area == "situation" and arguments.situation_command == "show":
        await commands.situation_show(
            arguments.user_id,
            arguments.workspace_id,
            arguments.situation_id,
        )
    elif (
        arguments.area == "memory"
        and arguments.memory_kind == "fact"
        and arguments.memory_command == "put"
    ):
        await commands.memory_fact_put(
            MemoryFactDraft(
                user_id=arguments.user_id,
                workspace_id=arguments.workspace_id,
                namespace=arguments.namespace,
                key=arguments.key,
                value_json=arguments.value_json,
                scope_type=arguments.scope_type,
                scope_id=arguments.scope_id,
                source_type=arguments.source_type,
                source_ref=arguments.source_ref,
                confidence=arguments.confidence,
                idempotency_key=arguments.idempotency_key,
                valid_from=arguments.valid_from or _utc_now(),
                valid_until=arguments.valid_until,
            )
        )
    elif (
        arguments.area == "memory"
        and arguments.memory_kind == "fact"
        and arguments.memory_command == "list"
    ):
        await commands.memory_fact_list(
            arguments.user_id,
            arguments.workspace_id,
            tuple(arguments.status),
            arguments.limit,
        )
    elif arguments.area == "memory" and arguments.memory_kind == "fact":
        command = {
            "show": commands.memory_fact_show,
            "retract": commands.memory_fact_retract,
        }.get(arguments.memory_command)
        if command is None:
            raise CliValidationError("Command is unavailable")
        await command(arguments.user_id, arguments.workspace_id, arguments.memory_id)
    elif (
        arguments.area == "memory"
        and arguments.memory_kind == "episode"
        and arguments.memory_command == "create"
    ):
        await commands.memory_episode_create(
            EpisodicMemoryDraft(
                user_id=arguments.user_id,
                workspace_id=arguments.workspace_id,
                type=arguments.type,
                summary=arguments.summary,
                entities=tuple(arguments.entity),
                goal_ids=tuple(arguments.goal_id),
                situation_id=arguments.situation_id,
                importance=arguments.importance,
                confidence=arguments.confidence,
                source_type=arguments.source_type,
                source_ref=arguments.source_ref,
                idempotency_key=arguments.idempotency_key,
                occurred_at=arguments.occurred_at or _utc_now(),
            )
        )
    elif (
        arguments.area == "memory"
        and arguments.memory_kind == "episode"
        and arguments.memory_command == "list"
    ):
        await commands.memory_episode_list(
            arguments.user_id,
            arguments.workspace_id,
            tuple(arguments.status),
            arguments.limit,
        )
    elif (
        arguments.area == "memory"
        and arguments.memory_kind == "episode"
        and arguments.memory_command == "search"
    ):
        await commands.memory_episode_search(
            arguments.user_id,
            arguments.workspace_id,
            arguments.query,
            arguments.limit,
        )
    elif arguments.area == "memory" and arguments.memory_kind == "episode":
        command = {
            "show": commands.memory_episode_show,
            "retract": commands.memory_episode_retract,
        }.get(arguments.memory_command)
        if command is None:
            raise CliValidationError("Command is unavailable")
        await command(arguments.user_id, arguments.workspace_id, arguments.memory_id)
    elif arguments.area == "context" and arguments.context_command == "build":
        await commands.context_build(
            arguments.user_id,
            arguments.workspace_id,
            arguments.situation_id,
            arguments.focus,
        )
    else:
        raise CliValidationError("Command is unavailable")


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid UUID") from None


def _parse_json_object(value: str) -> JsonObject:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        raise argparse.ArgumentTypeError("invalid JSON object") from None
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("invalid JSON object")
    return cast(JsonObject, parsed)


def _parse_json_value(value: str) -> JsonValue:
    try:
        return cast(JsonValue, json.loads(value))
    except json.JSONDecodeError:
        raise argparse.ArgumentTypeError("invalid JSON value") from None


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError("invalid ISO 8601 timestamp") from None
    if parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def _parse_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be an integer") from None
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("limit must be between 1 and 100")
    return parsed


def _add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user-id", required=True, type=_parse_uuid)
    parser.add_argument("--workspace-id", required=True, type=_parse_uuid)


def _connect_configuration(settings: Settings) -> tuple[str, str, Path]:
    project_id = settings.pubsub_project_id
    account = settings.gmail_account
    client_file = settings.gmail_oauth_client_file
    if (
        project_id is None
        or not project_id.strip()
        or account is None
        or not account.strip()
        or client_file is None
        or not settings.gmail_topic_id.strip()
    ):
        raise CliValidationError("Gmail configuration is incomplete")
    if not client_file.is_file():
        raise CliValidationError("OAuth client file is unavailable")
    return project_id.strip(), account.strip(), client_file


def _validate_runtime_configuration(
    settings: Settings,
    *,
    require_subscription: bool,
) -> None:
    _required_project_id(settings)
    if not settings.gmail_topic_id.strip():
        raise CliValidationError("Gmail configuration is incomplete")
    if require_subscription:
        _required_subscription_id(settings)


def _required_project_id(settings: Settings) -> str:
    project_id = settings.pubsub_project_id
    if project_id is None or not project_id.strip():
        raise CliValidationError("Gmail configuration is incomplete")
    return project_id.strip()


def _required_subscription_id(settings: Settings) -> str:
    subscription_id = settings.gmail_subscription_id
    if not subscription_id.strip():
        raise CliValidationError("Gmail configuration is incomplete")
    return subscription_id.strip()


def _qualified_topic(project_id: str, topic_id: str) -> str:
    if not topic_id.strip():
        raise CliValidationError("Gmail configuration is incomplete")
    return f"projects/{project_id}/topics/{topic_id.strip()}"


async def _close_database(database: Database) -> CleanupOutcome:
    try:
        await database.close()
    except asyncio.CancelledError as error:
        return CleanupOutcome(interruption=error)
    except Exception:
        return CleanupOutcome(ordinary_failure=True)
    except BaseException as error:
        return CleanupOutcome(interruption=error)
    return CleanupOutcome()


async def _run_database_operation[T](
    settings: Settings,
    database_factory: DatabaseFactory,
    operation: Callable[[Database], Awaitable[T]],
) -> T:
    database = database_factory(settings.database_url.get_secret_value())
    primary_failure: BaseException | None = None
    missing = object()
    result: T | object = missing
    try:
        result = await operation(database)
    except BaseException as error:
        primary_failure = error
    cleanup_failed = await _close_database(database)
    _raise_after_cleanup(primary_failure, cleanup_failed)
    if result is missing:
        raise CliResourceError("Command did not produce a result")
    return cast(T, result)


async def _run_memory_operation[T](
    settings: Settings,
    dependency_builder: MemoryDependencyBuilder,
    operation: Callable[[MemoryDependencies], Awaitable[T]],
) -> T:
    dependencies = dependency_builder(settings, include_embedding=True)
    primary_failure: BaseException | None = None
    missing = object()
    result: T | object = missing
    try:
        result = await operation(dependencies)
    except BaseException as error:
        primary_failure = error
    cleanup = await dependencies.close()
    _raise_after_cleanup(
        primary_failure,
        CleanupOutcome(cleanup.interruption, cleanup.ordinary_failure),
    )
    if result is missing:
        raise CliResourceError("Command did not produce a result")
    return cast(T, result)


def _write_json(document: object, stdout: TextIO | None) -> None:
    print(
        json.dumps(
            _json_compatible(document),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=stdout or sys.stdout,
    )


def _json_compatible(value: object) -> object:
    if isinstance(value, BaseModel):
        # Python-mode dumping lets this single serializer normalize equivalent Decimals
        # identically whether a record is freshly created or read back from PostgreSQL.
        return _json_compatible(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return value


def _raise_after_cleanup(
    primary_failure: BaseException | None,
    cleanup: CleanupOutcome,
) -> None:
    if primary_failure is not None:
        raise primary_failure
    if cleanup.interruption is not None:
        raise cleanup.interruption
    if cleanup.ordinary_failure:
        raise CliResourceError("Command resource cleanup failed")


def _utc_now() -> datetime:
    return datetime.now(UTC)
