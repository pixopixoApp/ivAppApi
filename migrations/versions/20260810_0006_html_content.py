"""Add reviewed remote HTML as a first-class published content type.

Revision ID: 20260810_0006
Revises: 20260810_0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260810_0006"
down_revision = "20260810_0005"
branch_labels = None
depends_on = None

_PAYLOAD_CHECK = "ck_published_videos_content_payload"


def _columns() -> dict[str, dict]:
    return {
        column["name"]: column
        for column in inspect(op.get_bind()).get_columns("published_videos")
    }


def _indexes() -> set[str]:
    return {
        index["name"]
        for index in inspect(op.get_bind()).get_indexes("published_videos")
        if index.get("name")
    }


def _checks() -> set[str]:
    return {
        constraint["name"]
        for constraint in inspect(op.get_bind()).get_check_constraints("published_videos")
        if constraint.get("name")
    }


def upgrade() -> None:
    columns = _columns()
    if "content_type" not in columns:
        op.add_column(
            "published_videos",
            sa.Column(
                "content_type",
                sa.String(length=16),
                nullable=False,
                server_default="runtime",
            ),
        )
    if "html_url" not in columns:
        op.add_column(
            "published_videos",
            sa.Column("html_url", sa.Text(), nullable=True),
        )
    if "bridge_version" not in columns:
        op.add_column(
            "published_videos",
            sa.Column("bridge_version", sa.Integer(), nullable=True),
        )
    if "required_capabilities" not in columns:
        op.add_column(
            "published_videos",
            sa.Column("required_capabilities", sa.JSON(), nullable=True),
        )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE published_videos SET content_type = 'runtime' "
            "WHERE content_type IS NULL OR content_type = ''"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE published_videos SET required_capabilities = '[]' "
            "WHERE required_capabilities IS NULL"
        )
    )

    columns = _columns()
    needs_payload_alter = any(
        not columns[name].get("nullable", True)
        for name in ("video_url", "timeline", "runtime_spec", "runtime_spec_version")
    ) or columns["required_capabilities"].get("nullable", True)
    needs_check = _PAYLOAD_CHECK not in _checks()
    if needs_payload_alter or needs_check:
        with op.batch_alter_table("published_videos") as batch:
            if not columns["video_url"].get("nullable", True):
                batch.alter_column("video_url", existing_type=sa.Text(), nullable=True)
            if not columns["timeline"].get("nullable", True):
                batch.alter_column("timeline", existing_type=sa.JSON(), nullable=True)
            if not columns["runtime_spec"].get("nullable", True):
                batch.alter_column("runtime_spec", existing_type=sa.JSON(), nullable=True)
            if not columns["runtime_spec_version"].get("nullable", True):
                batch.alter_column(
                    "runtime_spec_version",
                    existing_type=sa.String(length=32),
                    nullable=True,
                )
            if columns["required_capabilities"].get("nullable", True):
                batch.alter_column(
                    "required_capabilities",
                    existing_type=sa.JSON(),
                    nullable=False,
                )
            if needs_check:
                batch.create_check_constraint(
                    _PAYLOAD_CHECK,
                    "(content_type = 'runtime' AND video_url IS NOT NULL "
                    "AND timeline IS NOT NULL AND runtime_spec IS NOT NULL "
                    "AND runtime_spec_version IS NOT NULL AND html_url IS NULL "
                    "AND bridge_version IS NULL) OR "
                    "(content_type = 'html' AND video_url IS NULL AND timeline IS NULL "
                    "AND runtime_spec IS NULL AND runtime_spec_version IS NULL "
                    "AND html_url IS NOT NULL AND bridge_version = 1)",
                )

    if "ix_published_videos_content_type" not in _indexes():
        op.create_index(
            "ix_published_videos_content_type",
            "published_videos",
            ["content_type"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    html_count = int(
        bind.execute(
            sa.text("SELECT COUNT(*) FROM published_videos WHERE content_type = 'html'")
        ).scalar_one()
    )
    if html_count:
        raise RuntimeError("remove HTML published items before downgrading revision 0006")

    if "ix_published_videos_content_type" in _indexes():
        op.drop_index("ix_published_videos_content_type", table_name="published_videos")
    with op.batch_alter_table("published_videos") as batch:
        if _PAYLOAD_CHECK in _checks():
            batch.drop_constraint(_PAYLOAD_CHECK, type_="check")
        batch.alter_column("video_url", existing_type=sa.Text(), nullable=False)
        batch.alter_column("timeline", existing_type=sa.JSON(), nullable=False)
        batch.alter_column("runtime_spec", existing_type=sa.JSON(), nullable=False)
        batch.alter_column(
            "runtime_spec_version",
            existing_type=sa.String(length=32),
            nullable=False,
        )
        for column in (
            "required_capabilities",
            "bridge_version",
            "html_url",
            "content_type",
        ):
            if column in _columns():
                batch.drop_column(column)
