import json
import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]


def _fake_uv(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture_file = tmp_path / "argv.json"
    executable = bin_dir / "uv"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['EVA_TEST_ARGV_CAPTURE'], 'w', encoding='utf-8') as stream:\n"
        "    json.dump(sys.argv[1:], stream)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return bin_dir, capture_file


def _make_environment(bin_dir: Path, capture_file: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "EVA_TEST_ARGV_CAPTURE": str(capture_file),
    }


@pytest.mark.parametrize("input_form", ["environment", "command-line"])
def test_gmail_connect_target_forwards_literal_values_without_evaluating_source(
    tmp_path: Path,
    input_form: str,
) -> None:
    """Fails if Make or the shell evaluates a value instead of forwarding one argument."""
    bin_dir, capture_file = _fake_uv(tmp_path)
    make_marker = tmp_path / "make-injected"
    shell_marker = tmp_path / "shell-injected"
    user_value = (
        f'$(shell touch {make_marker})"; touch {shell_marker}; : "$DOLLAR whitespace\nline-two'
    )
    workspace_value = "workspace with internal and trailing space "
    environment = _make_environment(bin_dir, capture_file)
    command = ["make", "--no-print-directory", "gmail-connect"]
    if input_form == "environment":
        environment.update(
            EVA_USER_ID=user_value,
            EVA_WORKSPACE_ID=workspace_value,
        )
    else:
        command.extend(
            [
                f"EVA_USER_ID={user_value}",
                f"EVA_WORKSPACE_ID={workspace_value}",
            ]
        )

    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not make_marker.exists()
    assert not shell_marker.exists()
    assert json.loads(capture_file.read_text(encoding="utf-8")) == [
        "run",
        "eva",
        "gmail",
        "connect",
        "--user-id",
        user_value,
        "--workspace-id",
        workspace_value,
    ]


@pytest.mark.parametrize("input_form", ["environment", "command-line"])
def test_gmail_sync_target_dry_run_never_expands_operator_value(
    tmp_path: Path,
    input_form: str,
) -> None:
    """Fails if even a Make dry-run evaluates a function or renders shell source."""
    marker = tmp_path / "dry-run-injected"
    connector_value = f'$(shell touch {marker})"; touch {marker}; $HOME\nsecond-line'
    environment = dict(os.environ)
    command = ["make", "--no-print-directory", "-n", "gmail-sync"]
    if input_form == "environment":
        environment["EVA_GMAIL_CONNECTOR_ID"] = connector_value
    else:
        command.append(f"EVA_GMAIL_CONNECTOR_ID={connector_value}")

    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()
    assert connector_value not in completed.stdout
    assert '"${EVA_GMAIL_CONNECTOR_ID}"' in completed.stdout


def test_relevance_reevaluate_forwards_all_operator_values_as_literal_arguments(
    tmp_path: Path,
) -> None:
    bin_dir, capture_file = _fake_uv(tmp_path)
    marker = tmp_path / "relevance-injected"
    malicious = f'$(shell touch {marker})"; touch {marker}; $HOME\nsecond-line'
    environment = _make_environment(bin_dir, capture_file)
    environment.update(
        EVA_USER_ID="user-id",
        EVA_WORKSPACE_ID="workspace-id",
        EVA_EVENT_ID="event-id",
        EVA_RELEVANCE_REASON=malicious,
        EVA_RELEVANCE_IDEMPOTENCY_KEY="evaluation-id",
    )

    completed = subprocess.run(
        ["make", "--no-print-directory", "relevance-reevaluate"],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()
    assert json.loads(capture_file.read_text(encoding="utf-8")) == [
        "run",
        "eva",
        "relevance",
        "reevaluate",
        "--user-id",
        "user-id",
        "--workspace-id",
        "workspace-id",
        "--event-id",
        "event-id",
        "--reason",
        malicious,
        "--idempotency-key",
        "evaluation-id",
    ]


def test_relevance_documentation_and_safe_defaults_are_present() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "docs/relevance-operator.md" in readme
    assert "EVA_RELEVANCE_ENABLED=false" in example
    assert "EVA_OPENAI_API_KEY=\n" in example
    assert "EVA_OUTBOX_RELAY_POLL_SECONDS=1" in example
    assert "EVA_RELEVANCE_MODEL=gpt-5.6-luna" in example
