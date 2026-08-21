"""Incremental backend schema and persisted runtime specs.

Revision ID: 20260808_0001
Revises:
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from app import models  # noqa: F401
from app.db import Base

revision = "20260808_0001"
down_revision = None
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    # This repository inherited an existing production schema with no migration
    # history.  checkfirst creates a complete fresh database while the guarded
    # additions below adopt the existing one without destructive recreation.
    Base.metadata.create_all(bind=bind, checkfirst=True)

    if "runtime_spec" not in _columns("published_videos"):
        op.add_column("published_videos", sa.Column("runtime_spec", sa.JSON(), nullable=True))
    if "runtime_spec_version" not in _columns("published_videos"):
        op.add_column(
            "published_videos",
            sa.Column("runtime_spec_version", sa.String(length=32), nullable=True),
        )
    if "ix_published_videos_runtime_spec_version" not in _indexes("published_videos"):
        op.create_index(
            "ix_published_videos_runtime_spec_version",
            "published_videos",
            ["runtime_spec_version"],
        )

    if "purpose" not in _columns("email_codes"):
        op.add_column(
            "email_codes",
            sa.Column(
                "purpose",
                sa.String(length=32),
                nullable=False,
                server_default="login",
            ),
        )
    if "ix_email_codes_purpose" not in _indexes("email_codes"):
        op.create_index("ix_email_codes_purpose", "email_codes", ["purpose"])

    if "bio" not in _columns("users"):
        op.add_column(
            "users",
            sa.Column("bio", sa.String(length=80), nullable=False, server_default=""),
        )


def downgrade() -> None:
    # Existing baseline tables/data are intentionally preserved.  Only fields
    # introduced by this revision are removed; new capability tables are left in
    # place to avoid accidental production data loss during a code rollback.
    if "bio" in _columns("users"):
        op.drop_column("users", "bio")
    if "ix_email_codes_purpose" in _indexes("email_codes"):
        op.drop_index("ix_email_codes_purpose", table_name="email_codes")
    if "purpose" in _columns("email_codes"):
        op.drop_column("email_codes", "purpose")
    if "ix_published_videos_runtime_spec_version" in _indexes("published_videos"):
        op.drop_index(
            "ix_published_videos_runtime_spec_version",
            table_name="published_videos",
        )
    if "runtime_spec_version" in _columns("published_videos"):
        op.drop_column("published_videos", "runtime_spec_version")
    if "runtime_spec" in _columns("published_videos"):
        op.drop_column("published_videos", "runtime_spec")
