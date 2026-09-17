import pytest
from sqlalchemy import inspect

from eva_ai.db import Database


async def _schema_details(
    database: Database, table_name: str
) -> tuple[set[str], set[str], set[str]]:
    async with database.engine.connect() as connection:
        return await connection.run_sync(
            lambda sync_connection: (
                {
                    item["name"]
                    for item in inspect(sync_connection).get_foreign_keys(table_name)
                    if item["name"] is not None
                },
                {
                    item["name"]
                    for item in inspect(sync_connection).get_unique_constraints(table_name)
                    if item["name"] is not None
                },
                {
                    item["name"]
                    for item in inspect(sync_connection).get_check_constraints(table_name)
                    if item["name"] is not None
                },
            )
        )


@pytest.mark.integration
async def test_action_schema_has_every_durable_boundary(database: Database) -> None:
    async with database.engine.connect() as connection:
        tables, notification_columns = await connection.run_sync(
            lambda sync_connection: (
                set(inspect(sync_connection).get_table_names()),
                {item["name"] for item in inspect(sync_connection).get_columns("notifications")},
            )
        )

    assert {
        "action_proposals",
        "action_approvals",
        "actions",
        "action_results",
        "managed_gmail_drafts",
        "action_revision_sessions",
    } <= tables
    assert {"reply_markup", "action_approval_id"} <= notification_columns
    notification_fks, _, _ = await _schema_details(database, "notifications")
    assert "fk_notifications_action_approval_scope" in notification_fks


@pytest.mark.integration
async def test_action_proposals_enforce_scope_immutability_and_versioning(
    database: Database,
) -> None:
    foreign_keys, uniques, checks = await _schema_details(database, "action_proposals")

    assert foreign_keys >= {
        "fk_action_proposals_workspace_user",
        "fk_action_proposals_connector_scope",
        "fk_action_proposals_event_scope",
        "fk_action_proposals_supersedes_scope",
    }
    assert uniques >= {
        "uq_action_proposals_id_scope",
        "uq_action_proposals_scope_source_key",
        "uq_action_proposals_scope_version",
    }
    assert checks >= {
        "ck_action_proposals_capability",
        "ck_action_proposals_status",
        "ck_action_proposals_version",
        "ck_action_proposals_hash",
        "ck_action_proposals_expiry",
    }


@pytest.mark.integration
async def test_action_approval_and_execution_schema_enforces_exact_identity(
    database: Database,
) -> None:
    approval_fks, approval_uniques, approval_checks = await _schema_details(
        database, "action_approvals"
    )
    action_fks, action_uniques, action_checks = await _schema_details(database, "actions")

    assert approval_fks >= {
        "fk_action_approvals_proposal_scope",
        "fk_action_approvals_telegram_account_scope",
    }
    assert approval_uniques >= {
        "uq_action_approvals_id_scope",
        "uq_action_approvals_proposal",
        "uq_action_approvals_callback_digest",
    }
    assert approval_checks >= {
        "ck_action_approvals_status",
        "ck_action_approvals_hash",
        "ck_action_approvals_expiry",
    }
    assert action_fks >= {
        "fk_actions_proposal_scope",
        "fk_actions_event_scope",
    }
    assert action_uniques >= {
        "uq_actions_id_scope",
        "uq_actions_scope_idempotency",
        "uq_actions_proposal",
    }
    assert action_checks >= {
        "ck_actions_status",
        "ck_actions_attempt_count",
        "ck_actions_provider_boundary",
        "ck_actions_finished_after_start",
    }


@pytest.mark.integration
async def test_managed_drafts_and_revision_sessions_are_tenant_scoped(
    database: Database,
) -> None:
    draft_fks, draft_uniques, draft_checks = await _schema_details(database, "managed_gmail_drafts")
    revision_fks, revision_uniques, revision_checks = await _schema_details(
        database, "action_revision_sessions"
    )
    async with database.engine.connect() as connection:
        revision_indexes = await connection.run_sync(
            lambda sync_connection: {
                item["name"]
                for item in inspect(sync_connection).get_indexes("action_revision_sessions")
            }
        )

    assert draft_fks >= {
        "fk_managed_gmail_drafts_connector_scope",
        "fk_managed_gmail_drafts_create_proposal_scope",
        "fk_managed_gmail_drafts_send_proposal_scope",
    }
    assert draft_uniques >= {
        "uq_managed_gmail_drafts_id_scope",
        "uq_managed_gmail_drafts_provider_draft",
    }
    assert draft_checks >= {
        "ck_managed_gmail_drafts_status",
        "ck_managed_gmail_drafts_hash",
        "ck_managed_gmail_drafts_send_version",
    }
    assert revision_fks >= {
        "fk_action_revision_sessions_account_scope",
        "fk_action_revision_sessions_conversation_scope",
        "fk_action_revision_sessions_draft_scope",
        "fk_action_revision_sessions_proposal_scope",
    }
    assert revision_uniques >= {"uq_action_revision_sessions_id_scope"}
    assert revision_checks >= {
        "ck_action_revision_sessions_status",
        "ck_action_revision_sessions_expiry",
    }
    assert "uq_action_revision_sessions_active_account" in revision_indexes
