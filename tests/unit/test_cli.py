import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import cast
from uuid import UUID

import pytest

from eva_ai.cli import (
    CliValidationError,
    CommandFunctions,
    GmailDependencies,
    gmail_connect_command,
    gmail_maintain_command,
    gmail_pull_command,
    gmail_sync_command,
    main,
    scope_create_command,
)
from eva_ai.config import Settings
from eva_ai.connectors.gmail.bootstrap import ConnectGmail
from eva_ai.connectors.gmail.maintenance import MaintenanceSummary
from eva_ai.connectors.gmail.sync import SyncResult, SyncStatus
from eva_ai.connectors.types import ConnectorRecord, ConnectorStatus
from eva_ai.db.models import User, Workspace
from eva_ai.db.session import Database
from eva_ai.local_scope import LocalScope, create_local_scope

USER_ID = UUID("0191cafe-7b00-7000-8000-000000000001")
WORKSPACE_ID = UUID("0191cafe-7b00-7000-8000-000000000002")
CONNECTOR_ID = UUID("0191cafe-7b00-7000-8000-000000000003")
NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)


class RecordingCommand:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def __call__(self, *arguments: object) -> None:
        self.calls.append(arguments)


def command_functions() -> tuple[CommandFunctions, dict[str, RecordingCommand]]:
    commands = {
        "scope_create": RecordingCommand(),
        "gmail_connect": RecordingCommand(),
        "gmail_sync": RecordingCommand(),
        "gmail_pull": RecordingCommand(),
        "gmail_maintain": RecordingCommand(),
    }
    return (
        CommandFunctions(
            scope_create=commands["scope_create"],
            gmail_connect=commands["gmail_connect"],
            gmail_sync=commands["gmail_sync"],
            gmail_pull=commands["gmail_pull"],
            gmail_maintain=commands["gmail_maintain"],
        ),
        commands,
    )


@pytest.mark.parametrize(
    ("arguments", "called", "expected"),
    [
        (
            ["scope", "create", "--display-name", "Saswat Ray", "--workspace-name", "personal"],
            "scope_create",
            ("Saswat Ray", "personal"),
        ),
        (
            [
                "gmail",
                "connect",
                "--user-id",
                str(USER_ID),
                "--workspace-id",
                str(WORKSPACE_ID),
            ],
            "gmail_connect",
            (USER_ID, WORKSPACE_ID),
        ),
        (
            ["gmail", "sync", "--connector-id", str(CONNECTOR_ID)],
            "gmail_sync",
            (CONNECTOR_ID,),
        ),
        (["gmail", "pull"], "gmail_pull", ()),
        (["gmail", "maintain"], "gmail_maintain", ()),
    ],
)
def test_main_dispatches_exact_command_arguments(
    arguments: list[str],
    called: str,
    expected: tuple[object, ...],
) -> None:
    """Fails if argparse routes a command incorrectly or changes its required values."""
    functions, commands = command_functions()

    assert main(arguments, command_functions=functions) == 0

    assert commands[called].calls == [expected]
    assert sum(len(command.calls) for command in commands.values()) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        ["scope", "create", "--display-name", "Saswat Ray"],
        ["gmail", "connect", "--user-id", str(USER_ID)],
        ["gmail", "sync"],
    ],
)
def test_parser_rejects_missing_required_arguments_before_dispatch(arguments: list[str]) -> None:
    """Fails if an incomplete identity-bearing command can reach composition."""
    functions, commands = command_functions()

    with pytest.raises(SystemExit) as raised:
        main(arguments, command_functions=functions)

    assert raised.value.code == 2
    assert all(not command.calls for command in commands.values())


@pytest.mark.parametrize("arguments", [["--help"], ["gmail", "--help"]])
def test_help_never_loads_settings_or_constructs_dependencies(arguments: list[str]) -> None:
    """Fails if help can initialize OAuth, Google clients, or database composition."""

    def forbidden_settings() -> Settings:
        raise AssertionError("help must not load settings")

    with pytest.raises(SystemExit) as raised:
        main(arguments, settings_factory=forbidden_settings)

    assert raised.value.code == 0


