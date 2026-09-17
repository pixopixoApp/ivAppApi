from __future__ import annotations

import hashlib
import json
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.html_content import CONTENT_TYPE_HTML, CONTENT_TYPE_RUNTIME
from app.models import (
    PublishedVideo,
    PublishedVideoSeo,
    PublishedVideoSeoSlugAlias,
    User,
)

SEO_STATUSES = frozenset({"pending", "generating", "ready", "failed", "stale"})
_PLACEHOLDERS = frozenset(
    {
        "untitled",
        "untitled story",
        "untitled experience",
        "interactive experience",
        "new experience",
        "video",
    }
)
_SLUG_STOP_WORDS = frozenset(
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
    }
)
_SLUG_PROMOTIONAL_WORDS = frozenset(
    {
        "ultimate",
        "best",
        "amazing",
        "awesome",
        "easy",
        "simple",
    }
)
_SLUG_EDITORIAL_FILLER = frozenset(
    {
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
_SLUG_EXCLUDED_WORDS = (
    _SLUG_STOP_WORDS | _SLUG_PROMOTIONAL_WORDS | _SLUG_EDITORIAL_FILLER
)
_SLUG_MAX_WORDS = 5
_SLUG_RANDOM_ATTEMPTS = 32


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc_isoformat(value: datetime | None) -> str:
    """Render a datetime as a pure calendar date (``YYYY-MM-DD``).

    MySQL ``DATETIME`` columns are read back as naive datetimes even though we
    always write UTC.  Google sitemaps are most robust with plain dates (for
    example ``<lastmod>`` and ``<video:publication_date>`` accept ``YYYY-MM-DD``),
    so serialise only the calendar date part.
    """
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.date().isoformat()


def is_placeholder_text(value: str | None) -> bool:
    text = re.sub(r"\s+", " ", (value or "").strip()).lower()
    if not text or text in _PLACEHOLDERS:
        return True
    return bool(re.fullmatch(r"(?:video|experience|story)[-_ ]?\d*", text))


def seo_slug_stem(value: str) -> str:
    """Extract a short, lower-case ASCII keyword stem from an English title."""
    ascii_text = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode()
        .lower()
    )
    # Apostrophes and periods inside words add no useful URL boundary. Other
    # punctuation is naturally treated as a separator by the token matcher.
    ascii_text = ascii_text.replace("'", "").replace(".", "")
    words = []
    for word in re.findall(r"[a-z0-9]+", ascii_text):
        if word in _SLUG_EXCLUDED_WORDS:
            continue
        if word.isdigit() or re.fullmatch(r"(?:top|best)\d+|\d+(?:top|best)|v\d+", word):
            continue
        words.append(word[:40])
        if len(words) == _SLUG_MAX_WORDS:
            break
    stem = "-".join(words).strip("-")[:120].rstrip("-")
    return stem or "interactive-video"


def slugify(value: str, *, suffix: int | None = None) -> str:
    """Build a readable slug with one persisted five-digit random suffix."""
    number = suffix if suffix is not None else secrets.randbelow(90_000) + 10_000
    if not 10_000 <= number <= 99_999:
        raise ValueError("slug suffix must be a five-digit integer")
    return f"{seo_slug_stem(value)}-{number:05d}"


def unique_slug(db: Session, value: str, *, video_id: str) -> str:
    """Generate an unused slug without changing another video's permalink."""
    for _attempt in range(_SLUG_RANDOM_ATTEMPTS):
        candidate = slugify(value)
        current_collision = (
            db.query(PublishedVideoSeo.video_id)
            .filter(
                PublishedVideoSeo.slug == candidate,
                PublishedVideoSeo.video_id != video_id,
            )
            .first()
        )
        alias_collision = db.get(PublishedVideoSeoSlugAlias, candidate)
        if current_collision is None and alias_collision is None:
            return candidate
    raise RuntimeError("could not allocate a unique five-digit SEO slug")


def source_document(row: PublishedVideo) -> dict[str, Any]:
    return {
        "video_id": row.id,
        "version": row.version,
        "content_type": row.content_type,
        "title": row.title or "",
        "description": row.description or "",
        "timeline": row.timeline,
        "runtime_spec": row.runtime_spec,
        "required_capabilities": row.required_capabilities or [],
        "review_status": row.review_status,
        "distribution_enabled": bool(row.distribution_enabled),
        "cdn_ready": bool(row.cdn_ready),
        "cover_media_object_id": row.cover_media_object_id,
    }


def source_hash(row: PublishedVideo) -> str:
    raw = json.dumps(
        source_document(row),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def ensure_seo_row(db: Session, row: PublishedVideo) -> PublishedVideoSeo:
    seo = db.get(PublishedVideoSeo, row.id)
    if seo is not None:
        return seo
    now = utcnow()
    seo = PublishedVideoSeo(
        video_id=row.id,
        slug=unique_slug(db, row.title or "", video_id=row.id),
        status="pending",
        source_hash=source_hash(row),
        thumbnail_url=(
            f"/posters/{row.id}.jpg"
            if row.content_type == CONTENT_TYPE_RUNTIME
            else "/assets/pixo-logo.png"
        ),
        created_at=now,
        updated_at=now,
    )
    db.add(seo)
    return seo


def mark_seo_stale(db: Session, row: PublishedVideo) -> PublishedVideoSeo:
    seo = ensure_seo_row(db, row)
    current_hash = source_hash(row)
    if seo.source_hash != current_hash:
        seo.source_hash = current_hash
        if seo.status == "ready":
            seo.status = "stale"
        elif seo.status != "generating":
            seo.status = "pending"
        seo.updated_at = utcnow()
    return seo


def visible_experience_query(db: Session):
    return (
        db.query(PublishedVideo, PublishedVideoSeo, User)
        .join(PublishedVideoSeo, PublishedVideoSeo.video_id == PublishedVideo.id)
        .outerjoin(User, PublishedVideo.user_id == User.user_id)
        .filter(
            PublishedVideo.is_deleted == 0,
            PublishedVideo.deleted_at.is_(None),
            PublishedVideo.review_status == "approved",
            PublishedVideo.distribution_enabled.is_(True),
            PublishedVideo.cdn_ready.is_(True),
            PublishedVideoSeo.status == "ready",
            PublishedVideoSeo.page_title != "",
            PublishedVideoSeo.page_description != "",
            PublishedVideoSeo.meta_title != "",
            PublishedVideoSeo.meta_description != "",
            or_(
                PublishedVideo.user_id.is_(None),
                PublishedVideo.user_id == "",
                User.enabled.is_(True),
            ),
            or_(
                and_(
                    PublishedVideo.content_type == CONTENT_TYPE_RUNTIME,
                    PublishedVideo.runtime_spec.is_not(None),
                ),
                and_(
                    PublishedVideo.content_type == CONTENT_TYPE_HTML,
                    PublishedVideo.html_url.is_not(None),
                ),
            ),
        )
    )


def first_runtime_media(row: PublishedVideo) -> str:
    spec = row.runtime_spec if isinstance(row.runtime_spec, dict) else {}
    clips = spec.get("video") if isinstance(spec, dict) else None
    if isinstance(clips, list) and clips and isinstance(clips[0], dict):
        return str(clips[0].get("video") or "")
    return str(row.video_url or "")


def seo_public_item(
    row: PublishedVideo,
    seo: PublishedVideoSeo,
    author: User | None,
    *,
    site_url: str,
) -> dict[str, Any]:
    canonical = f"{site_url.rstrip('/')}/videos/{seo.slug}"
    thumbnail_path = seo.thumbnail_url or (
        f"/posters/{row.id}.jpg"
        if row.content_type == CONTENT_TYPE_RUNTIME
        else "/assets/pixo-logo.png"
    )
    thumbnail = (
        thumbnail_path
        if thumbnail_path.startswith(("https://", "http://"))
        else f"{site_url.rstrip('/')}/{thumbnail_path.lstrip('/')}"
    )
    return {
        "id": row.id,
        "slug": seo.slug,
        "canonical_url": canonical,
        "title": seo.page_title or row.title or seo.meta_title,
        "description": seo.page_description or row.description or seo.meta_description,
        "meta_title": seo.meta_title,
        "meta_description": seo.meta_description,
        "author": {
            "id": row.user_id or "",
            "name": ((author.nickname if author else "") or "Pixopixo Creator"),
            "avatar_url": ((author.avatar_url if author else "") or ""),
        },
        "thumbnail_url": thumbnail,
        "content_type": row.content_type,
        "playable_on_web": row.content_type == CONTENT_TYPE_RUNTIME,
        "interaction_types": list(seo.interaction_types or []),
        "interaction_summary": seo.interaction_summary,
        "tags": list(seo.tags or []),
        "duration_seconds": seo.duration_seconds,
        "width": seo.width,
        "height": seo.height,
        "content_url": first_runtime_media(row) if row.content_type == CONTENT_TYPE_RUNTIME else "",
        # The public detail page is now the stable watch/player URL.  Keep the
        # response field for compatibility without publishing a duplicate
        # query-parameter URL.
        "embed_url": canonical,
        "created_at": _aware_utc_isoformat(row.created_at),
        "updated_at": _aware_utc_isoformat(
            max(
                value for value in (row.updated_at, seo.updated_at) if value is not None
            )
        ),
    }
