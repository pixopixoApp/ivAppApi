"""Add durable Credits and web-first referral attribution.

Revision ID: 20260911_0019
Revises: 20260901_0018
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260911_0019"
down_revision = "20260901_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    if "credit_ledger_entries" not in tables:
        op.create_table(
            "credit_ledger_entries",
            sa.Column("id", sa.String(length=128), primary_key=True),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("reservation_id", sa.String(length=64), nullable=True),
            sa.Column("reference_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("note", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_credit_ledger_entries_user_id", "credit_ledger_entries", ["user_id"])
        op.create_index("ix_credit_ledger_entries_kind", "credit_ledger_entries", ["kind"])
        op.create_index("ix_credit_ledger_entries_reservation_id", "credit_ledger_entries", ["reservation_id"])
        op.create_index("ix_credit_ledger_entries_reference_id", "credit_ledger_entries", ["reference_id"])
    if "credit_reservations" not in tables:
        op.create_table(
            "credit_reservations",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("reference_id", sa.String(length=128), nullable=False),
            sa.Column("purpose", sa.String(length=32), nullable=False),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="reserved"),
            sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "reference_id", name="uq_credit_reservation_reference"),
        )
        op.create_index("ix_credit_reservations_user_id", "credit_reservations", ["user_id"])
        op.create_index("ix_credit_reservations_reference_id", "credit_reservations", ["reference_id"])
        op.create_index("ix_credit_reservations_status", "credit_reservations", ["status"])
    if "referral_invites" not in tables:
        op.create_table(
            "referral_invites",
            sa.Column("owner_user_id", sa.String(length=64), primary_key=True),
            sa.Column("code", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("code", name="uq_referral_invites_code"),
        )
        op.create_index("ix_referral_invites_code", "referral_invites", ["code"])
    if "referral_bindings" not in tables:
        op.create_table(
            "referral_bindings",
            sa.Column("invitee_user_id", sa.String(length=64), primary_key=True),
            sa.Column("inviter_user_id", sa.String(length=64), nullable=False),
            sa.Column("invite_code", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="pending_activation"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_referral_bindings_inviter_user_id", "referral_bindings", ["inviter_user_id"])
        op.create_index("ix_referral_bindings_status", "referral_bindings", ["status"])

    # Idempotent historical welcome-credit grant. New users are provisioned by
    # app.users.get_or_create_user after this migration is deployed.
    dialect = op.get_bind().dialect.name
    if dialect == "mysql":
        op.execute(
            """
            INSERT INTO credit_ledger_entries
              (id, user_id, kind, amount, reservation_id, reference_id, note, created_at)
            SELECT CONCAT('welcome:', user_id), user_id, 'welcome', 5, NULL, user_id,
                   'Welcome Credits', UTC_TIMESTAMP()
            FROM users
            WHERE enabled = 1
            ON DUPLICATE KEY UPDATE id = id
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            INSERT OR IGNORE INTO credit_ledger_entries
              (id, user_id, kind, amount, reservation_id, reference_id, note, created_at)
            SELECT 'welcome:' || user_id, user_id, 'welcome', 5, NULL, user_id,
                   'Welcome Credits', CURRENT_TIMESTAMP
            FROM users
            WHERE enabled = 1
            """
        )
    else:
        op.execute(
            """
            INSERT INTO credit_ledger_entries
              (id, user_id, kind, amount, reservation_id, reference_id, note, created_at)
            SELECT 'welcome:' || user_id, user_id, 'welcome', 5, NULL, user_id,
                   'Welcome Credits', CURRENT_TIMESTAMP
            FROM users
            WHERE enabled = true
              AND NOT EXISTS (
                SELECT 1 FROM credit_ledger_entries e
                WHERE e.id = 'welcome:' || users.user_id
              )
            """
        )


def downgrade() -> None:
    # Forward-only: financial audit rows and referral attribution are retained.
    pass
