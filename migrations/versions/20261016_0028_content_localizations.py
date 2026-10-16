"""Add durable content localization results and translation jobs.

Revision ID: 20261016_0028
Revises: 20261008_0027
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261016_0028"
down_revision = "20261008_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "content_localizations" not in tables:
        op.create_table(
            "content_localizations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("entity_type", sa.String(48), nullable=False),
            sa.Column("entity_id", sa.String(128), nullable=False),
            sa.Column("locale", sa.String(16), nullable=False),
            sa.Column("source_locale", sa.String(16), nullable=False, server_default="en"),
            sa.Column("source_hash", sa.String(64), nullable=False),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="ready"),
            sa.Column("model", sa.String(128), nullable=False, server_default=""),
            sa.Column("prompt_version", sa.String(64), nullable=False, server_default=""),
            sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "entity_type", "entity_id", "locale",
                name="uq_content_localizations_entity_locale",
            ),
        )
        op.create_index(
            "ix_content_localizations_entity_type",
            "content_localizations",
            ["entity_type"],
        )
        op.create_index(
            "ix_content_localizations_entity_id",
            "content_localizations",
            ["entity_id"],
        )
        op.create_index(
            "ix_content_localizations_source_hash",
            "content_localizations",
            ["source_hash"],
        )
        op.create_index(
            "ix_content_localizations_status",
            "content_localizations",
            ["status"],
        )
        op.create_index(
            "ix_content_localizations_status_locale",
            "content_localizations",
            ["status", "locale"],
        )

    if "content_translation_jobs" not in tables:
        op.create_table(
            "content_translation_jobs",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("entity_type", sa.String(48), nullable=False),
            sa.Column("entity_id", sa.String(128), nullable=False),
            sa.Column("source_hash", sa.String(64), nullable=False),
            sa.Column("source_locale", sa.String(16), nullable=False, server_default="en"),
            sa.Column("target_locales", sa.JSON(), nullable=False),
            sa.Column("source_payload", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("request_id", sa.String(96), nullable=False),
            sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=False),
            sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("request_id", name="uq_content_translation_jobs_request_id"),
        )
        op.create_index(
            "ix_content_translation_jobs_entity_type",
            "content_translation_jobs",
            ["entity_type"],
        )
        op.create_index(
            "ix_content_translation_jobs_entity_id",
            "content_translation_jobs",
            ["entity_id"],
        )
        op.create_index(
            "ix_content_translation_jobs_source_hash",
            "content_translation_jobs",
            ["source_hash"],
        )
        op.create_index(
            "ix_content_translation_jobs_status",
            "content_translation_jobs",
            ["status"],
        )
        op.create_index(
            "ix_content_translation_jobs_claim",
            "content_translation_jobs",
            ["status", "next_retry_at", "created_at"],
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "content_translation_jobs" in tables:
        op.drop_table("content_translation_jobs")
    if "content_localizations" in tables:
        op.drop_table("content_localizations")
