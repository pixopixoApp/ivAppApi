"""Durable creator versions, publish metadata and soft deletion.

Revision ID: 20260808_0003
Revises: 20260808_0002
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from app import models  # noqa: F401
from app.db import Base

revision = "20260808_0003"
down_revision = "20260808_0002"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)

    published = _columns("published_videos")
    if "title" not in published:
        op.add_column(
            "published_videos",
            sa.Column("title", sa.String(length=120), nullable=False, server_default=""),
        )
    if "description" not in published:
        op.add_column(
            "published_videos",
            # MySQL rejects defaults on TEXT columns. Add it nullable first so
            # existing rows can be adopted, backfill them, then enforce the
            # model's non-null contract without leaving a server default.
            sa.Column("description", sa.Text(), nullable=True),
        )
        op.execute(
            sa.text(
                "UPDATE published_videos "
                "SET description = '' "
                "WHERE description IS NULL"
            )
        )
        op.alter_column(
            "published_videos",
            "description",
            existing_type=sa.Text(),
            nullable=False,
        )
    if "deleted_at" not in published:
        op.add_column(
            "published_videos",
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "ix_published_videos_deleted_at" not in _indexes("published_videos"):
        op.create_index(
            "ix_published_videos_deleted_at",
            "published_videos",
            ["deleted_at"],
        )

    creations = _columns("creator_creations")
    if "active_version_id" not in creations:
        op.add_column(
            "creator_creations",
            sa.Column("active_version_id", sa.String(length=64), nullable=True),
        )
    if "ix_creator_creations_active_version_id" not in _indexes("creator_creations"):
        op.create_index(
            "ix_creator_creations_active_version_id",
            "creator_creations",
            ["active_version_id"],
        )


def downgrade() -> None:
    # Keep creator_versions rows during code rollback. Only optional columns are
    # removed, matching the repository's non-destructive baseline policy.
    if "ix_creator_creations_active_version_id" in _indexes("creator_creations"):
        op.drop_index("ix_creator_creations_active_version_id", table_name="creator_creations")
    if "active_version_id" in _columns("creator_creations"):
        op.drop_column("creator_creations", "active_version_id")
    if "ix_published_videos_deleted_at" in _indexes("published_videos"):
        op.drop_index("ix_published_videos_deleted_at", table_name="published_videos")
    published = _columns("published_videos")
    if "deleted_at" in published:
        op.drop_column("published_videos", "deleted_at")
    if "description" in published:
        op.drop_column("published_videos", "description")
    if "title" in published:
        op.drop_column("published_videos", "title")
