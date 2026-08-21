"""Add OSS-backed immutable media objects and publication bindings.

Revision ID: 20260811_0007
Revises: 20260810_0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from app.db import Base

revision = "20260811_0007"
down_revision = "20260810_0006"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _has_table(table: str) -> bool:
    return table in inspect(op.get_bind()).get_table_names()


def _indexes(table: str) -> set[str]:
    inspector = inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {
        index["name"]
        for index in inspector.get_indexes(table)
        if index.get("name")
    }


def _add_index(table: str, name: str, columns: list[str], *, unique: bool = False) -> None:
    if not _has_table(table):
        return
    if name not in _indexes(table):
        op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    published_columns = _columns("published_videos")
    if "active_publication_id" not in published_columns:
        op.add_column(
            "published_videos",
            sa.Column("active_publication_id", sa.String(length=64), nullable=True),
        )
    if "html_package_id" not in published_columns:
        op.add_column(
            "published_videos",
            sa.Column("html_package_id", sa.String(length=64), nullable=True),
        )

    user_columns = _columns("users")
    if _has_table("users") and "avatar_media_object_id" not in user_columns:
        op.add_column(
            "users",
            sa.Column("avatar_media_object_id", sa.String(length=64), nullable=True),
        )

    upload_columns = _columns("creator_uploads")
    if _has_table("creator_uploads") and "media_object_id" not in upload_columns:
        op.add_column(
            "creator_uploads",
            sa.Column("media_object_id", sa.String(length=64), nullable=True),
        )

    # create_all is intentionally limited to new tables here. Existing tables
    # have already been altered explicitly above.
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)

    _add_index(
        "published_videos",
        "ix_published_videos_active_publication_id",
        ["active_publication_id"],
    )
    _add_index(
        "published_videos",
        "ix_published_videos_html_package_id",
        ["html_package_id"],
    )
    _add_index(
        "users",
        "ix_users_avatar_media_object_id",
        ["avatar_media_object_id"],
    )
    _add_index(
        "creator_uploads",
        "ix_creator_uploads_media_object_id",
        ["media_object_id"],
        unique=True,
    )


def downgrade() -> None:
    # Media objects are intentionally permanent. A rollback may deploy the old
    # application, but must never drop provenance tables or OSS bindings.
    pass
