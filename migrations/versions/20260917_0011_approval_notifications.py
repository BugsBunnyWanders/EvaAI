"""Link Telegram notifications to exact action approvals.

Revision ID: 20260917_0011
Revises: 20260917_0010
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260917_0011"
down_revision: str | None = "20260917_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column("action_approval_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_notifications_action_approval_scope",
        "notifications",
        "action_approvals",
        ["action_approval_id", "workspace_id", "user_id"],
        ["id", "workspace_id", "user_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_notifications_action_approval_scope",
        "notifications",
        type_="foreignkey",
    )
    op.drop_column("notifications", "action_approval_id")
