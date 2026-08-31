import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid7

import pytest

from eva_ai.cli import CommandFunctions, build_command_functions, main
from eva_ai.config import get_settings
from eva_ai.db import Database
from eva_ai.events import EventService, NewEvent, PrincipalType
from eva_ai.situations import ResolveEvent, SituationRepository, SituationResolver
from tests.integration.factories import Scope, create_scope

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


async def run_cli(
    arguments: list[str],
    commands: CommandFunctions,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, Any] | None, str]:
    # main() owns its event loop, so execute it off the test's async loop.
    exit_code = await asyncio.to_thread(main, arguments, commands)
    captured = capsys.readouterr()
    if captured.out:
        assert captured.out.endswith("\n")
        assert captured.out.count("\n") == 1
    document = json.loads(captured.out) if captured.out else None
    return exit_code, document, captured.err


def scoped_arguments(scope: Scope) -> list[str]:
    return ["--user-id", str(scope.user_id), "--workspace-id", str(scope.workspace_id)]


@pytest.mark.integration
async def test_goal_operator_lifecycle_and_scoped_errors(
    database: Database,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scope = await create_scope(database)
    commands = build_command_functions(get_settings())

    first_exit, first, first_error = await run_cli(
        [
            "goal",
            "create",
            *scoped_arguments(scope),
            "--title",
            "Plan conference trip",
            "--objective",
            "Attend the conference",
            "--domain",
            "travel",
            "--mode",
            "ACHIEVE",
            "--priority",
            "80",
            "--success-criterion",
            "Travel booked",
            "--constraints-json",
            '{"budget":"bounded"}',
        ],
        commands,
        capsys,
    )
    assert first_exit == 0
    assert first_error == ""
    assert first is not None
    assert first["status"] == "ACTIVE"
    assert first["source"] == "USER_EXPLICIT"
    assert first["confidence"] == "1"

    second_exit, second, _ = await run_cli(
        [
            "goal",
            "create",
            *scoped_arguments(scope),
            "--title",
            "Maintain inbox",
            "--objective",
            "Keep important mail current",
            "--domain",
            "email",
            "--mode",
            "MAINTAIN",
            "--priority",
            "20",
        ],
        commands,
        capsys,
    )
    assert second_exit == 0
    assert second is not None

    list_exit, listed, _ = await run_cli(
        ["goal", "list", *scoped_arguments(scope), "--status", "ACTIVE"],
        commands,
        capsys,
    )
    assert list_exit == 0
    assert listed is not None
    assert listed["count"] == 2
    assert [item["id"] for item in listed["items"]] == [first["id"], second["id"]]

    show_exit, shown, _ = await run_cli(
        ["goal", "show", *scoped_arguments(scope), "--goal-id", first["id"]],
        commands,
        capsys,
    )
    assert show_exit == 0
    assert shown is not None
    assert shown == first

    update_exit, updated, _ = await run_cli(
        [
            "goal",
            "update",
            *scoped_arguments(scope),
            "--goal-id",
            first["id"],
            "--title",
            "Conference travel booked",
            "--status",
            "PAUSED",
        ],
        commands,
        capsys,
    )
    assert update_exit == 0
    assert updated is not None
    assert updated["title"] == "Conference travel booked"
    assert updated["status"] == "PAUSED"

    wrong_exit, wrong_document, wrong_error = await run_cli(
        [
            "goal",
            "show",
            "--user-id",
            str(uuid7()),
            "--workspace-id",
            str(scope.workspace_id),
            "--goal-id",
            first["id"],
        ],
        commands,
        capsys,
    )
    assert wrong_exit == 1
    assert wrong_document is None
    assert wrong_error == "eva: command failed\n"


@pytest.mark.integration
async def test_situation_operator_lists_safe_projection_without_event_payload(
    database: Database,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scope = await create_scope(database)
    event_id = await _ingest_private_gmail_event(database, scope)
    resolution = await SituationResolver(SituationRepository(database)).resolve(
        ResolveEvent(
            event_id=event_id,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            resolved_at=NOW + timedelta(minutes=1),
        )
    )
    commands = build_command_functions(get_settings())

    list_exit, listed, list_error = await run_cli(
        ["situation", "list", *scoped_arguments(scope), "--lifecycle", "OPEN"],
        commands,
        capsys,
    )
    assert list_exit == 0
    assert list_error == ""
    assert listed is not None
    assert listed["count"] == 1
    assert listed["items"][0]["id"] == str(resolution.situation.id)

    show_exit, shown, show_error = await run_cli(
        [
            "situation",
            "show",
            *scoped_arguments(scope),
            "--situation-id",
            str(resolution.situation.id),
        ],
        commands,
        capsys,
    )
    assert show_exit == 0
    assert show_error == ""
    assert shown is not None
    assert shown["situation"]["id"] == str(resolution.situation.id)
    assert shown["event_links"][0]["event_id"] == str(event_id)
    assert shown["goal_links"] == []
    serialized = json.dumps(shown)
    assert "PRIVATE_GMAIL_BODY" not in serialized
    assert "PRIVATE_AUTH_HEADER" not in serialized
    assert "payload" not in serialized


async def _ingest_private_gmail_event(database: Database, scope: Scope) -> UUID:
    event_id = uuid7()
    await EventService(database, "events").ingest(
        NewEvent(
            id=event_id,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            source="gmail",
            event_type="email.received",
            external_id="cli-message",
            idempotency_key=f"gmail:cli:{event_id}",
            occurred_at=NOW,
            received_at=NOW,
            principal_type=PrincipalType.EXTERNAL,
            payload={
                "thread_id": "cli-thread",
                "headers": {
                    "authorization": "PRIVATE_AUTH_HEADER",
                    "subject": "Travel status",
                },
                "snippet": "An itinerary update arrived.",
                "body": "PRIVATE_GMAIL_BODY",
            },
            correlation_keys=["gmail-thread:cli-thread"],
        )
    )
    return event_id
