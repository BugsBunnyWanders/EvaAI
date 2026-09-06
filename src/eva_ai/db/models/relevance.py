from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from eva_ai.db.base import Base
from eva_ai.db.models.common import UUIDPrimaryKeyMixin
from eva_ai.relevance.types import (
    EvaluationAttemptStatus,
    EvaluationTrigger,
    RelevanceDisposition,
    SignalKind,
    SignalProducer,
)
from eva_ai.situations.types import GoalContribution


class RelevanceEvaluationAttempt(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "relevance_evaluation_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id", "workspace_id", "user_id"],
            ["events.id", "events.workspace_id", "events.user_id"],
            name="fk_relevance_attempts_event_scope",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "id",
            "workspace_id",
            "user_id",
            "event_id",
            name="uq_relevance_attempts_id_scope",
        ),
        UniqueConstraint(
            "workspace_id",
            "user_id",
            "event_id",
            "evaluation_key",
            "attempt_number",
            name="uq_relevance_attempt_scope",
        ),
        CheckConstraint("attempt_number > 0", name="ck_relevance_attempt_number"),
        CheckConstraint(
            "trigger IN ('INITIAL', 'EXPLICIT_REEVALUATION', 'BACKFILL')",
            name="ck_relevance_attempt_trigger",
        ),
        CheckConstraint(
            "status IN ('STARTED', 'SUCCEEDED', 'RETRYABLE_FAILURE', 'PERMANENT_FAILURE')",
            name="ck_relevance_attempt_status",
        ),
        CheckConstraint(
            "char_length(input_digest) = 64",
            name="ck_relevance_attempt_digest",
        ),
        CheckConstraint(
            "(status = 'STARTED' AND completed_at IS NULL AND failure_code IS NULL) OR "
            "(status = 'SUCCEEDED' AND completed_at IS NOT NULL AND failure_code IS NULL) OR "
            "(status IN ('RETRYABLE_FAILURE', 'PERMANENT_FAILURE') "
            "AND completed_at IS NOT NULL AND failure_code IS NOT NULL)",
            name="ck_relevance_attempt_completion",
        ),
        Index(
            "ix_relevance_attempts_scope_event",
            "workspace_id",
            "user_id",
            "event_id",
            "started_at",
        ),
    )

    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    event_id: Mapped[UUID]
    evaluation_key: Mapped[UUID]
    attempt_number: Mapped[int]
    trigger: Mapped[EvaluationTrigger] = mapped_column(String(32))
    operator_reason: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[EvaluationAttemptStatus] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(100))
    classifier_version: Mapped[str] = mapped_column(String(100))
    input_digest: Mapped[str] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Signal(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "signals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id", "workspace_id", "user_id"],
            ["events.id", "events.workspace_id", "events.user_id"],
            name="fk_signals_event_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["successful_attempt_id", "workspace_id", "user_id", "event_id"],
            [
                "relevance_evaluation_attempts.id",
                "relevance_evaluation_attempts.workspace_id",
                "relevance_evaluation_attempts.user_id",
                "relevance_evaluation_attempts.event_id",
            ],
            name="fk_signals_attempt_scope",
        ),
        ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            ["situations.id", "situations.workspace_id", "situations.user_id"],
            name="fk_signals_situation_scope",
        ),
        ForeignKeyConstraint(
            ["supersedes_signal_id", "workspace_id", "user_id", "event_id"],
            ["signals.id", "signals.workspace_id", "signals.user_id", "signals.event_id"],
            name="fk_signals_supersedes_scope",
        ),
        UniqueConstraint("id", "workspace_id", "user_id", name="uq_signals_id_workspace_user"),
        UniqueConstraint(
            "id",
            "workspace_id",
            "user_id",
            "event_id",
            name="uq_signals_id_scope_event",
        ),
        UniqueConstraint(
            "workspace_id",
            "user_id",
            "event_id",
            "evaluation_key",
            name="uq_signals_scope_evaluation",
        ),
        CheckConstraint("kind IN ('RELEVANCE')", name="ck_signals_kind"),
        CheckConstraint("schema_version > 0", name="ck_signals_schema_version"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_signals_payload_object"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_signals_confidence"),
        CheckConstraint(
            "disposition IN ('IGNORE', 'RECORD', 'NOTIFY', 'INVESTIGATE')",
            name="ck_signals_disposition",
        ),
        CheckConstraint(
            "producer IN ('DETERMINISTIC', 'AI')",
            name="ck_signals_producer",
        ),
        CheckConstraint(
            "trigger IN ('INITIAL', 'EXPLICIT_REEVALUATION', 'BACKFILL')",
            name="ck_signals_trigger",
        ),
        CheckConstraint("char_length(input_digest) = 64", name="ck_signals_digest"),
        CheckConstraint(
            "supersedes_signal_id IS NULL OR supersedes_signal_id <> id",
            name="ck_signals_not_self_superseding",
        ),
        Index(
            "uq_signals_current_relevance",
            "workspace_id",
            "user_id",
            "event_id",
            unique=True,
            postgresql_where=text("kind = 'RELEVANCE' AND is_current"),
        ),
        Index(
            "ix_signals_scope_event_created",
            "workspace_id",
            "user_id",
            "event_id",
            "created_at",
        ),
    )

    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    event_id: Mapped[UUID]
    kind: Mapped[SignalKind] = mapped_column(String(32))
    schema_version: Mapped[int]
    payload: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    disposition: Mapped[RelevanceDisposition] = mapped_column(String(32))
    producer: Mapped[SignalProducer] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    classifier_version: Mapped[str] = mapped_column(String(100))
    policy_version: Mapped[str] = mapped_column(String(100))
    input_digest: Mapped[str] = mapped_column(String(64))
    trigger: Mapped[EvaluationTrigger] = mapped_column(String(32))
    operator_reason: Mapped[str | None] = mapped_column(String(500))
    evaluation_key: Mapped[UUID]
    successful_attempt_id: Mapped[UUID | None]
    situation_id: Mapped[UUID | None]
    supersedes_signal_id: Mapped[UUID | None]
    is_current: Mapped[bool] = mapped_column(default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=text("now()")
    )


class SignalGoal(Base):
    __tablename__ = "signal_goals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["signal_id", "workspace_id", "user_id"],
            ["signals.id", "signals.workspace_id", "signals.user_id"],
            name="fk_signal_goals_signal_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["goal_id", "workspace_id", "user_id"],
            ["goals.id", "goals.workspace_id", "goals.user_id"],
            name="fk_signal_goals_goal_scope",
            ondelete="CASCADE",
        ),
        CheckConstraint("relevance BETWEEN 0 AND 1", name="ck_signal_goals_relevance"),
        CheckConstraint(
            "contribution IN ('SUPPORTS', 'BLOCKS', 'CONTEXT')",
            name="ck_signal_goals_contribution",
        ),
        Index("ix_signal_goals_scope_goal", "workspace_id", "user_id", "goal_id"),
    )

    signal_id: Mapped[UUID] = mapped_column(primary_key=True)
    goal_id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID]
    user_id: Mapped[UUID]
    relevance: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    contribution: Mapped[GoalContribution] = mapped_column(String(20))
    reasoning: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=text("now()")
    )
