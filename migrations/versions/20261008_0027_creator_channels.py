"""Add creator channels, permanent handles, topics, links, and pinned works.

Revision ID: 20261008_0027
Revises: 20261003_0026
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

import sqlalchemy as sa
from alembic import op

revision = "20261008_0027"
down_revision = "20261003_0026"
branch_labels = None
depends_on = None

RESERVED = {
    "admin", "api", "assets", "create", "download", "explore", "help",
    "login", "me", "media", "moderator", "privacy", "settings", "support",
    "terms", "videos", "www", "pixopixo", "pixo",
}
TOPICS = (
    ("art-design", "Art & Design"),
    ("beauty-fashion", "Beauty & Fashion"),
    ("comedy", "Comedy"),
    ("education", "Education"),
    ("food", "Food"),
    ("games", "Games"),
    ("music-dance", "Music & Dance"),
    ("pets-animals", "Pets & Animals"),
    ("sports-fitness", "Sports & Fitness"),
    ("travel", "Travel"),
    ("lifestyle", "Lifestyle"),
    ("challenges", "Challenges"),
)


def _slug(raw: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", raw or "").encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-z0-9_]+", "_", ascii_value.lower()).strip("_")
    value = re.sub(r"_+", "_", value)
    return value[:30]


def _candidate(nickname: str, user_id: str, used: set[str]) -> str:
    base = _slug(nickname)
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    candidates: list[str] = []
    if len(base) >= 3 and base not in RESERVED:
        candidates.extend((base, f"{base[:21]}_{digest[:8]}"))
    candidates.append(f"pixo_{digest[:10]}")
    for candidate in candidates:
        if candidate not in used and candidate not in RESERVED:
            return candidate
    for length in range(11, 25):
        candidate = f"pixo_{digest[:length]}"[:30]
        if candidate not in used:
            return candidate
    raise RuntimeError(f"could not allocate handle for {user_id}")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    columns = {column["name"] for column in inspector.get_columns("users")}
    with op.batch_alter_table("users") as batch:
        if "handle" not in columns:
            batch.add_column(sa.Column("handle", sa.String(30), nullable=True))
        if "handle_changed_at" not in columns:
            batch.add_column(sa.Column("handle_changed_at", sa.DateTime(timezone=True), nullable=True))
        if "profile_updated_at" not in columns:
            batch.add_column(sa.Column("profile_updated_at", sa.DateTime(timezone=True), nullable=True))
        # MySQL rejects defaults on TEXT columns. Add these as nullable first so
        # the migration also works on populated tables, backfill them below,
        # then make them non-null without a server default.
        if "background_url" not in columns:
            batch.add_column(sa.Column("background_url", sa.Text(), nullable=True))
        if "background_mobile_url" not in columns:
            batch.add_column(sa.Column("background_mobile_url", sa.Text(), nullable=True))
        if "background_desktop_url" not in columns:
            batch.add_column(sa.Column("background_desktop_url", sa.Text(), nullable=True))
        if "background_focus_x" not in columns:
            batch.add_column(sa.Column("background_focus_x", sa.Float(), nullable=False, server_default="0.5"))
        if "background_focus_y" not in columns:
            batch.add_column(sa.Column("background_focus_y", sa.Float(), nullable=False, server_default="0.5"))
        if "content_language" not in columns:
            batch.add_column(sa.Column("content_language", sa.String(35), nullable=False, server_default=""))
        if "collaboration_email" not in columns:
            batch.add_column(sa.Column("collaboration_email", sa.String(256), nullable=False, server_default=""))
        if "collaboration_email_public" not in columns:
            batch.add_column(sa.Column("collaboration_email_public", sa.Boolean(), nullable=False, server_default=sa.false()))

    for column_name in (
        "background_url",
        "background_mobile_url",
        "background_desktop_url",
    ):
        bind.execute(sa.text(f"UPDATE users SET {column_name} = '' WHERE {column_name} IS NULL"))
    with op.batch_alter_table("users") as batch:
        for column_name in (
            "background_url",
            "background_mobile_url",
            "background_desktop_url",
        ):
            batch.alter_column(
                column_name,
                existing_type=sa.Text(),
                nullable=False,
                server_default=None,
            )

    rows = bind.execute(sa.text(
        "SELECT user_id, nickname, internal_purpose FROM users "
        "WHERE handle IS NULL ORDER BY created_at ASC, user_id ASC"
    )).mappings().all()
    used = {
        str(value) for (value,) in bind.execute(
            sa.text("SELECT handle FROM users WHERE handle IS NOT NULL")
        ).all()
    }
    for row in rows:
        if row["internal_purpose"] not in (None, "", "social_seed"):
            continue
        handle = _candidate(str(row["nickname"] or ""), str(row["user_id"]), used)
        bind.execute(
            sa.text("UPDATE users SET handle = :handle WHERE user_id = :user_id"),
            {"handle": handle, "user_id": row["user_id"]},
        )
        used.add(handle)

    uniques = {
        item.get("name") for item in sa.inspect(bind).get_unique_constraints("users")
    }
    indexes = {item.get("name") for item in sa.inspect(bind).get_indexes("users")}
    with op.batch_alter_table("users") as batch:
        if "uq_users_handle" not in uniques:
            batch.create_unique_constraint("uq_users_handle", ["handle"])
        if "ix_users_handle" not in indexes:
            batch.create_index("ix_users_handle", ["handle"], unique=False)

    if "creator_handle_aliases" not in tables:
        op.create_table(
            "creator_handle_aliases",
            sa.Column("handle", sa.String(30), primary_key=True),
            sa.Column("user_id", sa.String(64), nullable=False, index=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "creator_external_links" not in tables:
        op.create_table(
            "creator_external_links",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.String(64), nullable=False, index=True),
            sa.Column("label", sa.String(40), nullable=False, server_default=""),
            sa.Column("url", sa.String(2048), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "position", name="uq_creator_external_links_position"),
        )
    if "creator_pinned_works" not in tables:
        op.create_table(
            "creator_pinned_works",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.String(64), nullable=False, index=True),
            sa.Column("video_id", sa.String(128), nullable=False, index=True),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "video_id", name="uq_creator_pinned_works_video"),
            sa.UniqueConstraint("user_id", "position", name="uq_creator_pinned_works_position"),
        )
    if "creator_topics" not in tables:
        op.create_table(
            "creator_topics",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("name", sa.String(60), nullable=False, unique=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        for topic_id, name in TOPICS:
            bind.execute(sa.text(
                "INSERT INTO creator_topics (id, name, enabled, created_at, updated_at) "
                "VALUES (:id, :name, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ), {"id": topic_id, "name": name})
    if "creator_topic_assignments" not in tables:
        op.create_table(
            "creator_topic_assignments",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.String(64), nullable=False, index=True),
            sa.Column("topic_id", sa.String(64), nullable=False, index=True),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "topic_id", name="uq_creator_topic_assignments_pair"),
        )
    if "creator_profile_audits" not in tables:
        op.create_table(
            "creator_profile_audits",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.String(64), nullable=True),
            sa.Column("action", sa.String(64), nullable=False, index=True),
            sa.Column("actor_id", sa.String(128), nullable=False),
            sa.Column("actor_role", sa.String(32), nullable=False, server_default="creator"),
            sa.Column("source", sa.String(32), nullable=False, server_default="web"),
            sa.Column("before_json", sa.JSON(), nullable=True),
            sa.Column("after_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_creator_profile_audits_user_created",
            "creator_profile_audits",
            ["user_id", "created_at"],
        )


def downgrade() -> None:
    # Handles and audit history are durable public identity data. A rollback of
    # application code must not make old public URLs reusable.
    pass
