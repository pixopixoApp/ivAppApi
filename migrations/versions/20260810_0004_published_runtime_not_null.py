"""Require persisted runtime specs for every published video.

Revision ID: 20260810_0004
Revises: 20260808_0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260810_0004"
down_revision = "20260808_0003"
branch_labels = None
depends_on = None


def _missing_runtime_specs() -> int:
    bind = op.get_bind()
    return int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM published_videos "
                "WHERE runtime_spec IS NULL OR runtime_spec_version IS NULL"
            )
        ).scalar_one()
    )


def upgrade() -> None:
    missing = _missing_runtime_specs()
    if missing:
        raise RuntimeError(
            "published_videos contains "
            f"{missing} rows without runtime specs; run the verified backfill first"
        )
    with op.batch_alter_table("published_videos") as batch:
        batch.alter_column(
            "runtime_spec",
            existing_type=sa.JSON(),
            nullable=False,
        )
        batch.alter_column(
            "runtime_spec_version",
            existing_type=sa.String(length=32),
            nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("published_videos") as batch:
        batch.alter_column(
            "runtime_spec_version",
            existing_type=sa.String(length=32),
            nullable=True,
        )
        batch.alter_column(
            "runtime_spec",
            existing_type=sa.JSON(),
            nullable=True,
        )
