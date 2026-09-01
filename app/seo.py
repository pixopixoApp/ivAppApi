from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.html_content import CONTENT_TYPE_HTML, CONTENT_TYPE_RUNTIME
from app.models import PublishedVideo, PublishedVideoSeo, User

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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_placeholder_text(value: str | None) -> bool:
    text = re.sub(r"\s+", " ", (value or "").strip()).lower()
    if not text or text in _PLACEHOLDERS:
        return True
    return bool(re.fullmatch(r"(?:video|experience|story)[-_ ]?\d*", text))


def slugify(value: str, *, video_id: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:120]
    if not stem:
        stem = "interactive-experience"
    suffix = hashlib.sha256(video_id.encode("utf-8")).hexdigest()[:16]
    return f"{stem}-{suffix}"


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
        slug=slugify(row.title or "", video_id=row.id),
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
    canonical = f"{site_url.rstrip('/')}/experiences/{seo.slug}"
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
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": max(
            value for value in (row.updated_at, seo.updated_at) if value is not None
        ).isoformat(),
    }
