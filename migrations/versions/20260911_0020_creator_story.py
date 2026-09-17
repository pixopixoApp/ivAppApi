"""Add creator Story drafts and reuse durable video generation jobs.

Revision ID: 20260911_0020
Revises: 20260911_0019
"""
import sqlalchemy as sa
from alembic import op

revision = "20260911_0020"
down_revision = "20260911_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    additions = {
        "creator_creations": [
            sa.Column("request_id", sa.String(128), nullable=True),
            sa.Column("experience_mode", sa.String(16), nullable=False, server_default="auto"),
            sa.Column("story_plan", sa.JSON(), nullable=True),
        ],
        "creator_source_generations": [
            sa.Column("generation_kind", sa.String(16), nullable=False, server_default="source"),
            sa.Column("input_json", sa.JSON(), nullable=True),
        ],
        "creator_versions": [sa.Column("previewed_paths", sa.JSON(), nullable=True)],
    }
    for table, columns in additions.items():
        present = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}
        for column in columns:
            if column.name not in present:
                op.add_column(table, column)
    indexes = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("creator_creations")}
    if "uq_creator_creations_request" not in indexes:
        op.create_index("uq_creator_creations_request", "creator_creations", ["request_id"], unique=True)


def downgrade() -> None:
    # Forward-only: preserve creators' drafts, generated assets and confirmations.
    pass
