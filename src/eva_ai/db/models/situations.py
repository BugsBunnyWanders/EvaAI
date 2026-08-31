from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from eva_ai.db.base import Base
from eva_ai.db.models.common import TimestampMixin, UUIDPrimaryKeyMixin
from eva_ai.situations import (
    AttentionLevel,
    CorrelationKeyKind,
    CorrelationMethod,
    GoalContribution,
    SituationLifecycle,
    SituationType,
)

SITUATION_SCOPE_REFERENCE = ["situations.id", "situations.workspace_id", "situations.user_id"]


class Situation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "situations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_situations_workspace_user",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "workspace_id", "user_id", name="uq_situations_id_workspace_user"),
        CheckConstraint("type IN ('EMAIL_THREAD')", name="ck_situations_type"),
        CheckConstraint(
            "lifecycle IN ('OPEN', 'ACTIVE', 'WAITING_USER', 'WAITING_EXTERNAL', "
            "'RESOLVED', 'ABANDONED')",
            name="ck_situations_lifecycle",
        ),
        CheckConstraint(
            "attention IN ('LOW', 'NORMAL', 'HIGH', 'URGENT')",
            name="ck_situations_attention",
        ),
        CheckConstraint("version >= 1", name="ck_situations_version"),
        CheckConstraint("btrim(title) <> ''", name="ck_situations_title_nonblank"),
        CheckConstraint("btrim(current_state) <> ''", name="ck_situations_current_state_nonblank"),
        Index("ix_situations_scope_lifecycle", "workspace_id", "user_id", "lifecycle"),
        Index(
            "ix_situations_scope_last_activity",
            "workspace_id",
            "user_id",
            "last_activity_at",
        ),
    )

    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    type: Mapped[SituationType] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(300))
    lifecycle: Mapped[SituationLifecycle] = mapped_column(
        String(32), default=SituationLifecycle.OPEN, server_default="OPEN"
    )
    attention: Mapped[AttentionLevel] = mapped_column(
        String(20), default=AttentionLevel.NORMAL, server_default="NORMAL"
    )
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    current_state: Mapped[str] = mapped_column(String(100))
    next_action: Mapped[str | None] = mapped_column(String(1000))
    next_expected: Mapped[str | None] = mapped_column(String(1000))
    version: Mapped[int] = mapped_column(default=1, server_default="1")
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SituationEvent(Base):
    __tablename__ = "situation_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            SITUATION_SCOPE_REFERENCE,
            name="fk_situation_events_situation_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["event_id", "workspace_id", "user_id"],
            ["events.id", "events.workspace_id", "events.user_id"],
            name="fk_situation_events_event_scope",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "correlation_method IN ('DETERMINISTIC_KEY', 'EXPLICIT')",
            name="ck_situation_events_method",
        ),
        Index("ix_situation_events_scope_event", "workspace_id", "user_id", "event_id"),
    )

    situation_id: Mapped[UUID] = mapped_column(primary_key=True)
    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID]
    user_id: Mapped[UUID]
    correlation_method: Mapped[CorrelationMethod] = mapped_column(String(32))
    correlation_key: Mapped[str | None] = mapped_column(String(500))
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=text("now()")
    )


class SituationGoal(Base):
    __tablename__ = "situation_goals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            SITUATION_SCOPE_REFERENCE,
            name="fk_situation_goals_situation_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["goal_id", "workspace_id", "user_id"],
            ["goals.id", "goals.workspace_id", "goals.user_id"],
            name="fk_situation_goals_goal_scope",
            ondelete="CASCADE",
        ),
        CheckConstraint("relevance BETWEEN 0 AND 1", name="ck_situation_goals_relevance"),
        CheckConstraint(
            "contribution IN ('SUPPORTS', 'BLOCKS', 'CONTEXT')",
            name="ck_situation_goals_contribution",
        ),
        Index("ix_situation_goals_scope_goal", "workspace_id", "user_id", "goal_id"),
    )

    situation_id: Mapped[UUID] = mapped_column(primary_key=True)
    goal_id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID]
    user_id: Mapped[UUID]
    relevance: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    contribution: Mapped[GoalContribution] = mapped_column(String(20))
    reasoning: Mapped[str | None] = mapped_column(String(1000))
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=text("now()")
    )


class SituationCorrelationKey(Base):
    __tablename__ = "situation_correlation_keys"
    __table_args__ = (
        ForeignKeyConstraint(
            ["situation_id", "workspace_id", "user_id"],
            SITUATION_SCOPE_REFERENCE,
            name="fk_situation_correlation_keys_situation_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_situation_correlation_keys_workspace_user",
            ondelete="CASCADE",
        ),
        CheckConstraint("kind IN ('GMAIL_THREAD')", name="ck_situation_correlation_keys_kind"),
        CheckConstraint(
            "btrim(correlation_key) <> ''",
            name="ck_situation_correlation_keys_key_nonblank",
        ),
        Index(
            "ix_situation_correlation_keys_scope_situation",
            "workspace_id",
            "user_id",
            "situation_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(primary_key=True)
    correlation_key: Mapped[str] = mapped_column(String(500), primary_key=True)
    user_id: Mapped[UUID]
    situation_id: Mapped[UUID]
    kind: Mapped[CorrelationKeyKind] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=text("now()")
    )
