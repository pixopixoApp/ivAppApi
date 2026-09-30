"""Add creator social engagement, notifications, and anonymous viewer keys.

Revision ID: 20260929_0024
Revises: 20260918_0023
"""
from __future__ import annotations

import hashlib
import hmac
import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260929_0024"
down_revision = "20260918_0023"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tables = _tables()
    if "published_videos" in tables:
        columns = _columns("published_videos")
        with op.batch_alter_table("published_videos") as batch:
            if "like_count" not in columns:
                batch.add_column(sa.Column("like_count", sa.Integer(), nullable=False, server_default="0"))
            if "comment_count" not in columns:
                batch.add_column(sa.Column("comment_count", sa.Integer(), nullable=False, server_default="0"))
    if "users" in tables and "creator_activated_at" not in _columns("users"):
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column("creator_activated_at", sa.DateTime(), nullable=True))
            batch.create_index("ix_users_creator_activated_at", ["creator_activated_at"])

    if "video_views" in tables and "viewer_key" not in _columns("video_views"):
        with op.batch_alter_table("video_views") as batch:
            batch.add_column(sa.Column("viewer_key", sa.String(length=64), nullable=True))
            batch.alter_column("user_id", existing_type=sa.String(length=64), nullable=True)
        secret = os.environ.get("VIEWER_KEY_SECRET") or os.environ.get("CURSOR_SECRET") or "migration"
        connection = op.get_bind()
        rows = connection.execute(sa.text("SELECT id, user_id FROM video_views")).fetchall()
        for row in rows:
            digest = hmac.new(secret.encode(), f"user:{row.user_id}".encode(), hashlib.sha256).hexdigest()
            connection.execute(
                sa.text("UPDATE video_views SET viewer_key=:viewer_key WHERE id=:id"),
                {"viewer_key": digest, "id": row.id},
            )
        with op.batch_alter_table("video_views") as batch:
            batch.alter_column("viewer_key", existing_type=sa.String(length=64), nullable=False)
            batch.create_index("ix_video_views_viewer_key", ["viewer_key"])
            batch.create_unique_constraint("uq_video_views_video_viewer", ["video_id", "viewer_key"])

    if "video_likes" not in tables:
        op.create_table(
            "video_likes",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("video_id", sa.String(128), nullable=False, index=True),
            sa.Column("user_id", sa.String(64), nullable=False, index=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("video_id", "user_id", name="uq_video_likes_video_user"),
        )
        op.create_index("ix_video_likes_user_created", "video_likes", ["user_id", "created_at"])
    if "comments" not in tables:
        op.create_table(
            "comments",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("video_id", sa.String(128), nullable=False, index=True),
            sa.Column("author_user_id", sa.String(64), nullable=False, index=True),
            sa.Column("root_comment_id", sa.String(64), nullable=True, index=True),
            sa.Column("reply_to_user_id", sa.String(64), nullable=True),
            sa.Column("body", sa.String(1120), nullable=False, server_default=""),
            sa.Column("moderation_status", sa.String(16), nullable=False, server_default="visible", index=True),
            sa.Column("like_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("reply_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("ix_comments_video_root_created", "comments", ["video_id", "root_comment_id", "created_at"])
        op.create_index("ix_comments_author_created", "comments", ["author_user_id", "created_at"])
    if "comment_likes" not in tables:
        op.create_table(
            "comment_likes",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("comment_id", sa.String(64), nullable=False, index=True),
            sa.Column("user_id", sa.String(64), nullable=False, index=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("comment_id", "user_id", name="uq_comment_likes_comment_user"),
        )
    if "social_notifications" not in tables:
        op.create_table(
            "social_notifications",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("recipient_user_id", sa.String(64), nullable=False, index=True),
            sa.Column("actor_user_id", sa.String(64), nullable=False, index=True),
            sa.Column("type", sa.String(24), nullable=False, index=True),
            sa.Column("video_id", sa.String(128), nullable=True, index=True),
            sa.Column("comment_id", sa.String(64), nullable=True, index=True),
            sa.Column("dedupe_key", sa.String(320), nullable=False, unique=True),
            sa.Column("read_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("ix_social_notifications_recipient_created", "social_notifications", ["recipient_user_id", "created_at"])
    if "social_rate_events" not in tables:
        op.create_table(
            "social_rate_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.String(64), nullable=False, index=True),
            sa.Column("kind", sa.String(24), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("ix_social_rate_events_user_created", "social_rate_events", ["user_id", "created_at"])

    connection = op.get_bind()
    connection.execute(sa.text(
        "UPDATE users SET creator_activated_at = ("
        "SELECT MIN(created_at) FROM published_videos WHERE published_videos.user_id = users.user_id"
        ") WHERE creator_activated_at IS NULL AND EXISTS ("
        "SELECT 1 FROM published_videos WHERE published_videos.user_id = users.user_id)"
    ))


def downgrade() -> None:
    # Social data is user content. Keep it intact during application rollback.
    pass
