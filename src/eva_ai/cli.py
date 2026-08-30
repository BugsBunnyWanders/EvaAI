import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TextIO
from uuid import UUID

from eva_ai.config import Settings, get_settings
from eva_ai.connectors.gmail.bootstrap import ConnectGmail, GmailBootstrapService
from eva_ai.connectors.gmail.maintenance import GmailMaintenanceService
from eva_ai.connectors.gmail.sync import (
    GmailRecoveryService,
    GmailSyncService,
    SyncStatus,
)
from eva_ai.connectors.gmail.worker import GmailPullWorker
from eva_ai.connectors.repository import ConnectorRepository
from eva_ai.db.session import Database
from eva_ai.events.service import EventService
from eva_ai.integrations.gcp.secret_manager import GoogleSecretManagerCredentialStore
from eva_ai.integrations.gcp.subscriber import GooglePullSubscriber
from eva_ai.integrations.gmail.api import GoogleGmailClientFactory
from eva_ai.integrations.gmail.oauth import GoogleDesktopOAuthAuthorizer
from eva_ai.local_scope import LocalScope, create_local_scope, local_scope_exists
from eva_ai.logging import configure_logging

ScopeCreateCommand = Callable[[str, str], Awaitable[None]]
GmailConnectCommand = Callable[[UUID, UUID], Awaitable[None]]
GmailSyncCommand = Callable[[UUID], Awaitable[None]]
NoArgumentCommand = Callable[[], Awaitable[None]]
DatabaseFactory = Callable[[str], Database]
DependencyBuilder = Callable[[Settings], "GmailDependencies"]
ScopeCreator = Callable[..., Awaitable[LocalScope]]
ScopeValidator = Callable[[Database, UUID, UUID], Awaitable[bool]]
Clock = Callable[[], datetime]


class CliValidationError(ValueError):
    """A fixed, content-free local command validation failure."""


class CliResourceError(RuntimeError):
    """A fixed, content-free local resource cleanup failure."""


@dataclass(frozen=True, slots=True)
class CommandFunctions:
    scope_create: ScopeCreateCommand
    gmail_connect: GmailConnectCommand
    gmail_sync: GmailSyncCommand
    gmail_pull: NoArgumentCommand
    gmail_maintain: NoArgumentCommand


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

    async def close(self) -> bool:
        failed = False
        # Each close is independently attempted so one provider cannot strand later resources.
        for close in (
            self.subscriber.close,
            self.client_factory.close,
            self.credential_store.close,
            self.database.close,
        ):
            try:
                await close()
            except BaseException:
                failed = True
        return failed


def build_gmail_dependencies(settings: Settings) -> GmailDependencies:
    project_id = _required_project_id(settings)
    topic_name = _qualified_topic(project_id, settings.gmail_topic_id)
    database = Database(settings.database_url.get_secret_value())
    repository = ConnectorRepository(database)
    credential_store = GoogleSecretManagerCredentialStore(project_id)
    client_factory = GoogleGmailClientFactory()
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
    if result is None or result.status is SyncStatus.UNKNOWN_ACCOUNT:
        raise CliValidationError("Connector is unavailable")


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
    try:
        await dependencies.maintenance.run_due((clock or _utc_now)())
    except BaseException as error:
        primary_failure = error
    cleanup_failed = await dependencies.close()
    _raise_after_cleanup(primary_failure, cleanup_failed)


def build_command_functions(settings: Settings) -> CommandFunctions:
    return CommandFunctions(
        scope_create=partial(scope_create_command, settings=settings),
        gmail_connect=partial(gmail_connect_command, settings=settings),
        gmail_sync=partial(gmail_sync_command, settings=settings),
        gmail_pull=partial(gmail_pull_command, settings=settings),
        gmail_maintain=partial(gmail_maintain_command, settings=settings),
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
    except Exception:
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
    else:
        raise CliValidationError("Command is unavailable")


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid UUID") from None


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


async def _close_database(database: Database) -> bool:
    try:
        await database.close()
    except BaseException:
        return True
    return False


def _raise_after_cleanup(primary_failure: BaseException | None, cleanup_failed: bool) -> None:
    if primary_failure is not None:
        raise primary_failure
    if cleanup_failed:
        raise CliResourceError("Command resource cleanup failed")


def _utc_now() -> datetime:
    return datetime.now(UTC)
