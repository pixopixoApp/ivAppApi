"""Add durable CDN cache task outbox.

Revision ID: 20260819_0010
Revises: 20260814_0009
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260819_0010"
down_revision = "20260814_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "cdn_cache_jobs" in inspect(bind).get_table_names():
        return
    op.create_table(
        "cdn_cache_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_task_id", sa.String(length=255), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("error_message", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "operation IN ('prefetch', 'refresh')",
            name="ck_cdn_cache_jobs_operation",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_cdn_cache_jobs_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation",
            "url_hash",
            name="uq_cdn_cache_jobs_operation_url",
        ),
    )
    op.create_index(
        "ix_cdn_cache_jobs_ready",
        "cdn_cache_jobs",
        ["state", "next_attempt_at"],
    )


def downgrade() -> None:
    # Forward-only: cache task history is harmless and useful for incident review.
    pass
