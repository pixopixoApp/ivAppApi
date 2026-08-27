from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.avatar_storage import (
    AvatarStorageError,
    avatar_media_type,
    resolve_avatar_path,
    store_cover_image,
    store_user_avatar,
)
from app.cdn_cache import enqueue_prefetch, html_package_public_urls
from app.cdn_publication import (
    CdnPublicationError,
    activate_ready_publications,
    cancel_warming_publications,
    require_runtime_cdn_gate,
    stage_publication_gate,
)
from app.config import Settings, get_settings
from app.db import engine, get_db
from app.deps import require_publish_key
from app.html_content import (
    CONTENT_TYPE_HTML,
    CONTENT_TYPE_RUNTIME,
    HTML_BRIDGE_VERSION,
    HtmlContentError,
    normalize_required_capabilities,
    probe_html_entry,
    validate_html_package_url,
)
from app.impressions import ImpressionUnavailableError, get_impression_store
from app.logging_config import get_logger
from app.media_api import RuntimeObjectPublishRequest, RuntimePreviewRequest
from app.media_service import MediaServiceError, media_mode_is_oss
from app.models import (
    AnalyticsLog,
    CreatorCreation,
    HtmlPackage,
    MediaObject,
    PublishedMediaAsset,
    PublishedVideo,
    User,
    VideoView,
)
from app.oss_storage import OssStorageError, public_url, sign_get_url
from app.private_cdn import sign_private_media_url
from app.protocol_video import (
    RUNTIME_SPEC_VERSION,
    RuntimeSpecError,
    compile_runtime_spec,
    read_runtime_spec,
)
from app.public_origin import (
    PublicOriginError,
    canonical_public_url_for_key,
    canonicalize_public_payload,
    canonicalize_public_url,
)
from app.publication_service import (
    RuntimeSourceAsset,
    load_published_runtime_urls,
    publish_runtime_assets,
)
from app.schemas import (
    AdminUserDeactivateResponse,
    AdminUserListResponse,
    AdminUserOut,
    AdminUserUpsertRequest,
    AnalyticsLogOut,
    AnalyticsLogsResponse,
    BatchUsersRequest,
    BatchUsersResponse,
    BatchVideosRequest,
    BatchVideosResponse,
    ContentManagementUpdateRequest,
    FeedWeightUpdateRequest,
    PublishedVideoInfo,
    PublishHtmlRequest,
    PublishHtmlResponse,
    PublishResponse,
    RuntimeSpecAuditOut,
    Timeline,
    UnpublishResponse,
    UserImpressionsOut,
)
from app.users import (
    USER_SOURCE_ADMIN,
    USER_SOURCE_APP,
    UserIdentityConflict,
    apply_user_update,
    to_profile_fields,
)

router = APIRouter(prefix="/internal/v1", tags=["admin"])
media_router = APIRouter(tags=["media"])
log = get_logger(__name__)

_CONTENT_MODE_SINGLE = "single"
_CONTENT_MODE_STORY = "story"


