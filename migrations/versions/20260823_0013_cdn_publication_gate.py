"""Gate runtime publication on confirmed CDN prefetch completion.

Revision ID: 20260823_0013
Revises: 20260823_0012
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260823_0013"
down_revision = "20260823_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "published_videos" in tables:
        columns = {
            item["name"] for item in inspector.get_columns("published_videos")
        }
        if "cdn_ready" not in columns:
            op.add_column(
                "published_videos",
                sa.Column(
                    "cdn_ready",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.true(),
                ),
            )
        indexes = {
            item["name"] for item in inspect(bind).get_indexes("published_videos")
        }
        if "ix_published_videos_cdn_ready" not in indexes:
            op.create_index(
                "ix_published_videos_cdn_ready",
                "published_videos",
                ["cdn_ready"],
            )

    if "cdn_publication_gates" not in tables:
        op.create_table(
            "cdn_publication_gates",
            sa.Column("publication_id", sa.String(length=64), nullable=False),
            sa.Column("video_id", sa.String(length=128), nullable=False),
            sa.Column("urls", sa.JSON(), nullable=False),
            sa.Column("staged_payload", sa.JSON(), nullable=False),
            sa.Column("state", sa.String(length=16), nullable=False),
            sa.Column("error_message", sa.String(length=500), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "state IN ('warming', 'active', 'failed', 'superseded')",
                name="ck_cdn_publication_gates_state",
            ),
            sa.PrimaryKeyConstraint("publication_id"),
        )
        op.create_index(
            "ix_cdn_publication_gates_state",
            "cdn_publication_gates",
            ["state"],
        )
        op.create_index(
            "ix_cdn_publication_gates_video",
            "cdn_publication_gates",
            ["video_id", "state"],
        )


def downgrade() -> None:
    # Forward-only: readiness history prevents accidental cold publication.
    pass
