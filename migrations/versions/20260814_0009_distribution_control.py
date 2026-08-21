"""Separate operational visibility from content review status.

Revision ID: 20260814_0009
Revises: 20260812_0008
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260814_0009"
down_revision = "20260812_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in inspect(bind).get_columns("published_videos")}
    if "distribution_enabled" not in columns:
        op.add_column(
            "published_videos",
            sa.Column(
                "distribution_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )
    indexes = {item["name"] for item in inspect(bind).get_indexes("published_videos")}
    if "ix_published_videos_distribution_enabled" not in indexes:
        op.create_index(
            "ix_published_videos_distribution_enabled",
            "published_videos",
            ["distribution_enabled"],
        )


def downgrade() -> None:
    # Forward-only: retaining operational visibility history is safer than dropping it.
    pass
