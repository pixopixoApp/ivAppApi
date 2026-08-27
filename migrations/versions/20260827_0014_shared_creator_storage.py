"""Allow creator uploads to share content-addressed storage.

Revision ID: 20260827_0014
Revises: 20260823_0013
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

revision = "20260827_0014"
down_revision = "20260823_0013"
branch_labels = None
depends_on = None

_TABLE = "creator_uploads"
_COLUMN = "storage_key"
_INDEX = "ix_creator_uploads_storage_key"


def _is_storage_key(columns: object) -> bool:
    return list(columns or []) == [_COLUMN]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    constraints = [
        item
        for item in inspector.get_unique_constraints(_TABLE)
        if _is_storage_key(item.get("column_names")) and item.get("name")
    ]
    if constraints and bind.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE) as batch:
            for constraint in constraints:
                batch.drop_constraint(str(constraint["name"]), type_="unique")
    else:
        for constraint in constraints:
            op.drop_constraint(str(constraint["name"]), _TABLE, type_="unique")

    # Some MySQL schemas expose a UNIQUE key only as an index. Refresh after
    # dropping constraints so the same physical key is never dropped twice.
    inspector = inspect(bind)
    for index in inspector.get_indexes(_TABLE):
        if _is_storage_key(index.get("column_names")) and index.get("unique"):
            op.drop_index(str(index["name"]), table_name=_TABLE)

    inspector = inspect(bind)
    has_lookup_index = any(
        _is_storage_key(index.get("column_names"))
        for index in inspector.get_indexes(_TABLE)
    )
    if not has_lookup_index:
        op.create_index(_INDEX, _TABLE, [_COLUMN], unique=False)


def downgrade() -> None:
    # Forward-only: valid duplicate uploads make restoring uniqueness unsafe.
    pass
