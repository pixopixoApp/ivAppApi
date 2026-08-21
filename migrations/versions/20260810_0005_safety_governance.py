"""Add user blocking and content moderation reports.

Revision ID: 20260810_0005
Revises: 20260810_0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260810_0005"
down_revision = "20260810_0004"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    return inspect(op.get_bind()).has_table(table_name)


def _indexes(table_name: str) -> set[str]:
    if not _has_table(table_name):
        return set()
    return {
        index["name"]
        for index in inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def upgrade() -> None:
    # Revision 0001 intentionally bootstraps an empty database from the current
    # SQLAlchemy metadata. That means a fresh install may already contain these
    # tables, while an existing database at revision 0004 will not. Keep this
    # migration safe for both paths.
    if not _has_table("user_blocks"):
        op.create_table(
            "user_blocks",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("blocker_user_id", sa.String(length=64), nullable=False),
            sa.Column("blocked_user_id", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("blocker_user_id", "blocked_user_id", name="uq_user_blocks_pair"),
        )
    block_indexes = _indexes("user_blocks")
    for column in ("blocker_user_id", "blocked_user_id"):
        index_name = f"ix_user_blocks_{column}"
        if index_name not in block_indexes:
            op.create_index(index_name, "user_blocks", [column])

    if not _has_table("content_reports"):
        op.create_table(
            "content_reports",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("reporter_user_id", sa.String(length=64), nullable=False),
            sa.Column("target_type", sa.String(length=16), nullable=False),
            sa.Column("target_id", sa.String(length=128), nullable=False),
            sa.Column("target_user_id", sa.String(length=64), nullable=True),
            sa.Column("reason", sa.String(length=64), nullable=False),
            sa.Column("details", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
            sa.Column("resolution", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("reviewed_by", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "reporter_user_id",
                "target_type",
                "target_id",
                name="uq_content_reports_reporter_target",
            ),
        )
    report_indexes = _indexes("content_reports")
    for column in ("reporter_user_id", "target_type", "target_id", "target_user_id", "status"):
        index_name = f"ix_content_reports_{column}"
        if index_name not in report_indexes:
            op.create_index(index_name, "content_reports", [column])


def downgrade() -> None:
    if _has_table("content_reports"):
        op.drop_table("content_reports")
    if _has_table("user_blocks"):
        op.drop_table("user_blocks")
