"""Add creator local-ingress and normalized-playable bindings.

Revision ID: 20260823_0012
Revises: 20260819_0011
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260823_0012"
down_revision = "20260819_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "creator_uploads" not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns("creator_uploads")}
    additions = (
        ("source_local_uri", sa.String(length=512), ""),
        ("source_sha256", sa.String(length=64), ""),
        ("upload_transport", sa.String(length=32), "oss"),
        ("normalization_job_id", sa.String(length=64), ""),
        ("normalization_status", sa.String(length=24), "pending"),
        ("normalization_profile", sa.String(length=32), "mobile-v1"),
        ("normalization_error", sa.String(length=500), ""),
        ("playable_local_uri", sa.String(length=512), ""),
        ("playable_sha256", sa.String(length=64), ""),
    )
    with op.batch_alter_table("creator_uploads") as batch:
        for name, column_type, default in additions:
            if name not in columns:
                batch.add_column(
                    sa.Column(name, column_type, nullable=False, server_default=default)
                )
        if "playable_media_object_id" not in columns:
            batch.add_column(sa.Column("playable_media_object_id", sa.String(length=64), nullable=True))
        if "playable_size_bytes" not in columns:
            batch.add_column(sa.Column("playable_size_bytes", sa.BigInteger(), nullable=True))

    refreshed = {item["name"] for item in inspect(bind).get_indexes("creator_uploads")}
    for name, columns_to_index in (
        ("ix_creator_uploads_source_sha256", ["source_sha256"]),
        ("ix_creator_uploads_normalization_job_id", ["normalization_job_id"]),
        ("ix_creator_uploads_normalization_status", ["normalization_status"]),
        ("ix_creator_uploads_playable_media_object_id", ["playable_media_object_id"]),
        ("ix_creator_uploads_playable_sha256", ["playable_sha256"]),
    ):
        if name not in refreshed:
            op.create_index(name, "creator_uploads", columns_to_index)

    op.execute(
        "UPDATE creator_uploads SET source_sha256 = COALESCE((SELECT sha256 FROM media_objects "
        "WHERE media_objects.id = creator_uploads.media_object_id), source_sha256) "
        "WHERE source_sha256 = '' AND media_object_id IS NOT NULL"
    )
    # Existing OSS sources migrate under an explicit canary/batch command.
    # Newly finalized uploads set ``pending`` in application code.
    op.execute(
        "UPDATE creator_uploads SET normalization_status = 'legacy_pending' "
        "WHERE media_object_id IS NOT NULL"
    )


def downgrade() -> None:
    # Forward-only: media identities are required for safe publication.
    pass
