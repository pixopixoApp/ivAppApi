"""Isolate internal social seed accounts and interactions.

Revision ID: 20261003_0026
Revises: 20261003_0025
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261003_0026"
down_revision = "20261003_0025"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _uniques(table: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table)
        if constraint.get("name")
    }


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "users" in tables:
        columns = _columns("users")
        indexes = _indexes("users")
        with op.batch_alter_table("users") as batch:
            if "internal_purpose" not in columns:
                batch.add_column(sa.Column("internal_purpose", sa.String(32), nullable=True))
                batch.create_index("ix_users_internal_purpose", ["internal_purpose"])
            if "internal_batch" not in columns:
                batch.add_column(sa.Column("internal_batch", sa.String(64), nullable=True))
                batch.create_index("ix_users_internal_batch", ["internal_batch"])
            if "ix_users_internal_purpose_batch" not in indexes:
                batch.create_index(
                    "ix_users_internal_purpose_batch",
                    ["internal_purpose", "internal_batch"],
                )

    if "published_videos" in tables:
        columns = _columns("published_videos")
        with op.batch_alter_table("published_videos") as batch:
            if "seed_like_count" not in columns:
                batch.add_column(
                    sa.Column("seed_like_count", sa.Integer(), nullable=False, server_default="0")
                )
            if "seed_comment_count" not in columns:
                batch.add_column(
                    sa.Column("seed_comment_count", sa.Integer(), nullable=False, server_default="0")
                )

    if "video_likes" in tables and "is_seed" not in _columns("video_likes"):
        with op.batch_alter_table("video_likes") as batch:
            batch.add_column(
                sa.Column("is_seed", sa.Boolean(), nullable=False, server_default=sa.false())
            )
            batch.create_index("ix_video_likes_is_seed", ["is_seed"])

    if "comments" in tables:
        columns = _columns("comments")
        uniques = _uniques("comments")
        with op.batch_alter_table("comments") as batch:
            if "is_seed" not in columns:
                batch.add_column(
                    sa.Column("is_seed", sa.Boolean(), nullable=False, server_default=sa.false())
                )
                batch.create_index("ix_comments_is_seed", ["is_seed"])
            if "idempotency_key" not in columns:
                batch.add_column(sa.Column("idempotency_key", sa.String(128), nullable=True))
            if "uq_comments_author_idempotency" not in uniques:
                batch.create_unique_constraint(
                    "uq_comments_author_idempotency",
                    ["author_user_id", "idempotency_key"],
                )

    if "social_seed_preview_config" not in tables:
        op.create_table(
            "social_seed_preview_config",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("updated_by", sa.String(128), nullable=False, server_default="system"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        bind.execute(
            sa.text(
                "INSERT INTO social_seed_preview_config "
                "(id, enabled, version, updated_by, updated_at) "
                "VALUES (1, 0, 1, 'system', CURRENT_TIMESTAMP)"
            )
        )


def downgrade() -> None:
    # Seed records are operational audit data. Keep schema/data intact on rollback.
    pass