def test_command_failure_has_one_fixed_content_free_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fails if provider, mailbox, token, or database details reach stderr."""
    functions, _ = command_functions()

    async def fail(_: UUID) -> None:
        raise RuntimeError(
            "postgresql://owner:password@db private@example.com refresh_token=private"
        )

    functions = CommandFunctions(
        scope_create=functions.scope_create,
        gmail_connect=functions.gmail_connect,
        gmail_sync=fail,
        gmail_pull=functions.gmail_pull,
        gmail_maintain=functions.gmail_maintain,
    )

    assert main(["gmail", "sync", "--connector-id", str(CONNECTOR_ID)], functions) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "eva: command failed\n"


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.transaction_entries = 0
        self.transaction_exits = 0

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self)

    def add_all(self, instances: list[object]) -> None:
        assert self.transaction_entries == 1
        assert self.transaction_exits == 0
        self.added.extend(instances)


class FakeTransaction(AbstractAsyncContextManager[None]):
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> None:
        self.session.transaction_entries += 1

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.session.transaction_exits += 1


class FakeSessionContext(AbstractAsyncContextManager[FakeSession]):
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> FakeSession:
        return self.session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class FakeDatabase:
    def __init__(self) -> None:
        self.session_value = FakeSession()
        self.close_calls = 0

    def session(self) -> FakeSessionContext:
        return FakeSessionContext(self.session_value)

    async def close(self) -> None:
        self.close_calls += 1


async def test_local_scope_inserts_one_user_and_workspace_in_one_transaction() -> None:
    """Fails if local ownership rows can commit separately or implicit scope is created."""
    database = FakeDatabase()

    scope = await create_local_scope(
        cast(Database, database),
        display_name="Saswat Ray",
        workspace_name="personal",
    )

    assert isinstance(scope, LocalScope)
    assert database.session_value.transaction_entries == 1
    assert database.session_value.transaction_exits == 1
    assert len(database.session_value.added) == 2
    user, workspace = database.session_value.added
    assert isinstance(user, User)
    assert isinstance(workspace, Workspace)
    assert (user.id, user.display_name) == (scope.user_id, "Saswat Ray")
    assert (workspace.id, workspace.user_id, workspace.name) == (
        scope.workspace_id,
        scope.user_id,
        "personal",
    )


class FakeBootstrap:
    def __init__(self) -> None:
        self.commands: list[ConnectGmail] = []

    async def connect(self, command: ConnectGmail) -> ConnectorRecord:
        self.commands.append(command)
        return ConnectorRecord(
            id=CONNECTOR_ID,
            user_id=command.user_id,
            workspace_id=command.workspace_id,
            provider="gmail",
            account_identity=command.expected_identity,
            granted_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
            status=ConnectorStatus.ACTIVE,
            secret_reference="projects/eva/secrets/synthetic",
            connected_at=NOW,
        )


class FakeSync:
    def __init__(self) -> None:
        self.connector_ids: list[UUID] = []

    async def sync_connector(self, connector_id: UUID) -> SyncResult:
        self.connector_ids.append(connector_id)
        return SyncResult(SyncStatus.SYNCED, connector_id, 0, "100")


class FakeMaintenance:
    def __init__(self) -> None:
        self.calls: list[datetime] = []

    async def run_due(self, now: datetime) -> MaintenanceSummary:
        self.calls.append(now)
        return MaintenanceSummary(renewed=0, safety_synced=0, failed=0)


class FakeWorker:
    def __init__(self) -> None:
        self.calls = 0
        self.failure: BaseException | None = None

    async def run_forever(self) -> None:
        self.calls += 1
        if self.failure is not None:
            raise self.failure


class FakeDependencies:
    def __init__(self) -> None:
        self.bootstrap = FakeBootstrap()
        self.sync_service = FakeSync()
        self.maintenance = FakeMaintenance()
        self.worker = FakeWorker()
        self.close_calls = 0

    async def close(self) -> bool:
        self.close_calls += 1
        return False


def configured_settings(client_file: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://user:password@localhost/eva",
        pubsub_project_id="eva-project",
        gmail_topic_id="gmail-topic",
        gmail_subscription_id="gmail-subscription",
        gmail_account="owner@example.com",
        gmail_oauth_client_file=client_file,
    )


@pytest.mark.parametrize(
    "settings_override",
    [
        {"pubsub_project_id": None},
        {"gmail_account": None},
        {"gmail_oauth_client_file": None},
    ],
)
async def test_connect_rejects_missing_configuration_before_database_or_clients(
    tmp_path: Path,
    settings_override: dict[str, object],
) -> None:
    """Fails if missing project/account/client input can start persistence or OAuth."""
    client_file = tmp_path / "client.json"
    client_file.write_text("synthetic-not-parsed", encoding="utf-8")
    settings = configured_settings(client_file).model_copy(update=settings_override)
    database_calls = 0
    builder_calls = 0

    def database_factory(_: str) -> Database:
        nonlocal database_calls
        database_calls += 1
        return cast(Database, FakeDatabase())

    def dependency_builder(_: Settings) -> GmailDependencies:
        nonlocal builder_calls
        builder_calls += 1
        return cast(GmailDependencies, FakeDependencies())

    with pytest.raises(CliValidationError, match="^Gmail configuration is incomplete$"):
        await gmail_connect_command(
            USER_ID,
            WORKSPACE_ID,
            settings=settings,
            database_factory=database_factory,
            dependency_builder=dependency_builder,
        )

    assert database_calls == 0
    assert builder_calls == 0


@pytest.mark.parametrize("client_kind", ["missing", "directory"])
async def test_connect_requires_oauth_client_to_be_a_regular_file_before_side_effects(
    tmp_path: Path,
    client_kind: str,
) -> None:
    """Fails if OAuth starts for a missing/directory client path or the path is parsed."""
    client_file = tmp_path / "client.json"
    if client_kind == "directory":
        client_file.mkdir()
    settings = configured_settings(client_file)
    database_calls = 0

    def database_factory(_: str) -> Database:
        nonlocal database_calls
        database_calls += 1
        return cast(Database, FakeDatabase())

    with pytest.raises(CliValidationError, match="^OAuth client file is unavailable$"):
        await gmail_connect_command(
            USER_ID,
            WORKSPACE_ID,
            settings=settings,
            database_factory=database_factory,
        )

    assert database_calls == 0


async def test_connect_validates_persisted_scope_before_building_google_dependencies(
    tmp_path: Path,
) -> None:
    """Fails if missing/mismatched ownership can reach browser or Google constructors."""
    client_file = tmp_path / "client.json"
    client_file.write_text("synthetic-not-parsed", encoding="utf-8")
    database = FakeDatabase()
    builder_calls = 0

    async def missing_scope(_: Database, __: UUID, ___: UUID) -> bool:
        return False

    def dependency_builder(_: Settings) -> GmailDependencies:
        nonlocal builder_calls
        builder_calls += 1
        return cast(GmailDependencies, FakeDependencies())

    with pytest.raises(CliValidationError, match="^User and Workspace scope is unavailable$"):
        await gmail_connect_command(
            USER_ID,
            WORKSPACE_ID,
            settings=configured_settings(client_file),
            database_factory=lambda _: cast(Database, database),
            scope_validator=missing_scope,
            dependency_builder=dependency_builder,
        )

    assert builder_calls == 0
    assert database.close_calls == 1


async def test_connect_uses_fully_qualified_topic_and_prints_only_connector_uuid(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fails if connect passes a partial topic, prints metadata, or leaves resources open."""
    client_file = tmp_path / "client.json"
    client_file.write_text("synthetic-not-parsed", encoding="utf-8")
    database = FakeDatabase()
    dependencies = FakeDependencies()

    async def valid_scope(_: Database, user_id: UUID, workspace_id: UUID) -> bool:
        return (user_id, workspace_id) == (USER_ID, WORKSPACE_ID)

    await gmail_connect_command(
        USER_ID,
        WORKSPACE_ID,
        settings=configured_settings(client_file),
        database_factory=lambda _: cast(Database, database),
        scope_validator=valid_scope,
        dependency_builder=lambda *_: cast(GmailDependencies, dependencies),
    )

    assert dependencies.bootstrap.commands == [
        ConnectGmail(
            user_id=USER_ID,
            workspace_id=WORKSPACE_ID,
            expected_identity="owner@example.com",
            client_file=client_file,
            topic_name="projects/eva-project/topics/gmail-topic",
        )
    ]
    assert capsys.readouterr().out == f"{CONNECTOR_ID}\n"
    assert dependencies.close_calls == 1
    assert database.close_calls == 1


