"""Add prompt-generated creator sources and daily quota state.

Revision ID: 20260829_0015
Revises: 20260827_0014
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260829_0015"
down_revision = "20260827_0014"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {str(item["name"]) for item in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "creator_uploads" in tables:
        columns = _columns("creator_uploads")
        with op.batch_alter_table("creator_uploads") as batch:
            if "origin" not in columns:
                batch.add_column(
                    sa.Column(
                        "origin",
                        sa.String(length=24),
                        nullable=False,
                        server_default="user_upload",
                    )
                )
                batch.create_index("ix_creator_uploads_origin", ["origin"], unique=False)
            if "source_generation_id" not in columns:
                batch.add_column(sa.Column("source_generation_id", sa.String(length=64), nullable=True))
                batch.create_index(
                    "ix_creator_uploads_source_generation_id",
                    ["source_generation_id"],
                    unique=False,
                )

    if "creator_creations" in tables:
        columns = _columns("creator_creations")
        with op.batch_alter_table("creator_creations") as batch:
            batch.alter_column(
                "upload_id",
                existing_type=sa.String(length=64),
                nullable=True,
            )
            if "source_mode" not in columns:
                batch.add_column(
                    sa.Column(
                        "source_mode",
                        sa.String(length=16),
                        nullable=False,
                        server_default="upload",
                    )
                )
                batch.create_index("ix_creator_creations_source_mode", ["source_mode"], unique=False)
            if "source_prompt" not in columns:
                batch.add_column(
                    sa.Column(
                        "source_prompt",
                        sa.String(length=1000),
                        nullable=False,
                        server_default="",
                    )
                )
            if "source_generation_id" not in columns:
                batch.add_column(sa.Column("source_generation_id", sa.String(length=64), nullable=True))
                batch.create_index(
                    "ix_creator_creations_source_generation_id",
                    ["source_generation_id"],
                    unique=False,
                )

    if "creator_source_generations" not in tables:
        op.create_table(
            "creator_source_generations",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("creation_id", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("request_id", sa.String(length=128), nullable=False),
            sa.Column("original_prompt", sa.String(length=1000), nullable=False),
            sa.Column("prompt_summary", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("generation_prompt", sa.Text(), nullable=False),
            sa.Column("interaction_brief", sa.String(length=1000), nullable=False, server_default=""),
            sa.Column("preset_json", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="queued"),
            sa.Column("progress_stage", sa.String(length=64), nullable=False, server_default="queued"),
            sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("ivadmin_job_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("provider_task_accepted", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("upload_id", sa.String(length=64), nullable=True),
            sa.Column("quota_date", sa.String(length=10), nullable=False),
            sa.Column("quota_state", sa.String(length=16), nullable=False, server_default="reserved"),
            sa.Column("error_code", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("error_message", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "creation_id", "attempt", name="uq_creator_source_generation_attempt"
            ),
            sa.UniqueConstraint("request_id", name="uq_creator_source_generation_request"),
        )
        op.create_index(
            "ix_creator_source_generations_creation_id",
            "creator_source_generations",
            ["creation_id"],
        )
        op.create_index(
            "ix_creator_source_generations_user_id",
            "creator_source_generations",
            ["user_id"],
        )
        op.create_index(
            "ix_creator_source_generations_status",
            "creator_source_generations",
            ["status"],
        )
        op.create_index(
            "ix_creator_source_generations_request_id",
            "creator_source_generations",
            ["request_id"],
        )
        op.create_index(
            "ix_creator_source_generations_upload_id",
            "creator_source_generations",
            ["upload_id"],
        )
        op.create_index(
            "ix_creator_source_generations_quota_date",
            "creator_source_generations",
            ["quota_date"],
        )
        op.create_index(
            "ix_creator_source_generations_quota_state",
            "creator_source_generations",
            ["quota_state"],
        )
        op.create_index(
            "ix_creator_source_generations_next_poll_at",
            "creator_source_generations",
            ["next_poll_at"],
        )
        op.create_index(
            "ix_creator_source_generations_expires_at",
            "creator_source_generations",
            ["expires_at"],
        )
        op.create_index(
            "ix_creator_source_generations_created_at",
            "creator_source_generations",
            ["created_at"],
        )
        op.create_index(
            "ix_creator_source_generation_quota",
            "creator_source_generations",
            ["user_id", "quota_date", "quota_state"],
        )


def downgrade() -> None:
    # Forward-only: generated drafts may already reference new source uploads.
    pass
