"""Add configurable referral rewards and per-binding snapshots.

Revision ID: 20261003_0025
Revises: 20260929_0024
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261003_0025"
down_revision = "20260929_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "referral_reward_config" not in tables:
        op.create_table(
            "referral_reward_config",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("inviter_activation_reward_credits", sa.Integer(), nullable=False),
            sa.Column("invitee_registration_reward_credits", sa.Integer(), nullable=False),
            sa.Column("updated_by", sa.String(length=128), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "referral_reward_config_history" not in tables:
        op.create_table(
            "referral_reward_config_history",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("inviter_activation_reward_credits", sa.Integer(), nullable=False),
            sa.Column("invitee_registration_reward_credits", sa.Integer(), nullable=False),
            sa.Column("updated_by", sa.String(length=128), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("version", name="uq_referral_reward_config_history_version"),
        )
    if "app_handoff_codes" not in tables:
        op.create_table(
            "app_handoff_codes",
            sa.Column("code_hash", sa.String(length=64), primary_key=True),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_app_handoff_codes_user_id", "app_handoff_codes", ["user_id"])
        op.create_index("ix_app_handoff_codes_expires_at", "app_handoff_codes", ["expires_at"])

    columns = {column["name"] for column in inspector.get_columns("referral_bindings")}
    with op.batch_alter_table("referral_bindings") as batch:
        if "config_version" not in columns:
            batch.add_column(sa.Column("config_version", sa.Integer(), nullable=False, server_default="0"))
        if "inviter_reward_credits" not in columns:
            batch.add_column(sa.Column("inviter_reward_credits", sa.Integer(), nullable=False, server_default="10"))
        if "invitee_reward_credits" not in columns:
            batch.add_column(sa.Column("invitee_reward_credits", sa.Integer(), nullable=False, server_default="0"))
        if "invitee_rewarded_at" not in columns:
            batch.add_column(sa.Column("invitee_rewarded_at", sa.DateTime(timezone=True), nullable=True))

    now = sa.func.now()
    config = sa.table(
        "referral_reward_config",
        sa.column("id", sa.Integer()),
        sa.column("version", sa.Integer()),
        sa.column("inviter_activation_reward_credits", sa.Integer()),
        sa.column("invitee_registration_reward_credits", sa.Integer()),
        sa.column("updated_by", sa.String()),
        sa.column("updated_at", sa.DateTime()),
    )
    history = sa.table(
        "referral_reward_config_history",
        sa.column("version", sa.Integer()),
        sa.column("inviter_activation_reward_credits", sa.Integer()),
        sa.column("invitee_registration_reward_credits", sa.Integer()),
        sa.column("updated_by", sa.String()),
        sa.column("updated_at", sa.DateTime()),
    )
    existing = bind.execute(sa.select(config.c.id).where(config.c.id == 1)).first()
    if existing is None:
        bind.execute(
            config.insert().values(
                id=1,
                version=1,
                inviter_activation_reward_credits=5,
                invitee_registration_reward_credits=5,
                updated_by="system",
                updated_at=now,
            )
        )
        bind.execute(
            history.insert().values(
                version=1,
                inviter_activation_reward_credits=5,
                invitee_registration_reward_credits=5,
                updated_by="system",
                updated_at=now,
            )
        )


def downgrade() -> None:
    pass
