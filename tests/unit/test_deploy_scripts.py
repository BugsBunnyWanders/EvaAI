import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERIFY_EXECUTOR = ROOT / ".github" / "scripts" / "verify-private-action-executor.sh"


def test_private_executor_verification_accepts_internal_service(tmp_path: Path) -> None:
    fake_gcloud = tmp_path / "gcloud"
    fake_gcloud.write_text(
        """#!/usr/bin/env bash
set -euo pipefail

case "$*" in
  "run services describe"*)
    printf '%s\n' '{"metadata":{"annotations":{"run.googleapis.com/ingress":"internal"}}}'
    ;;
  "run services get-iam-policy"*)
    printf '%s\n' '{"bindings":[]}'
    ;;
  *)
    printf 'unexpected gcloud arguments: %s\n' "$*" >&2
    exit 2
    ;;
esac
"""
    )
    fake_gcloud.chmod(0o755)
    env = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PROJECT_ID": "test-project",
        "REGION": "test-region",
    }

    result = subprocess.run(
        ["bash", str(VERIFY_EXECUTOR)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr
