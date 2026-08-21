"""Add origin/review metadata to published content.

Revision ID: 20260812_0008
Revises: 20260811_0007
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260812_0008"
down_revision = "20260811_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in inspect(bind).get_columns("published_videos")}
    additions = (
        ("content_source", sa.String(24), "pgc"),
        ("review_status", sa.String(24), "approved"),
        ("reviewed_by", sa.String(128), ""),
        ("reviewed_at", sa.DateTime(timezone=True), None),
        ("review_note", sa.String(500), ""),
        ("cover_media_object_id", sa.String(64), None),
    )
    for name, type_, default in additions:
        if name not in columns:
            op.add_column("published_videos", sa.Column(name, type_, nullable=True, server_default=default))
    bind.execute(sa.text("UPDATE published_videos SET content_source='pgc' WHERE content_source IS NULL OR content_source=''"))
    bind.execute(sa.text("UPDATE published_videos SET review_status='approved' WHERE review_status IS NULL OR review_status=''"))
    for name in ("content_source", "review_status", "cover_media_object_id"):
        index = f"ix_published_videos_{name}"
        existing = {item["name"] for item in inspect(bind).get_indexes("published_videos")}
        if index not in existing:
            op.create_index(index, "published_videos", [name])


def downgrade() -> None:
    # Forward-only: old binaries safely ignore these fields; never discard audit data.
    pass
