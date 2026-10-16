from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import String, cast, or_
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.content_localization import localized_content
from app.db import get_db
from app.deps import require_publish_key
from app.locale import request_locale
from app.models import (
    MediaObject,
    PublishedVideo,
    PublishedVideoSeo,
    PublishedVideoSeoSlugAlias,
)
from app.oss_storage import OssStorageError
from app.public_origin import PublicOriginError, canonical_public_url_for_key
from app.schemas_seo import (
    SeoAdminEdit,
    SeoBackfillRequest,
    SeoMetadataWrite,
    SeoStatusUpdate,
)
from app.seo import (
    SEO_STATUSES,
    ensure_seo_row,
    mark_seo_stale,
    seo_public_item,
    source_hash,
    unique_slug,
    utcnow,
    visible_experience_query,
)

public_router = APIRouter(prefix="/api/v1/public/seo", tags=["public-seo"])
internal_router = APIRouter(
    prefix="/internal/v1/seo",
    tags=["admin"],
    dependencies=[Depends(require_publish_key)],
)

InteractionGroup = Literal["touch", "camera", "motion", "sound"]
_INTERACTION_TYPES_BY_GROUP: dict[InteractionGroup, frozenset[str]] = {
    "touch": frozenset({
        "tap", "double_tap", "rapid_tap", "multi_tap", "hold", "hold_charge",
        "swipe_left", "swipe_right", "swipe_up", "swipe_down", "drag_left",
        "drag_right", "drag_up", "drag_down", "scrub_left", "scrub_right",
        "scrub_up", "scrub_down", "continuous_swipe", "continuous_tap",
        "continuous_hold", "pinch", "draw_circle", "erase",
    }),
    "camera": frozenset({"camera_motion", "camera_continuous"}),
    "motion": frozenset({
        "hold_still", "tilt_left", "tilt_right", "tilt_forward", "tilt_backward",
        "shake", "rotate",
    }),
    "sound": frozenset({
        "mic_level", "mic_level_continuous", "mic_blow", "mic_blow_continuous",
        "mic_clap", "mic_quiet",
    }),
}

_WEB_LOCALE_PREFIXES = {
    "zh-Hans": "/zh-hans",
    "zh-Hant": "/zh-hant",
    "ja": "/ja",
    "ko": "/ko",
    "es": "/es",
    "pt-BR": "/pt-br",
    "fr": "/fr",
    "de": "/de",
}


def _site_url(settings: Settings) -> str:
    return settings.seo_public_base_url.strip().rstrip("/") or "https://pixopixo.com"


def _cache(response: Response, seconds: int = 300) -> None:
    response.headers["Cache-Control"] = f"public, max-age=0, s-maxage={seconds}, stale-while-revalidate=600"


def _localized_video_path(slug: str, locale: str) -> str:
    return f"{_WEB_LOCALE_PREFIXES.get(locale, '')}/videos/{slug}"


def _runtime_interaction_summary(runtime_spec: Any) -> str:
    if not isinstance(runtime_spec, dict):
        return ""
    descriptions: list[str] = []
    for clip in runtime_spec.get("video") or []:
        if not isinstance(clip, dict):
            continue
        for interaction in clip.get("interactions") or []:
            if not isinstance(interaction, dict):
                continue
            description = str(interaction.get("description") or "").strip()
            if description and description not in descriptions:
                descriptions.append(description)
    # The detailed text is already translated and persisted by stable
    # video/interaction id. Deriving this SEO view avoids a second LLM field
    # that could drift away from the instructions used by the Runtime.
    return " · ".join(descriptions[:3])[:500].rstrip()


def _localized_public_item(
    db: Session,
    row: PublishedVideo,
    seo: PublishedVideoSeo,
    author: Any,
    *,
    settings: Settings,
    locale: str,
) -> dict[str, Any]:
    site_url = _site_url(settings)
    item = seo_public_item(row, seo, author, site_url=site_url)
    title, description, localized_runtime, localization = localized_content(
        db,
        row,
        locale,
        enabled=settings.content_localization_enabled,
    )
    use_legacy_english_seo = (
        locale == "en"
        and row.content_type == "runtime"
        and localization["status"] != "ready"
    )
    if not use_legacy_english_seo:
        item["title"] = title
        item["description"] = description
    if not use_legacy_english_seo:
        item["meta_title"] = f"{title} | Pixopixo"[:70]
        item["meta_description"] = description[:180]
    item["interaction_summary_locale"] = "en"
    summary_matches_locale = localization["status"] == "ready" or (
        localization["status"] == "source"
        and localization.get("source_locale") == locale
    )
    if locale != "en" and summary_matches_locale:
        localized_summary = _runtime_interaction_summary(localized_runtime)
        if localized_summary:
            item["interaction_summary"] = localized_summary
            item["interaction_summary_locale"] = locale
    # English keeps the historic permalink; every translated document gets a
    # stable visible locale path that matches Web canonical/hreflang metadata.
    path = _localized_video_path(seo.slug, locale)
    item["canonical_url"] = f"{site_url}{path}"
    item["embed_url"] = item["canonical_url"]
    item["localization"] = localization
    return item


