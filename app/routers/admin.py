from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.avatar_storage import (
    AvatarStorageError,
    avatar_media_type,
    resolve_avatar_path,
    save_user_avatar,
)
from app.config import Settings, get_settings
from app.db import get_db
from app.deps import require_publish_key
from app.impressions import ImpressionUnavailableError, get_impression_store
from app.logging_config import get_logger
from app.models import AnalyticsLog, PublishedVideo, User
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
    FeedWeightUpdateRequest,
    PublishedVideoInfo,
    PublishResponse,
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


def _admin_user_out(row: User) -> AdminUserOut:
    return AdminUserOut(
        user_id=row.user_id,
        provider=row.provider,
        subject=row.subject,
        enabled=bool(row.enabled),
        nickname=row.nickname or "",
        avatar_url=row.avatar_url or "",
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
    except Exception as exc:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400, detail=f"invalid timeline for clip {cid}: {exc}"
            ) from exc
        normalized_clips[cid] = {"timeline": tl}
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
    file: UploadFile = File(..., description="头像图片文件"),
) -> AdminUserOut:
    uid = user_id.strip()
    if db.get(User, uid) is None:
        raise HTTPException(status_code=404, detail="user not found")

    raw = await file.read()
    try:
        avatar_url = save_user_avatar(
            settings,
            user_id=uid,
            raw=raw,
            filename=file.filename,
            content_type=file.content_type,
        )
        row = apply_user_update(
            db,
            user_id=uid,
            avatar_url=avatar_url,
            create_if_missing=False,
        )
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

        _remove_published_media(settings, item_id)
        dest_dir = _story_dir(settings, item_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        total_bytes = 0
        for cid, raw in by_clip.items():
            path = _story_clip_path(settings, item_id, cid)
            path.write_bytes(raw)
            total_bytes += len(raw)

        payload_json: dict[str, Any] = story_obj
        video_url = _story_clip_url(item_id, entry)
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

        _remove_published_media(settings, item_id)
        dest = _single_media_path(settings, item_id)
        dest.write_bytes(raw)

        payload_json = timeline_obj
        video_url = _single_video_url(item_id)
        detail = f"interactions={n_interactions} bytes={len(raw)}"

    now = datetime.now(timezone.utc)
    weight = 0 if feed_weight is None else int(feed_weight)
    tutorial_flag = _parse_form_bool(is_tutorial)
    row = db.get(PublishedVideo, item_id)
    if row is None:
        tutorial_value = False if tutorial_flag is None else tutorial_flag
        if tutorial_value:
            _clear_other_tutorials(db, keep_video_id=item_id)
        db.add(
            PublishedVideo(
                id=item_id,
                video_url=video_url,
                timeline=payload_json,
                version=version,
                user_id=author_id,
                content_mode=mode,
                feed_weight=weight,
                is_tutorial=tutorial_value,
                created_at=now,
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
        )

    row.video_url = video_url
    row.timeline = payload_json
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
    )


def _is_valid_video_id(video_id: str) -> bool:
    return _is_valid_id(video_id)


def _video_info(row: PublishedVideo) -> PublishedVideoInfo:
    return PublishedVideoInfo(
        video_id=row.id,
        version=row.version,
        video_url=row.video_url,
        user_id=row.user_id,
        content_mode=(row.content_mode or _CONTENT_MODE_SINGLE),
        feed_weight=int(row.feed_weight or 0),
        is_tutorial=bool(getattr(row, "is_tutorial", False)),
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


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
    if row is None:
        raise HTTPException(status_code=404, detail="video not found")

    log.info("get video video_id=%s version=%s", video_id, row.version)
    return _video_info(row)


@router.post(
    "/videos/{video_id}/feed",
    response_model=PublishedVideoInfo,
    summary="更新 Feed 运营字段",
    description="Header 需 X-Publish-Key。body 可选 feed_weight / is_tutorial（至少一项）；"
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

    if payload.feed_weight is None and payload.is_tutorial is None:
        raise HTTPException(
            status_code=400, detail="feed_weight or is_tutorial required"
        )

    row = db.get(PublishedVideo, video_id)
    if row is None:
        raise HTTPException(status_code=404, detail="video not found")

    if payload.feed_weight is not None:
        row.feed_weight = int(payload.feed_weight)
    if payload.is_tutorial is not None:
        if payload.is_tutorial:
            _clear_other_tutorials(db, keep_video_id=video_id)
        row.is_tutorial = bool(payload.is_tutorial)
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    log.info(
        "feed fields updated video_id=%s feed_weight=%s is_tutorial=%s",
        video_id,
        row.feed_weight,
        bool(row.is_tutorial),
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

    db.delete(row)
    db.commit()

    _remove_published_media(settings, video_id)
    log.info("unpublish ok video_id=%s", video_id)
    return UnpublishResponse(video_id=video_id, deleted=True)


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

    rows = db.query(PublishedVideo).filter(PublishedVideo.id.in_(ordered)).all() if ordered else []
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
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    if not _is_valid_id(item_id) or not _is_valid_id(clip_id):
        raise HTTPException(status_code=400, detail="invalid media path")
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
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    if not _is_valid_id(video_id):
        raise HTTPException(status_code=400, detail="invalid video_id")
    path = _single_media_path(settings, video_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="media not found")
    return FileResponse(path, media_type="video/mp4", filename=f"{video_id}.mp4")
