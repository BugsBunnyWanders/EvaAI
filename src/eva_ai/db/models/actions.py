from datetime import datetime
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from eva_ai.actions.types import (
    ActionOrigin,
    ActionProposalStatus,
    ActionStatus,
    ApprovalStatus,
    GmailActionCapability,
    ManagedDraftStatus,
    PolicyDecision,
    RevisionSessionStatus,
)
from eva_ai.db.base import Base
from eva_ai.db.models.common import UUIDPrimaryKeyMixin


class ActionProposal(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "action_proposals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_action_proposals_workspace_user",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["connector_account_id", "workspace_id", "user_id"],
            [
                "connector_accounts.id",
                "connector_accounts.workspace_id",
                "connector_accounts.user_id",
            ],
            name="fk_action_proposals_connector_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["event_id", "workspace_id", "user_id"],
            ["events.id", "events.workspace_id", "events.user_id"],
            name="fk_action_proposals_event_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_action_proposals_situation_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["goal_id", "workspace_id", "user_id"],
            ["goals.id", "goals.workspace_id", "goals.user_id"],
            name="fk_action_proposals_goal_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["agent_run_id", "workspace_id", "user_id"],
            ["agent_runs.id", "agent_runs.workspace_id", "agent_runs.user_id"],
            name="fk_action_proposals_agent_run_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["conversation_turn_id", "workspace_id", "user_id"],
            [
                "conversation_turns.id",
                "conversation_turns.workspace_id",
                "conversation_turns.user_id",
            ],
            name="fk_action_proposals_conversation_turn_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["supersedes_proposal_id", "workspace_id", "user_id"],
            ["action_proposals.id", "action_proposals.workspace_id", "action_proposals.user_id"],
            name="fk_action_proposals_supersedes_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "workspace_id", "user_id", name="uq_action_proposals_id_scope"),
        UniqueConstraint(
            "workspace_id",
            "user_id",
            "source_key",
            name="uq_action_proposals_scope_source_key",
        ),
        UniqueConstraint(
            "workspace_id",
            "user_id",
            "proposal_family_key",
            "version",
            name="uq_action_proposals_scope_version",
        ),
        CheckConstraint(
            "capability IN ('gmail.create_draft', 'gmail.update_draft', "
            "'gmail.send_draft', 'gmail.delete_draft')",
            name="ck_action_proposals_capability",
        ),
        CheckConstraint(
            "status IN ('PROPOSED', 'QUEUED', 'WAITING_APPROVAL', 'APPROVED', 'REJECTED', "
            "'EXPIRED', 'SUPERSEDED', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name="ck_action_proposals_status",
        ),
        CheckConstraint("version >= 1", name="ck_action_proposals_version"),
        CheckConstraint("char_length(parameters_hash) = 64", name="ck_action_proposals_hash"),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at", name="ck_action_proposals_expiry"
        ),
        CheckConstraint("btrim(description) <> ''", name="ck_action_proposals_description"),
        Index(
            "ix_action_proposals_scope_status",
            "workspace_id",
            "user_id",
            "status",
            "created_at",
        ),
    )

    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    connector_account_id: Mapped[UUID]
    event_id: Mapped[UUID]
    situation_id: Mapped[UUID | None]
    goal_id: Mapped[UUID | None]
    agent_run_id: Mapped[UUID | None]
    conversation_turn_id: Mapped[UUID | None]
    supersedes_proposal_id: Mapped[UUID | None]
    source_key: Mapped[str] = mapped_column(String(500))
    proposal_family_key: Mapped[str] = mapped_column(String(500))
    origin: Mapped[ActionOrigin] = mapped_column(String(32))
    capability: Mapped[GmailActionCapability] = mapped_column(String(100))
    parameters_json: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    parameters_hash: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    risk_level: Mapped[str] = mapped_column(String(20))
    policy_decision: Mapped[PolicyDecision] = mapped_column(String(32))
    status: Mapped[ActionProposalStatus] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActionApproval(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "action_approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "workspace_id", "user_id"],
            ["action_proposals.id", "action_proposals.workspace_id", "action_proposals.user_id"],
            name="fk_action_approvals_proposal_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["telegram_account_id", "workspace_id", "user_id"],
            ["telegram_accounts.id", "telegram_accounts.workspace_id", "telegram_accounts.user_id"],
            name="fk_action_approvals_telegram_account_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "workspace_id", "user_id", name="uq_action_approvals_id_scope"),
        UniqueConstraint("proposal_id", name="uq_action_approvals_proposal"),
        UniqueConstraint("callback_token_digest", name="uq_action_approvals_callback_digest"),
        CheckConstraint(
            "status IN ('PENDING', 'GRANTED', 'REJECTED', 'EXPIRED', 'SUPERSEDED')",
            name="ck_action_approvals_status",
        ),
        CheckConstraint("char_length(parameters_hash) = 64", name="ck_action_approvals_hash"),
        CheckConstraint(
            "char_length(callback_token_digest) = 64", name="ck_action_approvals_token"
        ),
        CheckConstraint("expires_at > requested_at", name="ck_action_approvals_expiry"),
        Index(
            "ix_action_approvals_scope_status_expiry",
            "workspace_id",
            "user_id",
            "status",
            "expires_at",
        ),
    )

    proposal_id: Mapped[UUID]
    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    parameters_hash: Mapped[str] = mapped_column(String(64))
    principal_type: Mapped[str] = mapped_column(String(32))
    telegram_account_id: Mapped[UUID]
    provider_chat_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[ApprovalStatus] = mapped_column(String(32), server_default="PENDING")
    callback_token_digest: Mapped[str] = mapped_column(String(64))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Action(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "actions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "workspace_id", "user_id"],
            ["action_proposals.id", "action_proposals.workspace_id", "action_proposals.user_id"],
            name="fk_actions_proposal_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["event_id", "workspace_id", "user_id"],
            ["events.id", "events.workspace_id", "events.user_id"],
            name="fk_actions_event_scope",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "workspace_id", "user_id", name="uq_actions_id_scope"),
        UniqueConstraint("proposal_id", name="uq_actions_proposal"),
        UniqueConstraint(
            "workspace_id", "user_id", "idempotency_key", name="uq_actions_scope_idempotency"
        ),
        CheckConstraint(
            "capability IN ('gmail.create_draft', 'gmail.update_draft', "
            "'gmail.send_draft', 'gmail.delete_draft')",
            name="ck_actions_capability",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'UNKNOWN')",
            name="ck_actions_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_actions_attempt_count"),
        CheckConstraint(
            "provider_call_started_at IS NULL OR started_at IS NOT NULL",
            name="ck_actions_provider_boundary",
        ),
        CheckConstraint(
            "finished_at IS NULL OR started_at IS NOT NULL",
            name="ck_actions_finished_after_start",
        ),
        Index(
            "ix_actions_scope_status_lease", "workspace_id", "user_id", "status", "lease_expires_at"
        ),
    )

    proposal_id: Mapped[UUID]
    event_id: Mapped[UUID]
    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    capability: Mapped[GmailActionCapability] = mapped_column(String(100))
    idempotency_key: Mapped[str] = mapped_column(String(500))
    cloud_task_name: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[ActionStatus] = mapped_column(String(32), server_default="QUEUED")
    claim_id: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    provider_call_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_summary: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActionResult(Base):
    __tablename__ = "action_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["action_id", "workspace_id", "user_id"],
            ["actions.id", "actions.workspace_id", "actions.user_id"],
            name="fk_action_results_action_scope",
            ondelete="CASCADE",
        ),
        CheckConstraint("btrim(outcome) <> ''", name="ck_action_results_outcome"),
    )

    action_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    outcome: Mapped[str] = mapped_column(String(32))
    provider_status_category: Mapped[str | None] = mapped_column(String(100))
    provider_draft_id: Mapped[str | None] = mapped_column(String(500))
    provider_message_id: Mapped[str | None] = mapped_column(String(500))
    provider_thread_id: Mapped[str | None] = mapped_column(String(500))
    result_metadata: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ManagedGmailDraft(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "managed_gmail_drafts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["connector_account_id", "workspace_id", "user_id"],
            [
                "connector_accounts.id",
                "connector_accounts.workspace_id",
                "connector_accounts.user_id",
            ],
            name="fk_managed_gmail_drafts_connector_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["create_proposal_id", "workspace_id", "user_id"],
            ["action_proposals.id", "action_proposals.workspace_id", "action_proposals.user_id"],
            name="fk_managed_gmail_drafts_create_proposal_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["active_send_proposal_id", "workspace_id", "user_id"],
            ["action_proposals.id", "action_proposals.workspace_id", "action_proposals.user_id"],
            name="fk_managed_gmail_drafts_send_proposal_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "workspace_id", "user_id", name="uq_managed_gmail_drafts_id_scope"),
        UniqueConstraint(
            "connector_account_id",
            "provider_draft_id",
            name="uq_managed_gmail_drafts_provider_draft",
        ),
        CheckConstraint(
            "status IN ('CREATING', 'READY', 'UPDATING', 'SENDING', 'SENT', 'DELETING', "
            "'DELETED', 'UNKNOWN')",
            name="ck_managed_gmail_drafts_status",
        ),
        CheckConstraint(
            "char_length(current_content_hash) = 64", name="ck_managed_gmail_drafts_hash"
        ),
        CheckConstraint("send_proposal_version >= 0", name="ck_managed_gmail_drafts_send_version"),
        Index("ix_managed_gmail_drafts_scope_status", "workspace_id", "user_id", "status"),
    )

    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    connector_account_id: Mapped[UUID]
    provider_draft_id: Mapped[str] = mapped_column(String(500))
    provider_message_id: Mapped[str | None] = mapped_column(String(500))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(500))
    create_proposal_id: Mapped[UUID]
    active_send_proposal_id: Mapped[UUID | None]
    send_proposal_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    current_content_hash: Mapped[str] = mapped_column(String(64))
    rfc_message_id: Mapped[str] = mapped_column(String(998))
    status: Mapped[ManagedDraftStatus] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActionRevisionSession(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "action_revision_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["telegram_account_id", "workspace_id", "user_id"],
            ["telegram_accounts.id", "telegram_accounts.workspace_id", "telegram_accounts.user_id"],
            name="fk_action_revision_sessions_account_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "workspace_id", "user_id"],
            [
                "telegram_conversations.id",
                "telegram_conversations.workspace_id",
                "telegram_conversations.user_id",
            ],
            name="fk_action_revision_sessions_conversation_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["managed_draft_id", "workspace_id", "user_id"],
            [
                "managed_gmail_drafts.id",
                "managed_gmail_drafts.workspace_id",
                "managed_gmail_drafts.user_id",
            ],
            name="fk_action_revision_sessions_draft_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["active_send_proposal_id", "workspace_id", "user_id"],
            ["action_proposals.id", "action_proposals.workspace_id", "action_proposals.user_id"],
            name="fk_action_revision_sessions_proposal_scope",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "id", "workspace_id", "user_id", name="uq_action_revision_sessions_id_scope"
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'COMPLETED', 'EXPIRED', 'CANCELLED')",
            name="ck_action_revision_sessions_status",
        ),
        CheckConstraint("expires_at > created_at", name="ck_action_revision_sessions_expiry"),
        Index(
            "uq_action_revision_sessions_active_account",
            "workspace_id",
            "user_id",
            "telegram_account_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    telegram_account_id: Mapped[UUID]
    conversation_id: Mapped[UUID]
    managed_draft_id: Mapped[UUID]
    active_send_proposal_id: Mapped[UUID]
    status: Mapped[RevisionSessionStatus] = mapped_column(String(32), server_default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
