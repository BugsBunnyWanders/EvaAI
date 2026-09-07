from datetime import datetime
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from eva_ai.agent.types import AgentRunStatus
from eva_ai.db.base import Base
from eva_ai.db.models.common import UUIDPrimaryKeyMixin


class AgentRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id", "workspace_id", "user_id"],
            ["events.id", "events.workspace_id", "events.user_id"],
            name="fk_agent_runs_event_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["signal_id", "workspace_id", "user_id", "event_id"],
            ["signals.id", "signals.workspace_id", "signals.user_id", "signals.event_id"],
            name="fk_agent_runs_signal_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_agent_runs_situation_scope",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "workspace_id", "user_id", name="uq_agent_runs_id_scope"),
        UniqueConstraint(
            "workspace_id",
            "user_id",
            "signal_id",
            "agent_version",
            name="uq_agent_runs_signal_version",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'RETRYABLE_FAILURE', "
            "'PERMANENT_FAILURE')",
            name="ck_agent_runs_status",
        ),
        CheckConstraint("output_schema_version > 0", name="ck_agent_runs_schema_version"),
        CheckConstraint("attempt_count >= 0", name="ck_agent_runs_attempt_count"),
        CheckConstraint(
            "input_digest IS NULL OR char_length(input_digest) = 64",
            name="ck_agent_runs_input_digest",
        ),
        CheckConstraint("input_tokens >= 0", name="ck_agent_runs_input_tokens"),
        CheckConstraint("output_tokens >= 0", name="ck_agent_runs_output_tokens"),
        CheckConstraint("total_tokens >= 0", name="ck_agent_runs_total_tokens"),
        Index(
            "ix_agent_runs_scope_status_retry", "workspace_id", "user_id", "status", "next_retry_at"
        ),
        Index("ix_agent_runs_situation_queued", "situation_id", "queued_at"),
    )

    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    event_id: Mapped[UUID]
    signal_id: Mapped[UUID]
    situation_id: Mapped[UUID]
    status: Mapped[AgentRunStatus] = mapped_column(String(32), server_default="QUEUED")
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(100))
    agent_version: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(100))
    output_schema_version: Mapped[int] = mapped_column(default=1, server_default="1")
    attempt_count: Mapped[int] = mapped_column(default=0, server_default="0")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_id: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_digest: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    provider_response_id: Mapped[str | None] = mapped_column(String(500))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tool_audit: Mapped[list[dict[str, JsonValue]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_summary: Mapped[str | None] = mapped_column(String(500))
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