def _admin_user_out(row: User, settings: Settings | None = None) -> AdminUserOut:
    active_settings = settings or get_settings()
    return AdminUserOut(
        user_id=row.user_id,
        provider=row.provider,
        subject=row.subject,
        enabled=bool(row.enabled),
        nickname=row.nickname or "",
        avatar_url=canonicalize_public_url(active_settings, row.avatar_url) or "",
        bio=row.bio or "",
        source=row.source or "app",
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


def _parse_form_bool(raw: str | bool | None) -> bool | None:
    """Parse optional multipart bool; None means not provided."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("", "none", "null"):
        return None
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise HTTPException(status_code=400, detail="is_tutorial must be true or false")


def _clear_other_tutorials(db: Session, *, keep_video_id: str) -> None:
    (
        db.query(PublishedVideo)
        .filter(
            PublishedVideo.is_tutorial.is_(True),
            PublishedVideo.is_deleted == 0,
            PublishedVideo.id != keep_video_id,
        )
        .update({PublishedVideo.is_tutorial: False}, synchronize_session=False)
    )


def _is_valid_id(value: str) -> bool:
    safe = "".join(ch for ch in value if ch.isalnum() or ch in "-_")
    return bool(safe) and safe == value


def _require_valid_id(value: str, *, label: str = "video_id") -> str:
    if not _is_valid_id(value):
        raise HTTPException(status_code=400, detail=f"invalid {label}")
    return value


def _media_root(settings: Settings) -> Path:
    root = Path(settings.media_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _single_media_path(settings: Settings, item_id: str) -> Path:
    item_id = _require_valid_id(item_id)
    return _media_root(settings) / f"{item_id}.mp4"


def _story_dir(settings: Settings, item_id: str) -> Path:
    item_id = _require_valid_id(item_id)
    return _media_root(settings) / item_id


def _story_clip_path(settings: Settings, item_id: str, clip_id: str) -> Path:
    item_id = _require_valid_id(item_id)
    clip_id = _require_valid_id(clip_id, label="clip_id")
    return _story_dir(settings, item_id) / f"{clip_id}.mp4"


def _single_video_url(item_id: str) -> str:
    return f"/media/{item_id}.mp4"


def _story_clip_url(item_id: str, clip_id: str) -> str:
    return f"/media/{item_id}/{clip_id}.mp4"


def _remove_published_media(settings: Settings, item_id: str) -> None:
    """Remove single file and/or story directory for an item_id."""
    if media_mode_is_oss(settings):
        # OSS objects are immutable and retained permanently. Unpublish/update
        # only changes database bindings.
        return
    if not _is_valid_id(item_id):
        return
    single = _media_root(settings) / f"{item_id}.mp4"
    if single.is_file():
        single.unlink()
        log.info("removed single media item_id=%s path=%s", item_id, single)
    story = _media_root(settings) / item_id
    if story.is_dir():
        shutil.rmtree(story)
        log.info("removed story media dir item_id=%s path=%s", item_id, story)


def _parse_timeline(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"timeline must be JSON: {exc}") from exc
    try:
        return Timeline.model_validate(data).model_dump(mode="python")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid timeline: {exc}") from exc


def _parse_story(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"story must be JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="story must be a JSON object")
    entry = data.get("entry_clip_id")
    clips = data.get("clips")
    if not isinstance(entry, str) or not entry.strip():
        raise HTTPException(status_code=400, detail="story.entry_clip_id required")
    entry = entry.strip()
    if not _is_valid_id(entry):
        raise HTTPException(status_code=400, detail="invalid story.entry_clip_id")
    if not isinstance(clips, dict) or not clips:
        raise HTTPException(status_code=400, detail="story.clips must be a non-empty object")
    normalized_clips: dict[str, Any] = {}
    for raw_cid, body in clips.items():
        cid = str(raw_cid).strip()
        if not _is_valid_id(cid):
            raise HTTPException(status_code=400, detail=f"invalid clip_id: {raw_cid}")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail=f"story.clips[{cid}] must be object")
        timeline = body.get("timeline")
        if timeline is None:
            raise HTTPException(status_code=400, detail=f"story.clips[{cid}].timeline required")
        try:
            tl = Timeline.model_validate(timeline).model_dump(mode="python")
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"invalid timeline for clip {cid}: {exc}"
            ) from exc
        normalized_body: dict[str, Any] = {"timeline": tl}
        if "on_end" in body:
            if not isinstance(body["on_end"], dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"story.clips[{cid}].on_end must be object",
                )
            normalized_body["on_end"] = body["on_end"]
        normalized_clips[cid] = normalized_body
    if entry not in normalized_clips:
        raise HTTPException(status_code=400, detail="entry_clip_id must be a key in clips")
    return {"entry_clip_id": entry, "clips": normalized_clips}


def _clip_id_from_upload(file: UploadFile) -> str:
    name = Path(file.filename or "").name
    if not name.lower().endswith(".mp4"):
        ctype = (file.content_type or "").lower()
        if "mp4" not in ctype and "video" not in ctype:
            raise HTTPException(
                status_code=400,
                detail=f"clip must be .mp4 file (got filename={file.filename!r})",
            )
    stem = Path(name).stem if name else ""
    if not stem or not _is_valid_id(stem):
        raise HTTPException(
            status_code=400,
            detail=f"clip filename must be {{clip_id}}.mp4 (got {file.filename!r})",
        )
    return stem


@router.post(
    "/users",
    response_model=AdminUserOut,
    summary="创建或覆盖用户",
    description="Header 需 X-Publish-Key。按 user_id upsert；可写 provider/subject/enabled/nickname/avatar_url。"
    "avatar_url 为相对路径。provider+subject 冲突返回 HTTP 409。",
)
def upsert_user(
    payload: AdminUserUpsertRequest,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_publish_key)],
) -> AdminUserOut:
    user_id = payload.user_id.strip()
    if not user_id or not payload.subject.strip():
        raise HTTPException(status_code=400, detail="user_id and subject required")

    try:
        row = apply_user_update(
            db,
            user_id=user_id,
            provider=payload.provider,
            subject=payload.subject,
            enabled=payload.enabled,
            nickname=payload.nickname,
            avatar_url=payload.avatar_url,
            bio=payload.bio,
            create_if_missing=True,
        )
        db.commit()
        db.refresh(row)
    except UserIdentityConflict as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"provider+subject already used by user_id={exc.other_user_id}",
        ) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log.info(
        "user upsert user_id=%s provider=%s subject=%s enabled=%s nickname=%s",
        row.user_id,
        row.provider,
        row.subject,
        row.enabled,
        row.nickname,
    )
    return _admin_user_out(row)


@router.post(
    "/users/batch",
    response_model=BatchUsersResponse,
    summary="批量查询用户信息",
    description="Header 需 X-Publish-Key。body.user_ids 最多 200 个；返回 items 与 missing（保序去重）。",
)
def batch_users(
    payload: BatchUsersRequest,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_publish_key)],
) -> BatchUsersResponse:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in payload.user_ids:
        uid = raw.strip() if isinstance(raw, str) else ""
        if not uid or uid in seen:
            continue
        seen.add(uid)
        ordered.append(uid)

    if not ordered:
        raise HTTPException(status_code=400, detail="no valid user_id")

    rows = db.query(User).filter(User.user_id.in_(ordered)).all()
    by_id = {row.user_id: row for row in rows}
    items = [_admin_user_out(by_id[uid]) for uid in ordered if uid in by_id]
    missing = [uid for uid in ordered if uid not in by_id]
    log.info(
        "batch users requested=%d found=%d missing=%d",
        len(ordered),
        len(items),
        len(missing),
    )
    return BatchUsersResponse(items=items, missing=missing)


@router.get(
    "/users",
    response_model=AdminUserListResponse,
    summary="用户列表（分页筛选）",
    description="Header 需 X-Publish-Key。可选筛选 source（app/admin）、enabled、昵称关键字 q；"
    "limit 默认 50（1–200），offset 默认 0。按 created_at 倒序。",
)
def list_users(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_publish_key)],
    source: Annotated[
        str | None, Query(description="创建来源：app 或 admin")
    ] = None,
    enabled: Annotated[bool | None, Query(description="是否启用")] = None,
    q: Annotated[str | None, Query(description="昵称模糊匹配")] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="本页条数")] = 50,
    offset: Annotated[int, Query(ge=0, description="偏移量")] = 0,
) -> AdminUserListResponse:
    query = db.query(User)
    if source is not None and source.strip():
        src = source.strip()
        if src not in (USER_SOURCE_APP, USER_SOURCE_ADMIN):
            raise HTTPException(status_code=400, detail="source must be app or admin")
        query = query.filter(User.source == src)
    if enabled is not None:
        query = query.filter(User.enabled == enabled)
    if q is not None and q.strip():
        query = query.filter(User.nickname.like(f"%{q.strip()}%"))

    total = query.count()
    rows = (
        query.order_by(User.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = [_admin_user_out(row) for row in rows]
    log.info(
        "list users source=%s enabled=%s q=%s limit=%d offset=%d total=%d returned=%d",
        source,
        enabled,
        q,
        limit,
        offset,
        total,
        len(items),
    )
    return AdminUserListResponse(
        items=items, total=total, limit=limit, offset=offset
    )


def _random_user_func():
    """Return a SQLAlchemy random()/rand() expression for the active DB dialect.

    MySQL uses RAND(), while SQLite/PostgreSQL use RANDOM().
    """
    if engine.dialect.name == "mysql":
        return func.rand()
    return func.random()


@router.get(
    "/users/random",
    response_model=AdminUserOut,
    summary="随机抽取一个可用账户",
    description="Header 需 X-Publish-Key。从 users 表中随机返回一个 source=admin、enabled=true、"
    "且不在 exclude_user_ids 中的账户；没有可用账户时返回 HTTP 404。",
)
def random_user(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_publish_key)],
    source: Annotated[
        str, Query(description="创建来源：固定为 admin")
    ] = USER_SOURCE_ADMIN,
    enabled: Annotated[
        bool | None, Query(description="是否启用，默认 true")
    ] = None,
    exclude_user_ids: Annotated[
        list[str] | None,
        Query(description="需要排除的已绑定 user_id 列表（可重复传参）"),
    ] = None,
) -> AdminUserOut:
    src = (source or USER_SOURCE_ADMIN).strip() or USER_SOURCE_ADMIN
    if src != USER_SOURCE_ADMIN:
        raise HTTPException(status_code=400, detail="source must be admin")
    is_enabled = True if enabled is None else enabled

    query = db.query(User).filter(
        User.source == src,
        User.enabled.is_(is_enabled),
    )
    if exclude_user_ids:
        cleaned = [u for u in exclude_user_ids if u and u.strip()]
        if cleaned:
            query = query.filter(~User.user_id.in_(cleaned))

    row = query.order_by(_random_user_func()).limit(1).first()
    if row is None:
        log.info(
            "random user none source=%s enabled=%s exclude_count=%d",
            src,
            is_enabled,
            len(exclude_user_ids or []),
        )
        raise HTTPException(status_code=404, detail="no available unbound account")
    log.info(
        "random user picked source=%s enabled=%s exclude_count=%d",
        src,
        is_enabled,
        len(exclude_user_ids or []),
    )
    return _admin_user_out(row)


@router.get(
    "/users/{user_id}",
    response_model=AdminUserOut,
    summary="查询用户信息",
    description="Header 需 X-Publish-Key。返回用户资料（含昵称、头像相对路径）。不存在返回 HTTP 404。",
)
def get_user(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_publish_key)],
) -> AdminUserOut:
    uid = user_id.strip()
    row = db.get(User, uid)
    if row is None:
        raise HTTPException(status_code=404, detail="user not found")
    fields = to_profile_fields(row)
    log.info("get user user_id=%s enabled=%s", fields["user_id"], fields["enabled"])
    return _admin_user_out(row)


@router.post(
    "/users/{user_id}/deactivate",
    response_model=AdminUserDeactivateResponse,
    summary="停用用户",
    description="Header 需 X-Publish-Key。将 enabled 置为 false；其名下视频对 Feed/单拉不可见。"
    "用户不存在返回 HTTP 404。",
)
def deactivate_user(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_publish_key)],
) -> AdminUserDeactivateResponse:
    uid = user_id.strip()
    try:
        row = apply_user_update(db, user_id=uid, enabled=False, create_if_missing=False)
        db.commit()
    except LookupError:
        db.rollback()
        raise HTTPException(status_code=404, detail="user not found") from None
    log.info("user deactivated user_id=%s", uid)
    return AdminUserDeactivateResponse(user_id=row.user_id, enabled=False)


@router.post(
    "/users/{user_id}/avatar",
    response_model=AdminUserOut,
    summary="上传用户头像",
    description="Header 需 X-Publish-Key。multipart 字段 `file`（jpg/png/webp，最大 2MB）。"
    "落盘 MEDIA_ROOT/avatars，更新 avatar_url 为 `/media/avatars/{user_id}.{ext}`。"
    "用户不存在返回 HTTP 404。",
)
async def upload_user_avatar(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(require_publish_key)],
    file: Annotated[UploadFile, File(description="头像图片文件")],
) -> AdminUserOut:
    uid = user_id.strip()
    if db.get(User, uid) is None:
        raise HTTPException(status_code=404, detail="user not found")

    raw = await file.read()
    try:
        avatar_url, media_object_id = store_user_avatar(
            db,
            settings,
            user_id=uid,
            raw=raw,
            filename=file.filename,
            content_type=file.content_type,
        )
        row = db.get(User, uid)
        if row is None:
            raise LookupError(uid)
        row.avatar_url = avatar_url
        row.avatar_media_object_id = media_object_id
        db.add(row)
        enqueue_prefetch(db, settings, [avatar_url])
        db.commit()
        db.refresh(row)
    except AvatarStorageError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError:
        db.rollback()
        raise HTTPException(status_code=404, detail="user not found") from None
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log.info("admin avatar ok user_id=%s avatar_url=%s", row.user_id, row.avatar_url)
    return _admin_user_out(row)


@router.post(
    "/publish-cover",
    summary="Upload a published cover image into ivapp media storage",
    description="Header 需 X-Publish-Key。multipart field `file`（jpg/png/webp，最大 2MB）。"
    "返回 cover_media_object_id，供 publish-assets 写入 published_videos。",
)
async def publish_cover_upload(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(require_publish_key)],
    file: Annotated[UploadFile, File(description="封面图片文件")],
) -> dict[str, Any]:
    raw = await file.read()
    try:
        cover_url, media_object_id = store_cover_image(
            db,
            settings,
            raw=raw,
            filename=file.filename,
            content_type=file.content_type,
        )
        if media_object_id:
            enqueue_prefetch(db, settings, [cover_url])
        db.commit()
    except AvatarStorageError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"cover_media_object_id": media_object_id, "cover_url": cover_url}


@router.post(
    "/publish",
    response_model=PublishResponse,
    summary="发布视频或 Story",
    description="Header 需 X-Publish-Key。multipart 公共字段：video_id（item_id）、version、user_id、"
    "content_mode（默认 single）、可选 feed_weight（默认 0）、可选 is_tutorial。"
    "**single**：timeline JSON + video（一个 mp4）→ `/media/{video_id}.mp4`。"
    "**story**：story JSON（entry_clip_id + clips）+ 多个 clips 文件（文件名须为 `{clip_id}.mp4`）"
    "→ `/media/{video_id}/{clip_id}.mp4`；入口路径写入 video_url。"
    "作者须存在且启用。成功返回 video_url、content_mode、updated。",
)
async def publish(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(require_publish_key)],
    video_id: Annotated[
        str, Form(min_length=1, max_length=128, description="发布单元幂等键（item_id）")
    ],
    version: Annotated[str, Form(min_length=1, max_length=64, description="内容版本号")],
    user_id: Annotated[str, Form(min_length=1, max_length=64, description="作者 user_id")],
    content_mode: Annotated[
        str, Form(description="内容模式：single 或 story，默认 single")
    ] = _CONTENT_MODE_SINGLE,
    feed_weight: Annotated[
        int | None, Form(description="Feed 权重，越大越靠前；默认 0 / 不传则新建为 0、更新保持原值")
    ] = None,
    is_tutorial: Annotated[
        str | None,
        Form(description="是否教学片；true/false；不传则新建 false、更新保持原值"),
    ] = None,
    created_at: Annotated[
        str | None,
        Form(description="可选：源 runs.created_at（ISO8601），用于 published_videos.created_at"),
    ] = None,
    timeline: Annotated[
        str | None, Form(description="single 模式：timeline JSON 字符串")
    ] = None,
    story: Annotated[
        str | None, Form(description="story 模式：story JSON 字符串")
    ] = None,
    video: Annotated[
        UploadFile | None, File(description="single 模式：mp4 视频文件")
    ] = None,
    clips: Annotated[
        list[UploadFile] | None,
        File(description="story 模式：多个 mp4，文件名为 {clip_id}.mp4"),
    ] = None,
) -> PublishResponse:
    if media_mode_is_oss(settings):
        raise HTTPException(
            status_code=410,
            detail="multipart media publish is disabled; upload to OSS and use /internal/v1/publish-assets",
        )
    item_id = _require_valid_id(video_id.strip())
    mode = (content_mode or _CONTENT_MODE_SINGLE).strip().lower()
    if mode not in (_CONTENT_MODE_SINGLE, _CONTENT_MODE_STORY):
        raise HTTPException(status_code=400, detail="content_mode must be single or story")

    author_id = user_id.strip()
    author = db.get(User, author_id)
    if author is None:
        raise HTTPException(status_code=400, detail="user_id not found")
    if not author.enabled:
        raise HTTPException(status_code=400, detail="user is disabled")

    existing_row = db.get(PublishedVideo, item_id)
    if existing_row is not None and existing_row.content_type != CONTENT_TYPE_RUNTIME:
        raise HTTPException(
            status_code=409,
            detail="item_id is already published with a different content_type",
        )

    if mode == _CONTENT_MODE_STORY:
        if not story or not story.strip():
            raise HTTPException(status_code=400, detail="story JSON required for content_mode=story")
        story_obj = _parse_story(story)
        entry = story_obj["entry_clip_id"]
        expected = set(story_obj["clips"].keys())
        upload_list = list(clips or [])
        if not upload_list:
            raise HTTPException(status_code=400, detail="clips files required for content_mode=story")

        by_clip: dict[str, bytes] = {}
        for upload in upload_list:
            cid = _clip_id_from_upload(upload)
            if cid in by_clip:
                raise HTTPException(status_code=400, detail=f"duplicate clip file for {cid}")
            raw = await upload.read()
            if not raw:
                raise HTTPException(status_code=400, detail=f"empty clip upload: {cid}")
            by_clip[cid] = raw

        missing = sorted(expected - set(by_clip.keys()))
        extra = sorted(set(by_clip.keys()) - expected)
        if missing:
            raise HTTPException(
                status_code=400, detail=f"missing clip files: {', '.join(missing)}"
            )
        if extra:
            raise HTTPException(
                status_code=400, detail=f"unexpected clip files: {', '.join(extra)}"
            )

        payload_json: dict[str, Any] = story_obj
        video_url = _story_clip_url(item_id, entry)
        try:
            runtime_spec = compile_runtime_spec(
                item_id=item_id,
                content_mode=mode,
                source=payload_json,
                video_url=video_url,
            )
        except RuntimeSpecError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        _remove_published_media(settings, item_id)
        dest_dir = _story_dir(settings, item_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        total_bytes = 0
        for cid, raw in by_clip.items():
            path = _story_clip_path(settings, item_id, cid)
            path.write_bytes(raw)
            total_bytes += len(raw)
        detail = f"clips={len(by_clip)} bytes={total_bytes}"
    else:
        if not timeline or not timeline.strip():
            raise HTTPException(
                status_code=400, detail="timeline JSON required for content_mode=single"
            )
        if video is None:
            raise HTTPException(
                status_code=400, detail="video file required for content_mode=single"
            )
        if not video.filename or not video.filename.lower().endswith(".mp4"):
            ctype = (video.content_type or "").lower()
            if "mp4" not in ctype and "video" not in ctype:
                raise HTTPException(status_code=400, detail="video must be an .mp4 file")

        timeline_obj = _parse_timeline(timeline)
        n_interactions = len(timeline_obj.get("interactions") or [])
        raw = await video.read()
        if not raw:
            raise HTTPException(status_code=400, detail="empty video upload")

        payload_json = timeline_obj
        video_url = _single_video_url(item_id)
        try:
            runtime_spec = compile_runtime_spec(
                item_id=item_id,
                content_mode=mode,
                source=payload_json,
                video_url=video_url,
            )
        except RuntimeSpecError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        _remove_published_media(settings, item_id)
        dest = _single_media_path(settings, item_id)
        dest.write_bytes(raw)
        detail = f"interactions={n_interactions} bytes={len(raw)}"

    now = datetime.now(timezone.utc)
    source_created_at = None
    if created_at:
        try:
            source_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="created_at must be ISO8601")
        if source_created_at.tzinfo is None:
            source_created_at = source_created_at.replace(tzinfo=timezone.utc)
    weight = 0 if feed_weight is None else int(feed_weight)
    tutorial_flag = _parse_form_bool(is_tutorial)
    row = existing_row
    if row is None:
        tutorial_value = False if tutorial_flag is None else tutorial_flag
        if tutorial_value:
            _clear_other_tutorials(db, keep_video_id=item_id)
        db.add(
            PublishedVideo(
                id=item_id,
                content_type=CONTENT_TYPE_RUNTIME,
                video_url=video_url,
                timeline=payload_json,
                runtime_spec=runtime_spec,
                runtime_spec_version=RUNTIME_SPEC_VERSION,
                html_url=None,
                bridge_version=None,
                required_capabilities=[],
                version=version,
                user_id=author_id,
                content_mode=mode,
                feed_weight=weight,
                content_source="pgc",
                review_status="approved",
                is_tutorial=tutorial_value,
                created_at=source_created_at or now,
                updated_at=now,
            )
        )
        db.commit()
        log.info(
            "publish created video_id=%s mode=%s user_id=%s version=%s weight=%s tutorial=%s %s url=%s",
            item_id,
            mode,
            author_id,
            version,
            weight,
            tutorial_value,
            detail,
            video_url,
        )
        return PublishResponse(
            video_id=item_id,
            version=version,
            video_url=video_url,
            user_id=author_id,
            content_mode=mode,
            updated=False,
            runtime_spec_version=RUNTIME_SPEC_VERSION,
        )

    row.video_url = video_url
    row.timeline = payload_json
    row.runtime_spec = runtime_spec
    row.runtime_spec_version = RUNTIME_SPEC_VERSION
    row.html_url = None
    row.bridge_version = None
    row.required_capabilities = []
    row.version = version
    row.user_id = author_id
    row.content_mode = mode
    if feed_weight is not None:
        row.feed_weight = weight
    if tutorial_flag is not None:
        if tutorial_flag:
            _clear_other_tutorials(db, keep_video_id=item_id)
        row.is_tutorial = tutorial_flag
    row.updated_at = now
    db.commit()
    log.info(
        "publish updated video_id=%s mode=%s user_id=%s version=%s weight=%s tutorial=%s %s url=%s",
        item_id,
        mode,
        author_id,
        version,
        row.feed_weight,
        bool(row.is_tutorial),
        detail,
        video_url,
    )
    return PublishResponse(
        video_id=item_id,
        version=version,
        video_url=video_url,
        user_id=author_id,
        content_mode=mode,
        updated=True,
        runtime_spec_version=RUNTIME_SPEC_VERSION,
    )


@router.post(
    "/publish-assets",
    response_model=PublishResponse,
    summary="Publish verified OSS runtime assets",
    description="No file bytes cross ivapp. Assets must be finalized media_object ids.",
)
def publish_assets(
    payload: RuntimeObjectPublishRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(require_publish_key)],
) -> PublishResponse:
    if not media_mode_is_oss(settings):
        raise HTTPException(status_code=409, detail="OSS media storage is not enabled")
    try:
        require_runtime_cdn_gate(settings)
    except CdnPublicationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    item_id = _require_valid_id(payload.video_id.strip())
    author_id = payload.user_id.strip()
    author = db.get(User, author_id)
    if author is None:
        raise HTTPException(status_code=400, detail="user_id not found")
    if not author.enabled:
        raise HTTPException(status_code=400, detail="user is disabled")
    existing = db.get(PublishedVideo, item_id)
    if existing is not None and existing.content_type != CONTENT_TYPE_RUNTIME:
        raise HTTPException(
            status_code=409,
            detail="item_id is already published with a different content_type",
        )

    if payload.content_mode == _CONTENT_MODE_SINGLE:
        if payload.timeline is None:
            raise HTTPException(status_code=400, detail="timeline is required")
        try:
            source_payload = Timeline.model_validate(payload.timeline).model_dump(mode="python")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid timeline: {exc}") from exc
        declarations = [item for item in payload.assets if item.role == "single"]
        if len(declarations) != 1 or len(payload.assets) != 1:
            raise HTTPException(status_code=400, detail="single mode requires exactly one single asset")
    else:
        if payload.story is None:
            raise HTTPException(status_code=400, detail="story is required")
        source_payload = _parse_story(json.dumps(payload.story, ensure_ascii=False))
        declarations = [item for item in payload.assets if item.role == "clip"]
        expected = set(source_payload["clips"].keys())
        actual = {item.clip_id for item in declarations}
        if len(declarations) != len(actual) or len(declarations) != len(payload.assets):
            raise HTTPException(status_code=400, detail="story assets must be unique clips")
        if expected != actual:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise HTTPException(
                status_code=400,
                detail={"message": "story asset mismatch", "missing": missing, "extra": extra},
            )

    media_by_id = {
        row.id: row
        for row in db.query(MediaObject)
        .filter(MediaObject.id.in_([item.media_object_id for item in declarations]))
        .all()
    }
    if len(media_by_id) != len(declarations):
        raise HTTPException(status_code=404, detail="one or more media objects were not found")
    sources = [
        RuntimeSourceAsset(
            role=item.role,
            clip_id=item.clip_id,
            media=media_by_id[item.media_object_id],
        )
        for item in declarations
    ]
    try:
        published_assets = publish_runtime_assets(
            db,
            settings,
            video_id=item_id,
            version=payload.version,
            source_payload=source_payload,
            assets=sources,
        )
        video_url = (
            published_assets.urls["single"]
            if payload.content_mode == _CONTENT_MODE_SINGLE
            else published_assets.urls[source_payload["entry_clip_id"]]
        )
        runtime_spec = compile_runtime_spec(
            item_id=item_id,
            content_mode=payload.content_mode,
            source=source_payload,
            video_url=video_url,
            video_urls=(
                published_assets.urls
                if payload.content_mode == _CONTENT_MODE_STORY
                else None
            ),
        )
    except (MediaServiceError, OssStorageError, RuntimeSpecError, KeyError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    now = datetime.now(timezone.utc)
    updated = existing is not None
    source_created_at = payload.created_at
    if source_created_at is not None and source_created_at.tzinfo is None:
        source_created_at = source_created_at.replace(tzinfo=timezone.utc)
    current_tutorial = bool(existing.is_tutorial) if existing is not None else False
    tutorial = (
        bool(payload.is_tutorial)
        if payload.is_tutorial is not None
        else current_tutorial
    )
    current_feed_weight = int(existing.feed_weight) if existing is not None else 0
    target_payload: dict[str, Any] = {
        "content_type": CONTENT_TYPE_RUNTIME,
        "video_url": video_url,
        "timeline": source_payload,
        "runtime_spec": runtime_spec,
        "runtime_spec_version": RUNTIME_SPEC_VERSION,
        "html_url": None,
        "bridge_version": None,
        "required_capabilities": [],
        "active_publication_id": published_assets.publication_id,
        "html_package_id": None,
        "version": payload.version,
        "title": payload.title.strip(),
        "description": payload.description.strip(),
        "cover_media_object_id": (
            (payload.cover_media_object_id or "").strip() or None
        ),
        "user_id": author_id,
        "content_mode": payload.content_mode,
        "feed_weight": (
            int(payload.feed_weight or 0)
            if payload.feed_weight is not None or not updated
            else current_feed_weight
        ),
        "content_source": "pgc",
        "review_status": "approved",
        "is_tutorial": tutorial,
        "is_deleted": 0,
        "deleted_at": None,
    }
    row = existing
    if row is None:
        row = PublishedVideo(
            id=item_id,
            content_type=CONTENT_TYPE_RUNTIME,
            created_at=source_created_at or now,
            distribution_enabled=True,
            cdn_ready=False,
        )
        for field, value in target_payload.items():
            setattr(row, field, value)
        row.updated_at = now
        db.add(row)
    enqueue_prefetch(db, settings, published_assets.urls.values())
    try:
        gate = stage_publication_gate(
            db,
            video_id=item_id,
            publication_id=published_assets.publication_id,
            urls=published_assets.urls.values(),
            staged_payload=target_payload,
        )
        db.flush()
        activate_ready_publications(
            db,
            publication_ids=[published_assets.publication_id],
        )
        db.flush()
    except CdnPublicationError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if gate.state == "failed":
        detail = gate.error_message or "CDN prefetch failed; old publication remains active"
        db.commit()
        raise HTTPException(status_code=503, detail=detail)
    cdn_status = "ready" if gate.state == "active" else "warming"
    db.commit()
    return PublishResponse(
        video_id=item_id,
        version=payload.version,
        video_url=video_url,
        user_id=author_id,
        content_mode=payload.content_mode,
        updated=updated,
        runtime_spec_version=RUNTIME_SPEC_VERSION,
        publication_id=published_assets.publication_id,
        cdn_status=cdn_status,
        poll_after_ms=0 if cdn_status == "ready" else 10_000,
    )


@router.post(
    "/preview-runtime",
    summary="Compile a signed admin preview without publishing it",
)
def preview_runtime(
    payload: RuntimePreviewRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(require_publish_key)],
) -> dict[str, Any]:
    """Return the same runtime spec compiler used by publication, without writes.

    ivadmin owns the preview authorization and sends only the current version's
    business JSON plus already-finalized media object IDs.  This service owns
    OSS URL issuance, so video bytes never transit ivadmin.
    """
    if not media_mode_is_oss(settings):
        raise HTTPException(status_code=409, detail="OSS media storage is not enabled")
    preview_id = _require_valid_id(payload.preview_id.strip(), label="preview_id")
    if payload.content_mode == _CONTENT_MODE_SINGLE:
        if payload.timeline is None:
            raise HTTPException(status_code=400, detail="timeline is required")
        try:
            source_payload = Timeline.model_validate(payload.timeline).model_dump(mode="python")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid timeline: {exc}") from exc
        declarations = [item for item in payload.assets if item.role == "single"]
        if len(declarations) != 1 or len(payload.assets) != 1:
            raise HTTPException(status_code=400, detail="single mode requires exactly one single asset")
    else:
        if payload.story is None:
            raise HTTPException(status_code=400, detail="story is required")
        source_payload = _parse_story(json.dumps(payload.story, ensure_ascii=False))
        declarations = [item for item in payload.assets if item.role == "clip"]
        expected = set(source_payload["clips"].keys())
        actual = {item.clip_id for item in declarations}
        if len(declarations) != len(actual) or len(declarations) != len(payload.assets) or expected != actual:
            raise HTTPException(status_code=400, detail="story assets must exactly match story clips")

    media_by_id = {
        row.id: row
        for row in db.query(MediaObject)
        .filter(MediaObject.id.in_([item.media_object_id for item in declarations]))
        .all()
    }
    if len(media_by_id) != len(declarations) or any(row.state != "ready" for row in media_by_id.values()):
        raise HTTPException(status_code=404, detail="one or more ready media objects were not found")
    ttl = max(
        30,
        min(
            3600,
            settings.private_media_cdn_ttl_seconds
            if settings.private_media_cdn_base_url.strip()
            else settings.oss_private_get_ttl_seconds,
        ),
    )
    try:
        urls = {
            item.clip_id or "single": (
                public_url(settings, media_by_id[item.media_object_id].object_key)
                if media_by_id[item.media_object_id].visibility == "public"
                else sign_private_media_url(
                    settings,
                    key=media_by_id[item.media_object_id].object_key,
                    expires_seconds=ttl,
                    filename=media_by_id[item.media_object_id].original_filename,
                )
            )
            for item in declarations
        }
        entry_url = urls["single"] if payload.content_mode == _CONTENT_MODE_SINGLE else urls[source_payload["entry_clip_id"]]
        runtime_spec = compile_runtime_spec(
            item_id=preview_id,
            content_mode=payload.content_mode,
            source=source_payload,
            video_url=entry_url,
            video_urls=urls if payload.content_mode == _CONTENT_MODE_STORY else None,
        )
    except (OssStorageError, RuntimeSpecError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"schema": "pixo.mobile-preview.v1", "preview_id": preview_id, "runtime_spec": runtime_spec, "media_expires_in": ttl}


@router.post(
    "/publish-html",
    response_model=PublishHtmlResponse,
    summary="发布已验收的 HTML 互动内容",
    description="Header 需 X-Publish-Key。仅写入已上传到受信 HTTPS 域名的不可变内容包。",
)
def publish_html(
    payload: PublishHtmlRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(require_publish_key)],
) -> PublishHtmlResponse:
    item_id = _require_valid_id(payload.item_id.strip(), label="item_id")
    version = payload.version.strip()
    author_id = payload.user_id.strip()
    author = db.get(User, author_id)
    if author is None:
        raise HTTPException(status_code=400, detail="user_id not found")
    if not author.enabled:
        raise HTTPException(status_code=400, detail="user is disabled")

    package = None
    if media_mode_is_oss(settings):
        if not payload.package_id:
            raise HTTPException(status_code=400, detail="package_id is required in OSS mode")
        package = db.get(HtmlPackage, payload.package_id)
        if (
            package is None
            or package.state != "ready"
            or package.item_id != item_id
            or package.version != version
        ):
            raise HTTPException(status_code=400, detail="verified HTML package not found")
        if payload.html_url != package.html_url:
            raise HTTPException(status_code=400, detail="html_url does not match verified package")
    try:
        capabilities = normalize_required_capabilities(payload.required_capabilities)
        html_url = validate_html_package_url(
            payload.html_url,
            item_id=item_id,
            version=version,
            settings=settings,
        )
        if not media_mode_is_oss(settings):
            probe_html_entry(html_url, settings)
    except (HtmlContentError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    row = db.get(PublishedVideo, item_id)
    if row is not None and row.content_type != CONTENT_TYPE_HTML:
        raise HTTPException(
            status_code=409,
            detail="item_id is already published with a different content_type",
        )

    now = datetime.now(timezone.utc)
    normalized_title = payload.title.strip()
    normalized_description = payload.description.strip()
    if not normalized_title:
        raise HTTPException(status_code=400, detail="title required")
    if row is None:
        row = PublishedVideo(
            id=item_id,
            content_type=CONTENT_TYPE_HTML,
            video_url=None,
            timeline=None,
            runtime_spec=None,
            runtime_spec_version=None,
            html_url=html_url,
            bridge_version=HTML_BRIDGE_VERSION,
            required_capabilities=capabilities,
            html_package_id=(package.id if package else None),
            version=version,
            title=normalized_title,
            description=normalized_description,
            user_id=author_id,
            content_mode=_CONTENT_MODE_SINGLE,
            feed_weight=int(payload.feed_weight),
            content_source="manual_upload",
            review_status="approved",
            is_tutorial=False,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        updated = False
    elif row.version == version:
        same_payload = (
            row.html_url == html_url
            and row.bridge_version == HTML_BRIDGE_VERSION
            and list(row.required_capabilities or []) == capabilities
            and (row.title or "") == normalized_title
            and (row.description or "") == normalized_description
            and row.user_id == author_id
            and int(row.feed_weight or 0) == int(payload.feed_weight)
            and row.html_package_id == (package.id if package else None)
        )
        if not same_payload:
            raise HTTPException(
                status_code=409,
                detail="item_id + version already exists with different metadata",
            )
        updated = False
    else:
        row.html_url = html_url
        row.bridge_version = HTML_BRIDGE_VERSION
        row.required_capabilities = capabilities
        row.html_package_id = package.id if package else None
        row.active_publication_id = None
        row.version = version
        row.title = normalized_title
        row.description = normalized_description
        row.user_id = author_id
        row.feed_weight = int(payload.feed_weight)
        row.content_source = "manual_upload"
        row.review_status = "approved"
        row.updated_at = now
        updated = True

    if package is not None:
        enqueue_prefetch(
            db,
            settings,
            html_package_public_urls(db, settings, package_id=package.id),
        )
    db.commit()
    log.info(
        "publish html item_id=%s version=%s user_id=%s updated=%s capabilities=%s url=%s",
        item_id,
        version,
        author_id,
        updated,
        ",".join(capabilities),
        html_url,
    )
    return PublishHtmlResponse(
        item_id=item_id,
        version=version,
        html_url=html_url,
        bridge_version=HTML_BRIDGE_VERSION,
        required_capabilities=capabilities,
        user_id=author_id,
        updated=updated,
    )


def _is_valid_video_id(video_id: str) -> bool:
    return _is_valid_id(video_id)


def _video_info(
    row: PublishedVideo,
    settings: Settings | None = None,
) -> PublishedVideoInfo:
    active_settings = settings or get_settings()
    return PublishedVideoInfo(
        video_id=row.id,
        version=row.version,
        content_type=row.content_type,
        video_url=canonicalize_public_url(active_settings, row.video_url),
        html_url=canonicalize_public_url(active_settings, row.html_url),
        bridge_version=row.bridge_version,
        required_capabilities=list(row.required_capabilities or []),
        user_id=row.user_id,
        content_mode=(row.content_mode or _CONTENT_MODE_SINGLE),
        feed_weight=int(row.feed_weight or 0),
        distribution_enabled=bool(getattr(row, "distribution_enabled", True)),
        is_tutorial=bool(getattr(row, "is_tutorial", False)),
        runtime_spec_version=row.runtime_spec_version,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


def _content_management_out(db: Session, settings: Settings, row: PublishedVideo) -> dict[str, Any]:
    """Small, UI-oriented projection. Binary media is always browser→OSS."""
    author = db.get(User, row.user_id) if row.user_id else None
    cover_url = ""
    if row.cover_media_object_id:
        cover = db.get(MediaObject, row.cover_media_object_id)
        if cover is not None:
            # 封面是公开的 immutable 对象，直接给 CDN / 公开 OSS 的永久 URL，
            # 避免使用 OSS 私有签名 URL（会过期、带签名参数，不适合 <img> 直用）。
            try:
                cover_url = canonical_public_url_for_key(settings, cover.object_key)
            except (OssStorageError, PublicOriginError):
                try:
                    cover_url = public_url(settings, cover.object_key)
                except OssStorageError:
                    log.warning("content cover public url failed video_id=%s", row.id)
    creation = db.query(CreatorCreation).filter(CreatorCreation.published_video_id == row.id).one_or_none()
    return {
        "id": row.id,
        "source": row.content_source or "pgc",
        "content_type": row.content_type,
        "title": row.title or row.id,
        "description": row.description or "",
        "status": row.review_status or "approved",
        "creation_status": creation.status if creation else "published",
        "author_user_id": row.user_id or "",
        "author_nickname": (author.nickname if author else "") or "",
        "feed_weight": int(row.feed_weight or 0),
        "distribution_enabled": bool(getattr(row, "distribution_enabled", True)),
        "cdn_ready": bool(getattr(row, "cdn_ready", True)),
        "is_tutorial": bool(row.is_tutorial),
        "cover_url": cover_url,
        # Runtime cards use this as a video poster fallback; never proxy bytes through ivapp.
        "preview_url": canonicalize_public_url(
            settings,
            row.html_url if row.content_type == CONTENT_TYPE_HTML else row.video_url,
        )
        or "",
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        "reviewed_by": row.reviewed_by or "",
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "review_note": row.review_note or "",
    }


@router.get("/content-management", summary="统一内容管理列表")
def list_content_management(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(require_publish_key)],
    source: str = Query(default="all", pattern="^(all|pgc|ugc|manual_upload)$"),
    status: str = Query(default="all", pattern="^(all|draft|pending|approved|rejected)$"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    q = db.query(PublishedVideo).filter(
        PublishedVideo.is_deleted == 0, PublishedVideo.deleted_at.is_(None)
    )
    if source != "all":
        q = q.filter(PublishedVideo.content_source == source)
    if status != "all":
        q = q.filter(PublishedVideo.review_status == status)
    total = q.count()
    rows = q.order_by(PublishedVideo.updated_at.desc(), PublishedVideo.id.desc()).offset(offset).limit(limit).all()
    return {"items": [_content_management_out(db, settings, row) for row in rows], "total": total}


@router.get("/content-management/{video_id}", summary="统一内容管理详情")
def get_content_management_detail(
    video_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(require_publish_key)],
) -> dict[str, Any]:
    row = db.get(PublishedVideo, video_id)
    if row is None or row.is_deleted != 0 or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="video not found")
    detail = _content_management_out(db, settings, row)
    # This private endpoint is for operations only. It returns metadata/configuration, never media
    # bytes; browser preview continues to load directly from OSS.
    detail["runtime_spec"] = (
        canonicalize_public_payload(settings, row.runtime_spec)
        if row.content_type == CONTENT_TYPE_RUNTIME
        else None
    )
    detail["timeline"] = row.timeline if row.content_type == CONTENT_TYPE_RUNTIME else None
    detail["html_url"] = (
        canonicalize_public_url(settings, row.html_url)
        if row.content_type == CONTENT_TYPE_HTML
        else None
    )
    detail["version"] = row.version
    return detail


@router.patch("/content-management/{video_id}", summary="编辑已生成内容")
def patch_content_management_detail(
    video_id: str,
    payload: ContentManagementUpdateRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(require_publish_key)],
) -> dict[str, Any]:
    """Update operator-owned metadata and, for Runtime, a validated timeline.

    A draft is deliberately never publicly reachable.  Setting a row to draft
    also turns off distribution, so a later query/filter regression cannot
    accidentally put an unfinished work back into the Feed.
    """
    row = db.get(PublishedVideo, video_id)
    if row is None or row.is_deleted != 0 or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="video not found")
    changed = payload.model_fields_set
    if not changed:
        raise HTTPException(status_code=400, detail="at least one editable field is required")

    if "title" in changed:
        row.title = str(payload.title or "").strip()
    if "description" in changed:
        row.description = str(payload.description or "").strip()
    if "review_status" in changed:
        row.review_status = payload.review_status or row.review_status
        if payload.review_status == "draft":
            row.distribution_enabled = False
            row.is_tutorial = False

    if "cover_media_object_id" in changed:
        # 校验封面 media object 存在且可用（发布后由 ivadmin 通过 publish-cover 上传）。
        cover_id = (payload.cover_media_object_id or "").strip() or None
        if cover_id is not None:
            cover = db.get(MediaObject, cover_id)
            if cover is None or cover.purpose != "cover":
                raise HTTPException(status_code=400, detail="cover media object not found")
        row.cover_media_object_id = cover_id

    if "timeline" in changed:
        if row.content_type != CONTENT_TYPE_RUNTIME:
            raise HTTPException(status_code=409, detail="HTML content has no runtime timeline")
        if not isinstance(payload.timeline, dict):
            raise HTTPException(status_code=400, detail="timeline must be an object")
        if not row.video_url:
            raise HTTPException(status_code=409, detail="runtime video_url is missing")
        try:
            story_urls = None
            if (row.content_mode or _CONTENT_MODE_SINGLE) == _CONTENT_MODE_STORY:
                # Reuse the publication parser so editing a Story does not
                # discard its clip graph or per-clip on_end actions.
                source = _parse_story(json.dumps(payload.timeline, ensure_ascii=False))
                if not row.active_publication_id:
                    raise MediaServiceError("story runtime publication is missing")
                story_urls = load_published_runtime_urls(
                    db, settings, video_id=row.id, publication_id=row.active_publication_id
                )
            else:
                source = Timeline.model_validate(payload.timeline).model_dump(
                    mode="python", exclude_none=True
                )
            row.timeline = source
            row.runtime_spec = compile_runtime_spec(
                item_id=row.id,
                content_mode=row.content_mode or _CONTENT_MODE_SINGLE,
                source=source,
                video_url=canonicalize_public_url(settings, row.video_url) or row.video_url,
                video_urls=story_urls,
            )
            row.runtime_spec_version = RUNTIME_SPEC_VERSION
        except HTTPException:
            raise
        except (MediaServiceError, OssStorageError, RuntimeSpecError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"invalid runtime timeline: {exc}") from exc

    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    detail = _content_management_out(db, settings, row)
    detail["runtime_spec"] = (
        canonicalize_public_payload(settings, row.runtime_spec)
        if row.content_type == CONTENT_TYPE_RUNTIME
        else None
    )
    detail["timeline"] = row.timeline if row.content_type == CONTENT_TYPE_RUNTIME else None
    detail["html_url"] = (
        canonicalize_public_url(settings, row.html_url)
        if row.content_type == CONTENT_TYPE_HTML
        else None
    )
    detail["version"] = row.version
    return detail


@router.get("/videos/{video_id}/metrics", summary="获取内容播放聚合指标")
def get_video_metrics(
    video_id: str,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_publish_key)],
) -> dict[str, Any]:
    """Return compact operational metrics; raw client events stay out of normal UI reads."""
    row = db.get(PublishedVideo, video_id)
    if row is None or row.is_deleted != 0 or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="video not found")

    unique_view_count, first_viewed_at, last_viewed_at = (
        db.query(
            func.count(VideoView.id),
            func.min(VideoView.first_viewed_at),
            func.max(VideoView.first_viewed_at),
        )
        .filter(VideoView.video_id == row.id)
        .one()
    )
    telemetry_event_count, last_telemetry_at = (
        db.query(func.count(AnalyticsLog.id), func.max(AnalyticsLog.created_at))
        .filter(AnalyticsLog.video_id == row.id)
        .one()
    )
    return {
        "video_id": row.id,
        # A view is recorded only after Android reports actual media playback.
        "unique_view_count": int(unique_view_count or 0),
        "first_viewed_at": first_viewed_at.isoformat() if first_viewed_at else None,
        "last_viewed_at": last_viewed_at.isoformat() if last_viewed_at else None,
        # Kept for observability only. It is not a play count and is never used for ranking.
        "telemetry_event_count": int(telemetry_event_count or 0),
        "last_telemetry_at": last_telemetry_at.isoformat() if last_telemetry_at else None,
    }


@router.post("/videos/{video_id}/review", summary="审核 UGC 内容")
def review_video(
    video_id: str,
    payload: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_publish_key)],
) -> dict[str, Any]:
    row = db.get(PublishedVideo, video_id)
    if row is None or row.is_deleted != 0 or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="video not found")
    if row.content_source != "ugc":
        raise HTTPException(status_code=409, detail="only UGC content requires review")
    decision = str(payload.get("status") or "").strip()
    if decision not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="status must be approved or rejected")
    row.review_status = decision
    row.reviewed_by = str(payload.get("reviewed_by") or "").strip()[:128]
    row.review_note = str(payload.get("note") or "").strip()[:500]
    row.reviewed_at = datetime.now(timezone.utc)
    row.updated_at = row.reviewed_at
    creation = db.query(CreatorCreation).filter(CreatorCreation.published_video_id == row.id).one_or_none()
    if creation is not None:
        creation.status = "published" if decision == "approved" else "rejected"
        creation.progress_stage = creation.status
        creation.updated_at = row.reviewed_at
    db.commit()
    return _video_info(row).model_dump() | {"review_status": row.review_status}


@router.get(
    "/runtime-specs/audit",
    response_model=RuntimeSpecAuditOut,
    summary="审计持久化播放协议",
)
def audit_runtime_specs(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_publish_key)],
) -> RuntimeSpecAuditOut:
    rows = (
        db.query(PublishedVideo)
        .filter(PublishedVideo.content_type == CONTENT_TYPE_RUNTIME)
        .order_by(PublishedVideo.id.asc())
        .all()
    )
    missing: list[str] = []
    invalid: dict[str, str] = {}
    ready = 0
    for row in rows:
        if row.runtime_spec is None or not row.runtime_spec_version:
            missing.append(row.id)
            continue
        try:
            read_runtime_spec(
                row.runtime_spec,
                item_id=row.id,
                version=row.runtime_spec_version,
            )
        except RuntimeSpecError as exc:
            invalid[row.id] = str(exc)
        else:
            ready += 1
    return RuntimeSpecAuditOut(
        total=len(rows),
        ready=ready,
        missing_video_ids=missing,
        invalid=invalid,
    )


@router.post(
    "/videos/{video_id}/runtime-spec/recompile",
    response_model=PublishedVideoInfo,
    summary="显式重新编译单个作品的持久化播放协议",
)
def recompile_video_runtime_spec(
    video_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(require_publish_key)],
) -> PublishedVideoInfo:
    row = db.get(PublishedVideo, video_id)
    if row is None:
        raise HTTPException(status_code=404, detail="video not found")
    if row.content_type != CONTENT_TYPE_RUNTIME:
        raise HTTPException(status_code=409, detail="HTML content has no runtime spec")
    if not row.video_url:
        raise HTTPException(status_code=409, detail="runtime video_url is missing")
    source = row.timeline if isinstance(row.timeline, dict) else {}
    try:
        story_urls = None
        if (row.content_mode or _CONTENT_MODE_SINGLE) == _CONTENT_MODE_STORY:
            if not row.active_publication_id:
                raise MediaServiceError("story runtime publication is missing")
            story_urls = load_published_runtime_urls(
                db,
                settings,
                video_id=row.id,
                publication_id=row.active_publication_id,
            )
        spec = compile_runtime_spec(
            item_id=row.id,
            content_mode=row.content_mode or _CONTENT_MODE_SINGLE,
            source=source,
            video_url=canonicalize_public_url(settings, row.video_url) or row.video_url,
            video_urls=story_urls,
        )
    except (MediaServiceError, OssStorageError, RuntimeSpecError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    row.runtime_spec = spec
    row.runtime_spec_version = RUNTIME_SPEC_VERSION
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _video_info(row)


@router.get(
    "/videos/{video_id}",
    response_model=PublishedVideoInfo,
    summary="查询已发布视频",
    description="Header 需 X-Publish-Key。返回元数据（不含 timeline）。不存在返回 HTTP 404。",
)
def get_video(
    video_id: str,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_publish_key)],
) -> PublishedVideoInfo:
    if not _is_valid_video_id(video_id):
        raise HTTPException(status_code=400, detail="invalid video_id")

    row = db.get(PublishedVideo, video_id)
    if row is None or row.is_deleted != 0:
        raise HTTPException(status_code=404, detail="video not found")

    log.info("get video video_id=%s version=%s", video_id, row.version)
    return _video_info(row)


@router.post(
    "/videos/{video_id}/feed",
    response_model=PublishedVideoInfo,
    summary="更新 Feed 运营字段",
    description="Header 需 X-Publish-Key。body 可选 feed_weight / is_tutorial / distribution_enabled（至少一项）；"
    "is_tutorial=true 时清其它教学标记。返回视频元数据。不存在 → 404。",
)
def update_video_feed_weight(
    video_id: str,
    payload: FeedWeightUpdateRequest,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_publish_key)],
) -> PublishedVideoInfo:
    if not _is_valid_video_id(video_id):
        raise HTTPException(status_code=400, detail="invalid video_id")

    if (
        payload.feed_weight is None
        and payload.is_tutorial is None
        and payload.distribution_enabled is None
    ):
        raise HTTPException(
            status_code=400, detail="feed_weight, is_tutorial, or distribution_enabled required"
        )

    row = db.get(PublishedVideo, video_id)
    if row is None or row.is_deleted != 0:
        raise HTTPException(status_code=404, detail="video not found")

    if payload.feed_weight is not None:
        row.feed_weight = int(payload.feed_weight)
    if payload.is_tutorial is not None:
        if payload.is_tutorial:
            _clear_other_tutorials(db, keep_video_id=video_id)
        row.is_tutorial = bool(payload.is_tutorial)
    if payload.distribution_enabled is not None:
        row.distribution_enabled = bool(payload.distribution_enabled)
        if not row.distribution_enabled:
            row.is_tutorial = False
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    log.info(
        "feed fields updated video_id=%s feed_weight=%s is_tutorial=%s distribution_enabled=%s",
        video_id,
        row.feed_weight,
        bool(row.is_tutorial),
        bool(row.distribution_enabled),
    )
    return _video_info(row)


@router.get(
    "/users/{user_id}/impressions",
    response_model=UserImpressionsOut,
    summary="调试：查看用户曝光去重池",
    description="Header 需 X-Publish-Key。用户不存在 → 404；存在则读 Redis Set（可为空）。",
)
def get_user_impressions(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_publish_key)],
) -> UserImpressionsOut:
    uid = user_id.strip()
    if not uid or db.get(User, uid) is None:
        raise HTTPException(status_code=404, detail="user not found")

    try:
        video_ids = sorted(get_impression_store().list_seen_ids(user_id=uid))
    except ImpressionUnavailableError as exc:
        raise HTTPException(status_code=503, detail="redis unavailable") from exc

    log.info("impressions debug user_id=%s count=%d", uid, len(video_ids))
    return UserImpressionsOut(
        user_id=uid,
        count=len(video_ids),
        video_ids=video_ids,
    )


@router.delete(
    "/videos/{video_id}",
    response_model=UnpublishResponse,
    summary="取消发布",
    description="Header 需 X-Publish-Key。删除 published_videos 记录及对应 mp4；埋点日志保留。"
    "不存在返回 HTTP 404。",
)
def unpublish_video(
    video_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(require_publish_key)],
) -> UnpublishResponse:
    if not _is_valid_video_id(video_id):
        raise HTTPException(status_code=400, detail="invalid video_id")

    row = db.get(PublishedVideo, video_id)
    if row is None:
        raise HTTPException(status_code=404, detail="video not found")

    cancel_warming_publications(
        db,
        video_id=video_id,
        reason="operator unpublished the video while CDN was warming",
    )
    db.delete(row)
    db.commit()

    _remove_published_media(settings, video_id)
    log.info("unpublish ok video_id=%s", video_id)
    return UnpublishResponse(video_id=video_id, deleted=True)


@router.post(
    "/videos/{video_id}/trash",
    response_model=UnpublishResponse,
    summary="标记删除（移入回收站）",
    description="Header 需 X-Publish-Key。将 published_videos 对应记录标记为删除（软删除，写入 deleted_at），"
    "不删除媒体文件，可恢复。不存在返回 HTTP 404。",
)
def trash_published_video(
    video_id: str,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_publish_key)],
) -> UnpublishResponse:
    if not _is_valid_video_id(video_id):
        raise HTTPException(status_code=400, detail="invalid video_id")

    row = db.get(PublishedVideo, video_id)
    if row is None:
        raise HTTPException(status_code=404, detail="video not found")

    if not bool(row.is_deleted) or row.deleted_at is None:
        row.is_deleted = 1
        row.deleted_at = datetime.now(timezone.utc)
        row.distribution_enabled = False
        cancel_warming_publications(
            db,
            video_id=video_id,
            reason="operator moved the video to trash while CDN was warming",
        )
        row.updated_at = row.deleted_at
        db.commit()
    log.info("trash published video video_id=%s", video_id)
    return UnpublishResponse(video_id=video_id, deleted=True)


@router.post(
    "/videos/{video_id}/restore",
    response_model=UnpublishResponse,
    summary="恢复已删除视频",
    description="Header 需 X-Publish-Key。清除 published_videos 对应记录的删除标记（is_deleted/deleted_at），"
    "并恢复分发。不存在或未删除返回 HTTP 404。",
)
def restore_published_video(
    video_id: str,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_publish_key)],
) -> UnpublishResponse:
    if not _is_valid_video_id(video_id):
        raise HTTPException(status_code=400, detail="invalid video_id")

    row = db.get(PublishedVideo, video_id)
    if row is None or (not bool(row.is_deleted) and row.deleted_at is None):
        raise HTTPException(status_code=404, detail="video not found or not deleted")

    row.is_deleted = 0
    row.deleted_at = None
    row.distribution_enabled = True
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    log.info("restore published video video_id=%s", video_id)
    return UnpublishResponse(video_id=video_id, deleted=False)


@router.post(
    "/videos/batch",
    response_model=BatchVideosResponse,
    summary="批量查询已发布视频",
    description="Header 需 X-Publish-Key。body.video_ids 最多 200 个；返回 items 与 missing。",
)
def batch_videos(
    payload: BatchVideosRequest,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_publish_key)],
) -> BatchVideosResponse:
    # Dedupe while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    invalid: list[str] = []
    for raw in payload.video_ids:
        vid = raw.strip() if isinstance(raw, str) else ""
        if not vid or not _is_valid_video_id(vid):
            if raw not in invalid:
                invalid.append(raw if isinstance(raw, str) else "")
            continue
        if vid in seen:
            continue
        seen.add(vid)
        ordered.append(vid)

    if not ordered and invalid:
        raise HTTPException(status_code=400, detail="no valid video_id")

    rows = (
        db.query(PublishedVideo)
        .filter(PublishedVideo.id.in_(ordered), PublishedVideo.is_deleted == 0)
        .all()
        if ordered
        else []
    )
    by_id = {row.id: row for row in rows}
    items = [_video_info(by_id[vid]) for vid in ordered if vid in by_id]
    missing = [vid for vid in ordered if vid not in by_id] + [v for v in invalid if v]
    log.info(
        "batch videos requested=%d found=%d missing=%d",
        len(ordered),
        len(items),
        len(missing),
    )
    return BatchVideosResponse(items=items, missing=missing)


@router.get(
    "/logs",
    response_model=AnalyticsLogsResponse,
    summary="拉取埋点日志",
    description="Header 需 X-Publish-Key。必填查询参数 video_id；可选 limit、after_id、token 过滤。",
)
def list_logs(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[None, Depends(require_publish_key)],
    video_id: Annotated[str, Query(min_length=1, max_length=128, description="发布视频 id")],
    limit: Annotated[int, Query(ge=1, le=500, description="返回条数上限")] = 100,
    after_id: Annotated[
        int | None, Query(ge=0, description="仅返回 id 大于该值的记录")
    ] = None,
    token: Annotated[str | None, Query(description="按客户端 token 过滤，可选")] = None,
) -> AnalyticsLogsResponse:
    q = (
        db.query(AnalyticsLog)
        .filter(AnalyticsLog.video_id == video_id.strip())
        .order_by(AnalyticsLog.id.asc())
    )
    if after_id is not None:
        q = q.filter(AnalyticsLog.id > after_id)
    if token is not None and token.strip():
        q = q.filter(AnalyticsLog.token == token.strip())
    rows = q.limit(limit).all()
    items = [
        AnalyticsLogOut(
            id=row.id,
            video_id=row.video_id,
            token=row.token,
            data=row.data,
            created_at=row.created_at.isoformat() if row.created_at else "",
        )
        for row in rows
    ]
    log.info(
        "logs video_id=%s limit=%d after_id=%s token=%s returned=%d",
        video_id,
        limit,
        after_id,
        token,
        len(items),
    )
    return AnalyticsLogsResponse(items=items)


@media_router.get(
    "/media/avatars/{filename}",
    summary="下载/查看头像",
    description="按文件名直出头像（相对路径 `/media/avatars/{user_id}.{ext}`）。"
    "文件不存在返回 HTTP 404。无需 Publish-Key。",
)
def serve_avatar(
    filename: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    if media_mode_is_oss(settings) and not settings.media_read_fallback_local:
        raise HTTPException(status_code=404, detail="avatar not found")
    try:
        path = resolve_avatar_path(settings, filename)
    except AvatarStorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="avatar not found")
    return FileResponse(
        path,
        media_type=avatar_media_type(path.name),
        filename=path.name,
    )


@media_router.get(
    "/media/{item_id}/{clip_id}.mp4",
    summary="下载/播放 Story clip mp4",
    description="按发布单元 item_id + clip_id 直出 Story 分段视频。"
    "相对路径 `/media/{item_id}/{clip_id}.mp4`。文件不存在 → 404。无需 Publish-Key。",
)
def serve_story_media(
    item_id: str,
    clip_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    if not _is_valid_id(item_id) or not _is_valid_id(clip_id):
        raise HTTPException(status_code=400, detail="invalid media path")
    if media_mode_is_oss(settings):
        video = db.get(PublishedVideo, item_id)
        if video is not None and video.active_publication_id:
            binding = (
                db.query(PublishedMediaAsset)
                .filter(
                    PublishedMediaAsset.video_id == item_id,
                    PublishedMediaAsset.publication_id == video.active_publication_id,
                    PublishedMediaAsset.role == "clip",
                    PublishedMediaAsset.clip_id == clip_id,
                )
                .one_or_none()
            )
            media = db.get(MediaObject, binding.media_object_id) if binding else None
            if media is not None:
                return RedirectResponse(public_url(settings, media.object_key), status_code=307)
        if not settings.media_read_fallback_local:
            raise HTTPException(status_code=404, detail="media not found")
    path = _story_clip_path(settings, item_id, clip_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="media not found")
    return FileResponse(path, media_type="video/mp4", filename=f"{clip_id}.mp4")


@media_router.get(
    "/media/{video_id}.mp4",
    summary="下载/播放单视频 mp4",
    description="按发布单元 video_id（item_id）直出 single 模式视频。"
    "文件不存在返回 HTTP 404。无需 Publish-Key。",
)
def serve_media(
    video_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    if not _is_valid_id(video_id):
        raise HTTPException(status_code=400, detail="invalid video_id")
    if media_mode_is_oss(settings):
        video = db.get(PublishedVideo, video_id)
        if video is not None and video.active_publication_id:
            binding = (
                db.query(PublishedMediaAsset)
                .filter(
                    PublishedMediaAsset.video_id == video_id,
                    PublishedMediaAsset.publication_id == video.active_publication_id,
                    PublishedMediaAsset.role == "single",
                )
                .one_or_none()
            )
            media = db.get(MediaObject, binding.media_object_id) if binding else None
            if media is not None:
                return RedirectResponse(public_url(settings, media.object_key), status_code=307)
        if not settings.media_read_fallback_local:
            raise HTTPException(status_code=404, detail="media not found")
    path = _single_media_path(settings, video_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="media not found")
    return FileResponse(path, media_type="video/mp4", filename=f"{video_id}.mp4")
