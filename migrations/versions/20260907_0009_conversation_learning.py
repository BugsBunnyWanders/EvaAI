"""Preserve conversation-agent output used by durable learning.

Revision ID: 20260907_0009
Revises: 20260907_0008
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260907_0009"
down_revision: str | None = "20260907_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversation_turns", sa.Column("reasoning_summary", sa.Text(), nullable=True))
    op.add_column(
        "conversation_turns",
        sa.Column(
            "proposed_actions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    op.add_column(
        "conversation_turns",
        sa.Column(
            "memory_proposals",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("conversation_turns", "memory_proposals")
    op.drop_column("conversation_turns", "proposed_actions")
    op.drop_column("conversation_turns", "reasoning_summary")
