"""Add non-blocking public text audit records.

Revision ID: 20260831_0017
Revises: 20260829_0016
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260831_0017"
down_revision = "20260829_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "public_text_issues" in set(inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "public_text_issues",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("entity_type", sa.String(length=48), nullable=False),
        sa.Column("entity_id", sa.String(length=128), nullable=False),
        sa.Column("field_path", sa.String(length=512), nullable=False),
        sa.Column("detected_scripts", sa.JSON(), nullable=False),
        sa.Column("sample_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "entity_type",
            "entity_id",
            "field_path",
            name="uq_public_text_issues_entity_field",
        ),
    )
    op.create_index(
        "ix_public_text_issues_entity_type",
        "public_text_issues",
        ["entity_type"],
    )
    op.create_index(
        "ix_public_text_issues_entity_id",
        "public_text_issues",
        ["entity_id"],
    )
    op.create_index(
        "ix_public_text_issues_status",
        "public_text_issues",
        ["status"],
    )
    op.create_index(
        "ix_public_text_issues_status_seen",
        "public_text_issues",
        ["status", "last_seen_at"],
    )


def downgrade() -> None:
    # Forward-only: language audit history supports later remediation.
    pass
