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
from uuid import UUID

from pydantic import BaseModel

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
from eva_ai.situations import SituationLifecycle, SituationRepository, SituationService

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
DatabaseFactory = Callable[[str], Database]
DependencyBuilder = Callable[[Settings], "GmailDependencies"]
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
        # Only the exception type is logged; provider/database messages may contain secrets.
        _LOGGER.warning(
            "CLI command failed",
            extra={"command_area": arguments.area, "error_type": type(error).__name__},
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