@public_router.get("/experiences")
def list_public_experiences(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=24, ge=1, le=48),
    interaction_group: Annotated[InteractionGroup | None, Query()] = None,
) -> dict[str, Any]:
    query = visible_experience_query(db)
    if interaction_group is not None:
        serialized_types = cast(PublishedVideoSeo.interaction_types, String)
        query = query.filter(or_(*(
            serialized_types.like(f'%"{interaction_type}"%')
            for interaction_type in _INTERACTION_TYPES_BY_GROUP[interaction_group]
        )))
    total = query.count()
    rows = (
        query.order_by(
            PublishedVideo.feed_weight.desc(),
            PublishedVideo.created_at.desc(),
            PublishedVideo.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    locale = request_locale(request)
    _cache(response)
    return {
        "items": [
            _localized_public_item(
                db, row, seo, author, settings=settings, locale=locale
            )
            for row, seo, author in rows
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "interaction_group": interaction_group,
    }


@public_router.get("/resolve/{video_id}")
def resolve_public_experience(
    video_id: str,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    result = visible_experience_query(db).filter(PublishedVideo.id == video_id).one_or_none()
    if result is None:
        raise HTTPException(status_code=404, detail="experience not found")
    row, seo, _author = result
    locale = request_locale(request)
    _cache(response)
    return {
        "id": row.id,
        "slug": seo.slug,
        "canonical_url": f"{_site_url(settings)}{_localized_video_path(seo.slug, locale)}",
    }


@public_router.get("/experiences/{slug}")
def get_public_experience(
    slug: str,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    result = visible_experience_query(db).filter(PublishedVideoSeo.slug == slug).one_or_none()
    if result is None:
        alias = db.get(PublishedVideoSeoSlugAlias, slug)
        if alias is not None:
            result = (
                visible_experience_query(db)
                .filter(PublishedVideo.id == alias.video_id)
                .one_or_none()
            )
    if result is None:
        raise HTTPException(status_code=404, detail="experience not found")
    row, seo, author = result
    locale = request_locale(request)
    item = _localized_public_item(
        db, row, seo, author, settings=settings, locale=locale
    )
    related_rows = (
        visible_experience_query(db)
        .filter(PublishedVideo.id != row.id)
        .order_by(PublishedVideo.created_at.desc(), PublishedVideo.id.desc())
        .limit(4)
        .all()
    )
    item["related"] = [
        _localized_public_item(
            db,
            other,
            other_seo,
            other_author,
            settings=settings,
            locale=locale,
        )
        for other, other_seo, other_author in related_rows
    ]
    _cache(response)
    return item


@internal_router.post("/backfill")
def create_seo_backfill(
    payload: SeoBackfillRequest,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    rows = (
        db.query(PublishedVideo)
        .filter(
            PublishedVideo.is_deleted == 0,
            PublishedVideo.deleted_at.is_(None),
            PublishedVideo.review_status == "approved",
            PublishedVideo.distribution_enabled.is_(True),
            PublishedVideo.cdn_ready.is_(True),
        )
        .order_by(PublishedVideo.created_at.asc(), PublishedVideo.id.asc())
        .limit(payload.limit)
        .all()
    )
    queued: list[str] = []
    unchanged = 0
    for row in rows:
        seo = db.get(PublishedVideoSeo, row.id)
        if seo is None:
            seo = ensure_seo_row(db, row)
            queued.append(row.id)
            continue
        should_queue = payload.force or seo.status in {"pending", "stale"}
        if payload.include_failed and seo.status == "failed":
            should_queue = True
        if should_queue:
            seo.status = "pending"
            seo.source_hash = source_hash(row)
            seo.last_error = ""
            seo.updated_at = utcnow()
            queued.append(row.id)
        else:
            unchanged += 1
    db.commit()
    queued_hashes = (
        {
            seo.video_id: seo.source_hash
            for seo in db.query(PublishedVideoSeo)
            .filter(PublishedVideoSeo.video_id.in_(queued))
            .all()
        }
        if queued
        else {}
    )
    return {
        "queued": queued,
        "jobs": [
            {"video_id": video_id, "source_hash": queued_hashes.get(video_id, "")}
            for video_id in queued
        ],
        "queued_count": len(queued),
        "unchanged": unchanged,
    }


@internal_router.get("/jobs")
def list_seo_jobs(
    db: Annotated[Session, Depends(get_db)],
    status: str = Query(default="pending", pattern="^(pending|generating|failed|stale|ready|all)$"),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    query = db.query(PublishedVideoSeo, PublishedVideo).join(
        PublishedVideo, PublishedVideo.id == PublishedVideoSeo.video_id
    )
    if status != "all":
        query = query.filter(PublishedVideoSeo.status == status)
    rows = query.order_by(PublishedVideoSeo.updated_at.asc()).limit(limit).all()
    return {
        "items": [
            {
                "video_id": seo.video_id,
                "status": seo.status,
                "attempts": seo.attempts,
                "last_error": seo.last_error,
                "source_hash": seo.source_hash,
                "title": row.title or "",
                "description": row.description or "",
                "content_type": row.content_type,
                "updated_at": seo.updated_at.isoformat() if seo.updated_at else "",
            }
            for seo, row in rows
        ]
    }


@internal_router.patch("/experiences/{video_id}/status")
def update_seo_status(
    video_id: str,
    payload: SeoStatusUpdate,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = db.get(PublishedVideo, video_id)
    if row is None:
        raise HTTPException(status_code=404, detail="video not found")
    seo = ensure_seo_row(db, row)
    if payload.status not in SEO_STATUSES or payload.status == "ready":
        raise HTTPException(status_code=400, detail="invalid generation status")
    seo.status = payload.status
    if payload.status in {"pending", "stale"}:
        seo.source_hash = source_hash(row)
    seo.last_error = payload.error if payload.status == "failed" else ""
    if payload.status == "generating":
        seo.attempts = int(seo.attempts or 0) + 1
    seo.updated_at = utcnow()
    db.commit()
    return {
        "video_id": video_id,
        "status": seo.status,
        "attempts": seo.attempts,
        "source_hash": seo.source_hash,
    }


@internal_router.put("/experiences/{video_id}")
def put_seo_metadata(
    video_id: str,
    payload: SeoMetadataWrite,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    row = db.get(PublishedVideo, video_id)
    if row is None or row.is_deleted != 0 or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="video not found")
    current_hash = source_hash(row)
    if payload.source_hash != current_hash:
        raise HTTPException(status_code=409, detail="published source changed during generation")
    seo = ensure_seo_row(db, row)
    # SEO generation writes only the derived SEO document. Creator-authored
    # canonical fields are immutable and are never replaced with generated copy.
    title_written = False
    description_written = False
    final_hash = source_hash(row)
    if seo.generated_at is None and seo.status != "ready":
        seo.slug = unique_slug(db, payload.title, video_id=row.id)
    seo.page_title = payload.title.strip()
    seo.page_description = payload.description.strip()
    seo.meta_title = payload.meta_title.strip()
    seo.meta_description = payload.meta_description.strip()
    seo.tags = payload.tags
    seo.interaction_types = payload.interaction_types
    seo.interaction_summary = payload.interaction_summary.strip()
    seo.duration_seconds = payload.duration_seconds
    seo.width = payload.width
    seo.height = payload.height
    cover = db.get(MediaObject, row.cover_media_object_id) if row.cover_media_object_id else None
    if cover is not None and cover.visibility == "public" and cover.state == "ready":
        try:
            seo.thumbnail_url = canonical_public_url_for_key(settings, cover.object_key)
        except (OssStorageError, PublicOriginError) as exc:
            raise HTTPException(status_code=409, detail=f"invalid cover media binding: {exc}")
    elif payload.thumbnail_url.strip():
        seo.thumbnail_url = payload.thumbnail_url.strip()
    seo.source_hash = final_hash
    seo.model = payload.model.strip()
    seo.prompt_version = payload.prompt_version.strip()
    seo.status = "ready"
    seo.last_error = ""
    seo.ai_title_written = seo.ai_title_written or title_written
    seo.ai_description_written = seo.ai_description_written or description_written
    seo.generated_at = datetime.now(timezone.utc)
    seo.updated_at = seo.generated_at
    row.updated_at = seo.generated_at
    db.commit()
    return {
        "video_id": row.id,
        "slug": seo.slug,
        "status": seo.status,
        "title": row.title,
        "description": row.description,
        "ai_title_written": seo.ai_title_written,
        "ai_description_written": seo.ai_description_written,
    }


@internal_router.patch("/experiences/{video_id}")
def edit_seo_metadata(
    video_id: str,
    payload: SeoAdminEdit,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = db.get(PublishedVideo, video_id)
    if row is None:
        raise HTTPException(status_code=404, detail="video not found")
    seo = ensure_seo_row(db, row)
    changed = payload.model_fields_set
    if not changed:
        raise HTTPException(status_code=400, detail="at least one field is required")
    for name in (
        "page_title",
        "page_description",
        "meta_title",
        "meta_description",
        "tags",
        "interaction_summary",
        "title_locked",
        "description_locked",
    ):
        if name in changed:
            setattr(seo, name, getattr(payload, name))
    if (
        seo.page_title
        and seo.page_description
        and seo.meta_title
        and seo.meta_description
        and seo.interaction_summary
    ):
        seo.status = "ready"
    seo.updated_at = utcnow()
    db.commit()
    return {"video_id": video_id, "slug": seo.slug, "status": seo.status}


def mark_content_seo_stale(db: Session, row: PublishedVideo) -> None:
    mark_seo_stale(db, row)