async def test_scope_create_prints_only_ids_and_closes_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fails if scope bootstrap prints names/secrets or leaks its database engine."""
    database = FakeDatabase()

    async def creator(_: Database, *, display_name: str, workspace_name: str) -> LocalScope:
        assert (display_name, workspace_name) == ("Saswat Ray", "personal")
        return LocalScope(USER_ID, WORKSPACE_ID)

    await scope_create_command(
        "Saswat Ray",
        "personal",
        settings=Settings(_env_file=None),
        database_factory=lambda _: cast(Database, database),
        scope_creator=creator,
    )

    assert capsys.readouterr().out == f"{USER_ID}\n{WORKSPACE_ID}\n"
    assert database.close_calls == 1


async def test_sync_is_one_stored_cursor_attempt_and_closes_all_resources() -> None:
    """Fails if manual sync loops, uses a notification cursor, or omits cleanup."""
    dependencies = FakeDependencies()

    await gmail_sync_command(
        CONNECTOR_ID,
        settings=Settings(_env_file=None, pubsub_project_id="eva-project"),
        dependency_builder=lambda *_: cast(GmailDependencies, dependencies),
    )

    assert dependencies.sync_service.connector_ids == [CONNECTOR_ID]
    assert dependencies.close_calls == 1


async def test_maintain_runs_one_due_pass_with_injected_clock() -> None:
    """Fails if maintain loops or ignores the command's deterministic current time."""
    dependencies = FakeDependencies()

    await gmail_maintain_command(
        settings=Settings(_env_file=None, pubsub_project_id="eva-project"),
        dependency_builder=lambda *_: cast(GmailDependencies, dependencies),
        clock=lambda: NOW,
    )

    assert dependencies.maintenance.calls == [NOW]
    assert dependencies.close_calls == 1


async def test_pull_runs_continuously_and_cleans_up_on_cancellation() -> None:
    """Fails if pull uses a one-shot batch or cancellation skips runtime cleanup."""
    dependencies = FakeDependencies()
    cancellation = asyncio.CancelledError("stop")
    dependencies.worker.failure = cancellation

    with pytest.raises(asyncio.CancelledError) as raised:
        await gmail_pull_command(
            settings=Settings(_env_file=None, pubsub_project_id="eva-project"),
            dependency_builder=lambda *_: cast(GmailDependencies, dependencies),
        )

    assert raised.value is cancellation
    assert dependencies.worker.calls == 1
    assert dependencies.close_calls == 1
