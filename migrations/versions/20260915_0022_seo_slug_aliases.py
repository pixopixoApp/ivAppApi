"""Shorten SEO slugs while preserving every published permalink.

Revision ID: 20260915_0022
Revises: 20260913_0021
"""
from __future__ import annotations

import re
import secrets
import unicodedata
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "20260915_0022"
down_revision = "20260913_0021"
branch_labels = None
depends_on = None

_EXCLUDED_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "in",
        "on",
        "at",
        "for",
        "to",
        "with",
        "and",
        "or",
        "of",
        "by",
        "is",
        "are",
        "ultimate",
        "best",
        "amazing",
        "awesome",
        "easy",
        "simple",
        "complete",
        "guide",
        "how",
        "overview",
        "review",
        "step",
        "steps",
        "tips",
        "top",
        "tricks",
        "ways",
    }
)


def _slug_stem(value: str) -> str:
    ascii_text = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode()
        .lower()
        .replace("'", "")
        .replace(".", "")
    )
    words: list[str] = []
    for word in re.findall(r"[a-z0-9]+", ascii_text):
        if word in _EXCLUDED_WORDS:
            continue
        if word.isdigit() or re.fullmatch(r"(?:top|best)\d+|\d+(?:top|best)|v\d+", word):
            continue
        words.append(word[:40])
        if len(words) == 5:
            break
    stem = "-".join(words).strip("-")[:120].rstrip("-")
    return stem or "interactive-video"


def _allocate_slug(stem: str, occupied: set[str]) -> str:
    for _attempt in range(1_000):
        candidate = f"{stem}-{secrets.randbelow(90_000) + 10_000:05d}"
        if candidate not in occupied:
            return candidate
    raise RuntimeError(f"could not allocate a five-digit SEO slug for {stem}")


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "published_video_seo_slug_aliases" not in tables:
        op.create_table(
            "published_video_seo_slug_aliases",
            sa.Column("slug", sa.String(length=180), primary_key=True),
            sa.Column(
                "video_id",
                sa.String(length=128),
                sa.ForeignKey("published_video_seo.video_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_published_video_seo_slug_aliases_video_id",
            "published_video_seo_slug_aliases",
            ["video_id"],
        )

    rows = list(
        bind.execute(
            sa.text(
                "SELECT video_id, slug, page_title "
                "FROM published_video_seo ORDER BY video_id"
            )
        ).mappings()
    )
    occupied = {str(row["slug"]) for row in rows}
    updates: list[tuple[str, str, str]] = []
    for row in rows:
        old_slug = str(row["slug"])
        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*-\d{5}", old_slug):
            continue
        new_slug = _allocate_slug(_slug_stem(str(row["page_title"] or "")), occupied)
        occupied.add(new_slug)
        updates.append((str(row["video_id"]), old_slug, new_slug))

    migrated_at = datetime.now(timezone.utc)
    for video_id, old_slug, new_slug in updates:
        bind.execute(
            sa.text(
                "INSERT INTO published_video_seo_slug_aliases "
                "(slug, video_id, created_at) "
                "VALUES (:slug, :video_id, :created_at)"
            ),
            {"slug": old_slug, "video_id": video_id, "created_at": migrated_at},
        )
        bind.execute(
            sa.text(
                "UPDATE published_video_seo SET slug = :new_slug "
                "WHERE video_id = :video_id AND slug = :old_slug"
            ),
            {
                "new_slug": new_slug,
                "video_id": video_id,
                "old_slug": old_slug,
            },
        )


def downgrade() -> None:
    # Forward-only: canonical URLs and their legacy redirects must not be recycled.
    pass
