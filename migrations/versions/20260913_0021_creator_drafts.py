"""Index account draft discovery without changing existing records."""
import sqlalchemy as sa
from alembic import op

revision = "20260913_0021"
down_revision = "20260911_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("creator_creations")}
    if "ix_creator_drafts_user_updated" not in indexes:
        op.create_index("ix_creator_drafts_user_updated", "creator_creations", ["user_id", "updated_at", "id"])


def downgrade() -> None:
    pass
