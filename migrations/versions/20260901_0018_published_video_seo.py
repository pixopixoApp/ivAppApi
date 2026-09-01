"""Add durable search metadata for published experiences.

Revision ID: 20260901_0018
Revises: 20260831_0017
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260901_0018"
down_revision = "20260831_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "published_video_seo" in set(inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "published_video_seo",
        sa.Column("video_id", sa.String(length=128), primary_key=True),
        sa.Column("slug", sa.String(length=180), nullable=False),
        sa.Column("page_title", sa.String(length=120), nullable=False, server_default=""),
        # MySQL 5.7 and compatible RDS editions reject defaults on TEXT columns.
        # ORM-side defaults still guarantee non-null values for application writes.
        sa.Column("page_description", sa.Text(), nullable=False),
        sa.Column("meta_title", sa.String(length=70), nullable=False, server_default=""),
        sa.Column("meta_description", sa.String(length=180), nullable=False, server_default=""),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("interaction_types", sa.JSON(), nullable=False),
        sa.Column("interaction_summary", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("source_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("model", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("ai_title_written", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ai_description_written", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("title_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("description_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("slug", name="uq_published_video_seo_slug"),
    )
    op.create_index("ix_published_video_seo_status", "published_video_seo", ["status"])
    op.create_index(
        "ix_published_video_seo_status_updated",
        "published_video_seo",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    # Forward-only: published permalinks must never be recycled accidentally.
    pass
