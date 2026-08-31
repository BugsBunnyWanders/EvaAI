from decimal import Decimal
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from eva_ai.db.base import Base
from eva_ai.db.models.common import TimestampMixin, UUIDPrimaryKeyMixin
from eva_ai.goals.types import GoalMode, GoalSource, GoalStatus


class Goal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "goals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspaces.id", "workspaces.user_id"],
            name="fk_goals_workspace_user",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "workspace_id", "user_id", name="uq_goals_id_workspace_user"),
        ForeignKeyConstraint(
            ["parent_goal_id", "workspace_id", "user_id"],
            ["goals.id", "goals.workspace_id", "goals.user_id"],
            name="fk_goals_parent_scope",
        ),
        CheckConstraint("mode IN ('ACHIEVE', 'MAINTAIN')", name="ck_goals_mode"),
        CheckConstraint("priority BETWEEN 0 AND 100", name="ck_goals_priority"),
        CheckConstraint(
            "status IN ('CANDIDATE', 'ACTIVE', 'PAUSED', 'COMPLETED', 'ABANDONED')",
            name="ck_goals_status",
        ),
        CheckConstraint(
            "jsonb_typeof(success_criteria) = 'array' "
            "AND jsonb_array_length(success_criteria) <= 20",
            name="ck_goals_success_criteria",
        ),
        CheckConstraint("jsonb_typeof(constraints) = 'object'", name="ck_goals_constraints"),
        CheckConstraint(
            'autonomy_policy = \'{"mode": "REQUIRE_APPROVAL"}\'::jsonb',
            name="ck_goals_autonomy_policy",
        ),
        CheckConstraint(
            "source IN ('USER_EXPLICIT', 'AGENT_INFERRED')",
            name="ck_goals_source",
        ),
        CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1",
            name="ck_goals_confidence",
        ),
        CheckConstraint("btrim(title) <> ''", name="ck_goals_title_nonblank"),
        CheckConstraint("btrim(objective) <> ''", name="ck_goals_objective_nonblank"),
        CheckConstraint("btrim(domain) <> ''", name="ck_goals_domain_nonblank"),
        Index("ix_goals_scope_status", "workspace_id", "user_id", "status"),
        Index(
            "ix_goals_scope_priority_created",
            "workspace_id",
            "user_id",
            "priority",
            "created_at",
        ),
    )

    user_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    title: Mapped[str] = mapped_column(String(200))
    objective: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(100))
    mode: Mapped[GoalMode] = mapped_column(String(32))
    priority: Mapped[int] = mapped_column(default=50, server_default="50")
    status: Mapped[GoalStatus] = mapped_column(String(32))
    success_criteria: Mapped[list[JsonValue]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    constraints: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    autonomy_policy: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB,
        default=lambda: {"mode": "REQUIRE_APPROVAL"},
        server_default=text('\'{"mode": "REQUIRE_APPROVAL"}\'::jsonb'),
    )
    source: Mapped[GoalSource] = mapped_column(String(32))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    parent_goal_id: Mapped[UUID | None]
