"""Add creator waitlist email delivery and assigned single-use invites.

Revision ID: 20260829_0016
Revises: 20260829_0015
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260829_0016"
down_revision = "20260829_0015"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {str(item["name"]) for item in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    if "creator_invites" in tables:
        columns = _columns("creator_invites")
        with op.batch_alter_table("creator_invites") as batch:
            if "assigned_user_id" not in columns:
                batch.add_column(sa.Column("assigned_user_id", sa.String(length=64), nullable=True))
                batch.create_index(
                    "ix_creator_invites_assigned_user_id",
                    ["assigned_user_id"],
                    unique=False,
                )

    if "creator_applications" in tables:
        columns = _columns("creator_applications")
        with op.batch_alter_table("creator_applications") as batch:
            if "email" not in columns:
                batch.add_column(
                    sa.Column(
                        "email",
                        sa.String(length=256),
                        nullable=False,
                        server_default="",
                    )
                )
                batch.create_index(
                    "ix_creator_applications_email",
                    ["email"],
                    unique=False,
                )
            if "invite_id" not in columns:
                batch.add_column(sa.Column("invite_id", sa.Integer(), nullable=True))
                batch.create_unique_constraint(
                    "uq_creator_applications_invite_id",
                    ["invite_id"],
                )
            if "invited_at" not in columns:
                batch.add_column(sa.Column("invited_at", sa.DateTime(timezone=True), nullable=True))
            if "email_sent_at" not in columns:
                batch.add_column(sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True))
            if "last_error" not in columns:
                batch.add_column(
                    sa.Column(
                        "last_error",
                        sa.String(length=500),
                        nullable=False,
                        server_default="",
                    )
                )


def downgrade() -> None:
    # Forward-only: emailed codes and their application audit trail must remain intact.
    pass
