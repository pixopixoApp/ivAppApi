from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth_user import AppUser
from app.avatar_storage import (
    AvatarStorageError,
    avatar_media_type,
    resolve_background_path,
    store_channel_background,
    store_user_avatar,
)
from app.config import Settings, get_settings
from app.creator_channels import (
    ChannelValidationError,
    audit,
    change_handle,
    channel_relations,
    check_handle_availability,
    ensure_user_handle,
    handle_change_available_at,
    iso,
    new_topic_id,
    normalize_channel_bio,
    normalize_channel_name,
    normalize_email,
    normalize_language,
    now_utc,
    replace_links,
    replace_pins,
    replace_topics,
    search_creators,
    snapshot,
)
from app.db import get_db
from app.deps import require_publish_key
from app.media_service import media_mode_is_oss
from app.models import (
    CreatorProfileAudit,
    CreatorTopic,
    CreatorTopicAssignment,
    User,
)
from app.public_origin import canonicalize_public_url
from app.schemas_social import CreatorTopicOut
from app.schemas_web import (
    CreatorChannelPrivateOut,
    CreatorChannelUpdateRequest,
    HandleAvailabilityOut,
)
from app.web_session import require_app_or_web_user

router = APIRouter(prefix="/api/v1", tags=["creator-channels"])
public_router = APIRouter(prefix="/api/v1/public", tags=["creator-channels-public"])
operations_router = APIRouter(
    prefix="/internal/v1/creator-channels",
    tags=["creator-channels-operations"],
    dependencies=[Depends(require_publish_key)],
)
media_router = APIRouter(tags=["creator-channel-media"])


