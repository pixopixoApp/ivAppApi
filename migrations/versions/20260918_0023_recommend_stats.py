"""Add per-video cumulative recommendation counter.

Revision ID: 20260918_0023
Revises: 20260915_0022
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260918_0023"
down_revision = "20260915_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "recommend_stats" in set(inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "recommend_stats",
        sa.Column("video_id", sa.String(length=128), primary_key=True),
        sa.Column("count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "first_recommended_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "last_recommended_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    # Recommendation counters are analytics data; keep them on rollback.
    pass
