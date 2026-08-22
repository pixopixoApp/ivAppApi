"""Add is_deleted flag to published_videos.

Revision ID: 20260819_0011
Revises: 20260819_0010
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260819_0011"
down_revision = "20260819_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in inspect(bind).get_columns("published_videos")}
    if "is_deleted" not in columns:
        op.add_column(
            "published_videos",
            sa.Column(
                "is_deleted",
                sa.SmallInteger(),
                nullable=False,
                server_default="0",
            ),
        )
    indexes = {item["name"] for item in inspect(bind).get_indexes("published_videos")}
    if "ix_published_videos_is_deleted" not in indexes:
        op.create_index(
            "ix_published_videos_is_deleted",
            "published_videos",
            ["is_deleted"],
        )
    # Backfill: rows already marked deleted (deleted_at set) become is_deleted=1.
    bind.execute(
        sa.text(
            "UPDATE published_videos SET is_deleted = 1 "
            "WHERE deleted_at IS NOT NULL AND is_deleted = 0"
        )
    )


def downgrade() -> None:
    # Forward-only: retaining delete-state history is safer than dropping it.
    pass