class TopicMutation(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    enabled: bool | None = None
    actor_id: str = Field(min_length=1, max_length=128)
    actor_role: str = Field(default="operator", max_length=32)
    source: str = Field(default="ivadmin", max_length=32)


class TopicArchiveMutation(BaseModel):
    strategy: Literal["replace", "remove"] | None = None
    replacement_topic_id: str | None = None
    actor_id: str = Field(min_length=1, max_length=128)
    actor_role: str = Field(default="operator", max_length=32)
    source: str = Field(default="ivadmin", max_length=32)


class CreatorTopicsMutation(BaseModel):
    topic_ids: list[str] = Field(max_length=3)
    actor_id: str = Field(min_length=1, max_length=128)
    actor_role: str = Field(default="operator", max_length=32)
    source: str = Field(default="ivadmin", max_length=32)


def _error(exc: ChannelValidationError, status_code: int = 422) -> HTTPException:
    if exc.code in {"HANDLE_TAKEN", "HANDLE_COOLDOWN"}:
        status_code = 409
    return HTTPException(status_code=status_code, detail=exc.detail)


def _user(db: Session, app_user: AppUser) -> User:
    row = db.get(User, app_user.user_id)
    if row is None or not row.enabled:
        raise HTTPException(status_code=404, detail="channel not found")
    ensure_user_handle(db, row)
    return row


def _private_out(
    db: Session, settings: Settings, row: User, *, commit_cleanup: bool = False
) -> CreatorChannelPrivateOut:
    handle = ensure_user_handle(db, row)
    links, topics, pinned = channel_relations(db, row.user_id)
    if commit_cleanup and (db.new or db.dirty or db.deleted):
        db.commit()
        db.refresh(row)
    available_at = handle_change_available_at(row)
    return CreatorChannelPrivateOut(
        user_id=row.user_id,
        nickname=row.nickname or "",
        handle=handle,
        share_url=f"https://pixopixo.com/@{handle}" if handle else "",
        avatar_url=canonicalize_public_url(settings, row.avatar_url) or "",
        bio=row.bio or "",
        background_url=canonicalize_public_url(settings, row.background_url) or "",
        background_mobile_url=canonicalize_public_url(settings, row.background_mobile_url) or "",
        background_desktop_url=canonicalize_public_url(settings, row.background_desktop_url) or "",
        background_focus_x=max(0.0, min(1.0, row.background_focus_x)),
        background_focus_y=max(0.0, min(1.0, row.background_focus_y)),
        content_language=row.content_language or "",
        collaboration_email=row.collaboration_email or "",
        collaboration_email_public=bool(row.collaboration_email_public),
        external_links=links,
        topics=topics,
        pinned_video_ids=pinned,
        handle_changed_at=iso(row.handle_changed_at),
        handle_change_available_at=iso(available_at),
        profile_updated_at=iso(row.profile_updated_at),
    )


@router.get("/channel", response_model=CreatorChannelPrivateOut)
@router.get("/web/channel", response_model=CreatorChannelPrivateOut)
def get_channel(
    app_user: Annotated[AppUser, Depends(require_app_or_web_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorChannelPrivateOut:
    return _private_out(db, settings, _user(db, app_user), commit_cleanup=True)


@router.get("/channel/handle-availability", response_model=HandleAvailabilityOut)
@router.get("/web/channel/handle-availability", response_model=HandleAvailabilityOut)
def handle_availability(
    handle: str,
    app_user: Annotated[AppUser, Depends(require_app_or_web_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    return check_handle_availability(db, handle, user=_user(db, app_user))


def _apply_update(
    payload: CreatorChannelUpdateRequest,
    *,
    app_user: AppUser,
    db: Session,
    settings: Settings,
) -> CreatorChannelPrivateOut:
    row = _user(db, app_user)
    before = snapshot(db, row)
    fields = payload.model_fields_set
    try:
        if "nickname" in fields and payload.nickname is not None:
            row.nickname = normalize_channel_name(payload.nickname)
        if "bio" in fields and payload.bio is not None:
            row.bio = normalize_channel_bio(payload.bio)
        if "handle" in fields and payload.handle is not None:
            change_handle(db, row, payload.handle)
        if "background_focus_x" in fields and payload.background_focus_x is not None:
            row.background_focus_x = payload.background_focus_x
        if "background_focus_y" in fields and payload.background_focus_y is not None:
            row.background_focus_y = payload.background_focus_y
        if "content_language" in fields:
            row.content_language = normalize_language(payload.content_language)
        if "collaboration_email" in fields:
            row.collaboration_email = normalize_email(payload.collaboration_email)
        if "collaboration_email_public" in fields and payload.collaboration_email_public is not None:
            row.collaboration_email_public = payload.collaboration_email_public
        if row.collaboration_email_public and not row.collaboration_email:
            raise ChannelValidationError(
                "EMAIL_REQUIRED", "Add a collaboration email before making it public."
            )
        if "external_links" in fields and payload.external_links is not None:
            replace_links(db, row.user_id, [item.model_dump() for item in payload.external_links])
        if "topic_ids" in fields and payload.topic_ids is not None:
            replace_topics(db, row.user_id, payload.topic_ids)
        if "pinned_video_ids" in fields and payload.pinned_video_ids is not None:
            replace_pins(db, row.user_id, payload.pinned_video_ids)
        row.profile_updated_at = now_utc()
        db.add(row)
        db.flush()
        after = snapshot(db, row)
        audit(
            db,
            user_id=row.user_id,
            action="channel.update",
            actor_id=row.user_id,
            actor_role="creator",
            source=app_user.channel,
            before=before,
            after=after,
        )
        db.commit()
        db.refresh(row)
    except ChannelValidationError as exc:
        db.rollback()
        raise _error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "HANDLE_TAKEN", "message": "This handle is unavailable."},
        ) from exc
    return _private_out(db, settings, row)


@router.patch("/channel", response_model=CreatorChannelPrivateOut)
@router.patch("/web/channel", response_model=CreatorChannelPrivateOut)
def update_channel(
    payload: CreatorChannelUpdateRequest,
    request: Request,
    app_user: Annotated[AppUser, Depends(require_app_or_web_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorChannelPrivateOut:
    del request
    return _apply_update(payload, app_user=app_user, db=db, settings=settings)


@router.post("/channel/avatar", response_model=CreatorChannelPrivateOut)
@router.post("/web/channel/avatar", response_model=CreatorChannelPrivateOut)
async def upload_channel_avatar(
    request: Request,
    app_user: Annotated[AppUser, Depends(require_app_or_web_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    file: Annotated[UploadFile, File()],
) -> CreatorChannelPrivateOut:
    del request
    row = _user(db, app_user)
    before = snapshot(db, row)
    try:
        url, object_id = store_user_avatar(
            db, settings, user_id=row.user_id, raw=await file.read(),
            filename=file.filename, content_type=file.content_type,
        )
    except AvatarStorageError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail={"code": "IMAGE_INVALID", "message": str(exc)}) from exc
    row.avatar_url = url
    row.avatar_media_object_id = object_id
    row.profile_updated_at = now_utc()
    db.add(row)
    db.flush()
    audit(
        db, user_id=row.user_id, action="channel.avatar.update", actor_id=row.user_id,
        actor_role="creator", source=app_user.channel, before=before, after=snapshot(db, row),
    )
    db.commit()
    db.refresh(row)
    return _private_out(db, settings, row)


@router.post("/channel/background", response_model=CreatorChannelPrivateOut)
@router.post("/web/channel/background", response_model=CreatorChannelPrivateOut)
async def upload_channel_background(
    request: Request,
    app_user: Annotated[AppUser, Depends(require_app_or_web_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    file: Annotated[UploadFile, File()],
    focus_x: float = Query(default=0.5, ge=0, le=1),
    focus_y: float = Query(default=0.5, ge=0, le=1),
) -> CreatorChannelPrivateOut:
    del request
    row = _user(db, app_user)
    before = snapshot(db, row)
    try:
        desktop, mobile = store_channel_background(
            db, settings, user_id=row.user_id, raw=await file.read(), filename=file.filename,
            content_type=file.content_type, focus_x=focus_x, focus_y=focus_y,
        )
    except AvatarStorageError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail={"code": "IMAGE_INVALID", "message": str(exc)}) from exc
    row.background_url = desktop
    row.background_desktop_url = desktop
    row.background_mobile_url = mobile
    row.background_focus_x = focus_x
    row.background_focus_y = focus_y
    row.profile_updated_at = now_utc()
    db.add(row)
    db.flush()
    audit(
        db, user_id=row.user_id, action="channel.background.update", actor_id=row.user_id,
        actor_role="creator", source=app_user.channel, before=before, after=snapshot(db, row),
    )
    db.commit()
    db.refresh(row)
    return _private_out(db, settings, row)


@router.delete("/channel/background", response_model=CreatorChannelPrivateOut)
@router.delete("/web/channel/background", response_model=CreatorChannelPrivateOut)
def delete_channel_background(
    request: Request,
    app_user: Annotated[AppUser, Depends(require_app_or_web_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorChannelPrivateOut:
    del request
    row = _user(db, app_user)
    before = snapshot(db, row)
    row.background_url = ""
    row.background_desktop_url = ""
    row.background_mobile_url = ""
    row.background_focus_x = 0.5
    row.background_focus_y = 0.5
    row.profile_updated_at = now_utc()
    db.add(row)
    audit(
        db, user_id=row.user_id, action="channel.background.delete", actor_id=row.user_id,
        actor_role="creator", source=app_user.channel, before=before, after=snapshot(db, row),
    )
    db.commit()
    db.refresh(row)
    return _private_out(db, settings, row)


@public_router.get("/creator-topics", response_model=list[CreatorTopicOut])
def public_topics(db: Annotated[Session, Depends(get_db)]) -> list[CreatorTopicOut]:
    rows = db.query(CreatorTopic).filter(
        CreatorTopic.enabled.is_(True), CreatorTopic.archived_at.is_(None)
    ).order_by(CreatorTopic.name.asc()).all()
    return [CreatorTopicOut(id=row.id, name=row.name) for row in rows]


def _topic_out(db: Session, row: CreatorTopic) -> dict:
    count = db.query(func.count(CreatorTopicAssignment.id)).filter(
        CreatorTopicAssignment.topic_id == row.id
    ).scalar()
    return {
        "id": row.id, "name": row.name, "enabled": bool(row.enabled),
        "archived_at": iso(row.archived_at), "creator_count": int(count or 0),
        "created_at": iso(row.created_at), "updated_at": iso(row.updated_at),
    }


@operations_router.get("/topics")
def list_topics(
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
    include_archived: bool = True,
) -> dict:
    query = db.query(CreatorTopic)
    if q.strip():
        query = query.filter(CreatorTopic.name.like(f"%{q.strip()}%"))
    if not include_archived:
        query = query.filter(CreatorTopic.enabled.is_(True), CreatorTopic.archived_at.is_(None))
    rows = query.order_by(CreatorTopic.enabled.desc(), CreatorTopic.name.asc()).all()
    return {"items": [_topic_out(db, row) for row in rows]}


@operations_router.post("/topics")
def create_topic(payload: TopicMutation, db: Annotated[Session, Depends(get_db)]) -> dict:
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="topic name required")
    row = CreatorTopic(id=new_topic_id(), name=name, enabled=True)
    db.add(row)
    audit(
        db, user_id=None, action="topic.create", actor_id=payload.actor_id,
        actor_role=payload.actor_role, source=payload.source, before=None,
        after={"id": row.id, "name": name, "enabled": True},
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="topic name already exists") from exc
    db.refresh(row)
    return _topic_out(db, row)


@operations_router.patch("/topics/{topic_id}")
def update_topic(
    topic_id: str, payload: TopicMutation, db: Annotated[Session, Depends(get_db)]
) -> dict:
    row = db.get(CreatorTopic, topic_id)
    if row is None:
        raise HTTPException(status_code=404, detail="topic not found")
    before = _topic_out(db, row)
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="topic name required")
        row.name = name
    if payload.enabled is not None:
        row.enabled = payload.enabled
        row.archived_at = None if payload.enabled else row.archived_at
    row.updated_at = now_utc()
    audit(
        db, user_id=None, action="topic.update", actor_id=payload.actor_id,
        actor_role=payload.actor_role, source=payload.source, before=before,
        after={"id": row.id, "name": row.name, "enabled": row.enabled},
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="topic name already exists") from exc
    db.refresh(row)
    return _topic_out(db, row)


@operations_router.post("/topics/{topic_id}/archive")
def archive_topic(
    topic_id: str, payload: TopicArchiveMutation, db: Annotated[Session, Depends(get_db)]
) -> dict:
    row = db.get(CreatorTopic, topic_id)
    if row is None:
        raise HTTPException(status_code=404, detail="topic not found")
    assignments = db.query(CreatorTopicAssignment).filter(
        CreatorTopicAssignment.topic_id == topic_id
    ).all()
    replacement = None
    if assignments and payload.strategy is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "TOPIC_IN_USE", "creator_count": len(assignments)},
        )
    if assignments and payload.strategy == "replace":
        if not payload.replacement_topic_id or payload.replacement_topic_id == topic_id:
            raise HTTPException(status_code=422, detail="active replacement topic required")
        replacement = db.get(CreatorTopic, payload.replacement_topic_id)
        if replacement is None or not replacement.enabled or replacement.archived_at is not None:
            raise HTTPException(status_code=422, detail="active replacement topic required")
    before = _topic_out(db, row)
    affected_user_ids = sorted({assignment.user_id for assignment in assignments})
    creator_before = {
        user_id: snapshot(db, db.get(User, user_id))
        for user_id in affected_user_ids
        if db.get(User, user_id) is not None
    }
    try:
        for assignment in assignments:
            if replacement is not None:
                existing = db.query(CreatorTopicAssignment).filter(
                    CreatorTopicAssignment.user_id == assignment.user_id,
                    CreatorTopicAssignment.topic_id == replacement.id,
                ).one_or_none()
                if existing is None:
                    assignment.topic_id = replacement.id
                    db.add(assignment)
                else:
                    db.delete(assignment)
            else:
                db.delete(assignment)
        db.flush()
        for user_id in affected_user_ids:
            remaining = db.query(CreatorTopicAssignment).filter(
                CreatorTopicAssignment.user_id == user_id
            ).order_by(CreatorTopicAssignment.position.asc()).all()
            for position, assignment in enumerate(remaining):
                assignment.position = position
        db.flush()
        row.enabled = False
        row.archived_at = now_utc()
        row.updated_at = row.archived_at
        audit(
            db, user_id=None, action="topic.archive", actor_id=payload.actor_id,
            actor_role=payload.actor_role, source=payload.source, before=before,
            after={
                "id": row.id, "name": row.name, "enabled": False,
                "strategy": payload.strategy or "none",
                "replacement_topic_id": replacement.id if replacement else None,
                "affected_creators": len(assignments),
            },
        )
        for user_id, prior in creator_before.items():
            creator = db.get(User, user_id)
            if creator is None:
                continue
            audit(
                db,
                user_id=user_id,
                action="creator.topics.replace" if replacement else "creator.topics.remove",
                actor_id=payload.actor_id,
                actor_role=payload.actor_role,
                source=payload.source,
                before=prior,
                after=snapshot(db, creator),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    return _topic_out(db, row)


@operations_router.get("/creators")
def list_creators(
    db: Annotated[Session, Depends(get_db)],
    q: str = "",
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows, total = search_creators(db, q, limit=limit, offset=offset)
    items = []
    for row in rows:
        handle = ensure_user_handle(db, row)
        _links, topics, _pins = channel_relations(db, row.user_id)
        items.append({
            "user_id": row.user_id, "nickname": row.nickname or "", "handle": handle,
            "avatar_url": row.avatar_url or "", "topics": topics,
            "profile_updated_at": iso(row.profile_updated_at),
        })
    if db.new or db.dirty or db.deleted:
        db.commit()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@operations_router.put("/creators/{user_id}/topics")
def set_creator_topics(
    user_id: str,
    payload: CreatorTopicsMutation,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    row = db.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="creator not found")
    before = snapshot(db, row)
    try:
        replace_topics(db, user_id, payload.topic_ids)
        row.profile_updated_at = now_utc()
        db.add(row)
        db.flush()
        after = snapshot(db, row)
        audit(
            db, user_id=user_id, action="creator.topics.update", actor_id=payload.actor_id,
            actor_role=payload.actor_role, source=payload.source, before=before, after=after,
        )
        db.commit()
    except ChannelValidationError as exc:
        db.rollback()
        raise _error(exc) from exc
    return {"user_id": user_id, "topics": after["topics"], "profile_updated_at": iso(row.profile_updated_at)}


@operations_router.get("/audits")
def list_audits(
    db: Annotated[Session, Depends(get_db)],
    user_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    query = db.query(CreatorProfileAudit)
    if user_id:
        query = query.filter(CreatorProfileAudit.user_id == user_id)
    rows = query.order_by(CreatorProfileAudit.created_at.desc(), CreatorProfileAudit.id.desc()).limit(limit).all()
    return {"items": [{
        "id": row.id, "user_id": row.user_id, "action": row.action,
        "actor_id": row.actor_id, "actor_role": row.actor_role, "source": row.source,
        "before": row.before_json, "after": row.after_json, "created_at": iso(row.created_at),
    } for row in rows]}


@media_router.get("/media/backgrounds/{filename}")
def serve_background(
    filename: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    if media_mode_is_oss(settings) and not settings.media_read_fallback_local:
        raise HTTPException(status_code=404, detail="background not found")
    try:
        path = resolve_background_path(settings, filename)
    except AvatarStorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="background not found")
    return FileResponse(
        path, media_type=avatar_media_type(Path(path).name), filename=Path(path).name,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
