from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = ROOT / "terraform" / "environments" / "production"


def _read(name: str) -> str:
    return (PRODUCTION / name).read_text()


def test_cloud_tasks_queue_and_filtered_dispatch_subscription_are_bounded() -> None:
    infrastructure = "\n".join(
        (_read("pubsub.tf"), _read("variables.tf"), _read("locals.tf"))
    )

    assert 'resource "google_cloud_tasks_queue" "actions"' in infrastructure
    assert "max_concurrent_dispatches" in infrastructure
    assert "max_dispatches_per_second" in infrastructure
    assert "max_attempts" in infrastructure
    assert 'message_type = \\"action.execution.requested\\"' in infrastructure
    assert "eva-action-dispatch-production" in infrastructure


def test_executor_identity_is_private_and_worker_can_only_enqueue_tasks() -> None:
    identity = _read("identity.tf")
    cloud_run = _read("cloud_run.tf")

    assert 'account_id   = "eva-action-executor"' in identity
    assert 'account_id   = "eva-action-task-caller"' in identity
    assert 'role    = "roles/cloudtasks.enqueuer"' in identity
    assert 'resource "google_cloud_run_v2_service_iam_member" "action_task_invoker"' in identity
    assert "google_service_account.action_task_caller.email" in identity
    assert 'resource "google_cloud_run_v2_service" "action_executor"' in cloud_run
    executor_block = cloud_run.split(
        'resource "google_cloud_run_v2_service" "action_executor"', maxsplit=1
    )[1]
    assert 'command = ["uvicorn"]' in executor_block
    assert (
        'args    = ["eva_ai.actions.api:app", "--host", "0.0.0.0", "--port", "8080"]'
        in executor_block
    )
    assert 'member   = "allUsers"' not in identity


def test_action_runtime_environment_and_output_use_executor_default_url() -> None:
    locals = _read("locals.tf")
    outputs = _read("outputs.tf")

    assert "EVA_ACTIONS_ENABLED" in locals
    assert "EVA_ACTION_DISPATCH_SUBSCRIPTION_ID" in locals
    assert "EVA_ACTION_EXECUTOR_URL" in locals
    assert "EVA_ACTION_EXECUTOR_AUDIENCE" in locals
    assert "EVA_ACTION_TASK_CALLER_SERVICE_ACCOUNT" in locals
    assert 'output "action_executor_url"' in outputs
    assert "google_cloud_run_v2_service.action_executor.uri" in outputs


def test_deploy_pauses_worker_migrates_restores_and_verifies_private_executor() -> None:
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()

    paused = workflow.index("Apply infrastructure with workers paused")
    migrated = workflow.index("Run database migrations")
    restored = workflow.index("Apply desired worker count")
    assert paused < migrated < restored
    assert "Verify private action executor" in workflow
    assert "eva-action-executor" in workflow
    assert "allUsers" in workflow
