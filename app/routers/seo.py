from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.deps import require_publish_key
from app.models import PublishedVideo, PublishedVideoSeo
from app.schemas_seo import (
    SeoAdminEdit,
    SeoBackfillRequest,
    SeoMetadataWrite,
    SeoStatusUpdate,
)
from app.seo import (
    SEO_STATUSES,
    ensure_seo_row,
    is_placeholder_text,
    mark_seo_stale,
    seo_public_item,
    slugify,
    source_hash,
    utcnow,
    visible_experience_query,
)

public_router = APIRouter(prefix="/api/v1/public/seo", tags=["public-seo"])
internal_router = APIRouter(
    prefix="/internal/v1/seo",
    tags=["admin"],
    dependencies=[Depends(require_publish_key)],
)


def _site_url(settings: Settings) -> str:
    return settings.seo_public_base_url.strip().rstrip("/") or "https://pixopixo.com"


def _cache(response: Response, seconds: int = 300) -> None:
    response.headers["Cache-Control"] = f"public, max-age=0, s-maxage={seconds}, stale-while-revalidate=600"


@public_router.get("/experiences")
def list_public_experiences(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    page: int = Query(default=1, ge=1, le=100_000),
    page_size: int = Query(default=24, ge=1, le=48),
) -> dict[str, Any]:
    query = visible_experience_query(db)
    total = query.count()
    rows = (
        query.order_by(PublishedVideo.created_at.desc(), PublishedVideo.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    site_url = _site_url(settings)
    _cache(response)
    return {
        "items": [seo_public_item(row, seo, author, site_url=site_url) for row, seo, author in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@public_router.get("/resolve/{video_id}")
def resolve_public_experience(
    video_id: str,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    result = visible_experience_query(db).filter(PublishedVideo.id == video_id).one_or_none()
    if result is None:
        raise HTTPException(status_code=404, detail="experience not found")
    row, seo, _author = result
    _cache(response)
    return {
        "id": row.id,
        "slug": seo.slug,
        "canonical_url": f"{_site_url(settings)}/experiences/{seo.slug}",
    }


@public_router.get("/experiences/{slug}")
def get_public_experience(
    slug: str,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    result = visible_experience_query(db).filter(PublishedVideoSeo.slug == slug).one_or_none()
    if result is None:
        raise HTTPException(status_code=404, detail="experience not found")
    row, seo, author = result
    site_url = _site_url(settings)
    item = seo_public_item(row, seo, author, site_url=site_url)
    related_rows = (
        visible_experience_query(db)
        .filter(PublishedVideo.id != row.id)
        .order_by(PublishedVideo.created_at.desc(), PublishedVideo.id.desc())
        .limit(4)
        .all()
    )
    item["related"] = [
        seo_public_item(other, other_seo, other_author, site_url=site_url)
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
) -> dict[str, Any]:
    row = db.get(PublishedVideo, video_id)
    if row is None or row.is_deleted != 0 or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="video not found")
    current_hash = source_hash(row)
    if payload.source_hash != current_hash:
        raise HTTPException(status_code=409, detail="published source changed during generation")
    seo = ensure_seo_row(db, row)
    title_written = False
    description_written = False
    if not seo.title_locked and is_placeholder_text(row.title):
        row.title = payload.title.strip()
        title_written = True
    if not seo.description_locked and is_placeholder_text(row.description):
        row.description = payload.description.strip()
        description_written = True
    # Source content may have been filled above. Bind readiness to the final row.
    final_hash = source_hash(row)
    if seo.generated_at is None and seo.status != "ready":
        seo.slug = slugify(payload.title, video_id=row.id)
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
    if payload.thumbnail_url.strip():
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
