"""App update package metadata.

Revision ID: 20260808_0002
Revises: 20260808_0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260808_0002"
down_revision = "20260808_0001"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns("app_versions")}


def upgrade() -> None:
    columns = _columns()
    if "package_name" not in columns:
        op.add_column(
            "app_versions",
            sa.Column("package_name", sa.String(length=255), nullable=False, server_default=""),
        )
    if "size_bytes" not in columns:
        op.add_column(
            "app_versions",
            sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    columns = _columns()
    if "size_bytes" in columns:
        op.drop_column("app_versions", "size_bytes")
    if "package_name" in columns:
        op.drop_column("app_versions", "package_name")
