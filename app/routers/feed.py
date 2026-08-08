from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth_user import load_app_user, require_app_user, resolve_current_user
from app.config import Settings, get_settings
from app.db import get_db
from app.feed_rank import build_feed_sequence, page_circular, pin_tutorial
from app.impressions import ImpressionUnavailableError, get_impression_store
from app.logging_config import get_logger
from app.mail import send_verification_code
from app.models import AnalyticsLog, EmailCode, PublishedVideo, RecommendCursor, User, UserToken
from app.google_auth import verify_google_id_token
from app.protocol_envelope import (
    google_login_error,
    google_login_ok,
    impression_error,
    impression_ok,
    resolve_token,
    send_code_error,
    send_code_ok,
    track_error,
    track_ok,
    verify_error,
    verify_ok,
    video_detail_error,
    video_detail_ok,
    video_error,
    video_ok,
)
from app.protocol_video import story_to_video, timeline_to_video
from app.schemas import (
    ClipOut,
    FeedItemOut,
    GoogleLoginRequest,
    GoogleLoginResponse,
    ImpressionRequest,
    ImpressionResponse,
    SendCodeRequest,
    SendCodeResponse,
    TrackRequest,
    TrackResponse,
    VerifyBodyOut,
    VerifyRequest,
    VerifyResponse,
    VideoBodyOut,
    VideoDetailRequest,
    VideoDetailResponse,
    VideoRequest,
    VideoResponse,
)
from app.users import get_or_create_user, is_author_visible, needs_birthday

public_router = APIRouter(tags=["feed"])
auth_router = APIRouter(tags=["feed"], dependencies=[Depends(require_app_user)])
log = get_logger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CONTENT_MODE_STORY = "story"


def _normalize_email(raw: str) -> str:
    return raw.strip().lower()


def _valid_email(email: str) -> bool:
    return bool(email) and _EMAIL_RE.match(email) is not None


def _story_clip_url(item_id: str, clip_id: str) -> str:
    return f"/media/{item_id}/{clip_id}.mp4"


def _item_from_published(db: Session, row: PublishedVideo) -> FeedItemOut | None:
    avatar_url = ""
    nickname = ""
    if row.user_id:
        author = db.get(User, row.user_id)
        if author is not None:
            avatar_url = author.avatar_url or ""
            nickname = author.nickname or ""

    mode = (row.content_mode or "single").strip().lower()
    clips_raw: list[dict] = []
    if mode == _CONTENT_MODE_STORY:
        story = row.timeline if isinstance(row.timeline, dict) else None
        url_by_clip: dict[str, str] = {}
        if isinstance(story, dict):
            clips_in = story.get("clips")
            if isinstance(clips_in, dict):
                for cid in clips_in:
                    url_by_clip[str(cid)] = _story_clip_url(row.id, str(cid))
        clips_raw = story_to_video(story, url_by_clip)
    else:
        clip = timeline_to_video(row.timeline, row.video_url, clip_id=row.id)
        clips_raw = [clip]

    if not clips_raw:
        return None

    clips = [ClipOut.model_validate(c) for c in clips_raw]
    return FeedItemOut(
        item_id=row.id,
        user_id=row.user_id,
        nickname=nickname,
        avatar_url=avatar_url,
        video=clips,
    )


def list_published_items(
    db: Session,
    *,
    author_user_ids: list[str] | None = None,
    limit: int = 10,
) -> list[FeedItemOut]:
    """List published items by author ids (newest first), skipping invisible authors."""
    q = db.query(PublishedVideo)
    if author_user_ids is not None:
        if not author_user_ids:
            return []
        q = q.filter(PublishedVideo.user_id.in_(author_user_ids))
    rows = q.order_by(PublishedVideo.created_at.desc()).limit(max(limit * 3, limit)).all()
    items: list[FeedItemOut] = []
    for row in rows:
        if not is_author_visible(db, row.user_id):
            continue
        item = _item_from_published(db, row)
        if item is None:
            continue
        items.append(item)
        if len(items) >= limit:
            break
    return items

def _ordered_pool(db: Session) -> tuple[list[str], str | None]:
    """Return (weight-ordered visible ids, tutorial_id or None)."""
    rows = (
        db.query(PublishedVideo)
        .order_by(PublishedVideo.feed_weight.desc(), PublishedVideo.id.asc())
        .all()
    )
    ordered: list[str] = []
    tutorial_id: str | None = None
    for row in rows:
        if not is_author_visible(db, row.user_id):
            continue
        ordered.append(row.id)
        if bool(getattr(row, "is_tutorial", False)) and tutorial_id is None:
            tutorial_id = row.id
    return ordered, tutorial_id


def _next_video_ids(
    db: Session,
    *,
    token: str,
    limit: int,
    user_id: str | None,
) -> list[str]:
    weight_ordered, tutorial_id = _ordered_pool(db)
    ordered = pin_tutorial(weight_ordered, tutorial_id=tutorial_id)
    seen_ids: set[str] | None = None
    if user_id:
        try:
            seen_ids = get_impression_store().list_seen_ids(user_id=user_id)
        except ImpressionUnavailableError:
            log.warning(
                "video feed redis unavailable; weight-only no sink user_id=%s",
                user_id,
            )
            seen_ids = None

    sequence = build_feed_sequence(ordered, seen_ids=seen_ids)
    n = len(sequence)

    state = db.get(RecommendCursor, token)
    cursor = state.cursor if state is not None else 0
    out, new_cursor = page_circular(sequence, cursor=cursor, limit=limit)

    if state is None:
        db.add(RecommendCursor(token=token, cursor=new_cursor))
    else:
        state.cursor = new_cursor
    db.commit()

    log.info(
        "video feed token=%s user_id=%s pool=%d tutorial=%s seen=%s cursor=%d->%d limit=%d returned=%d",
        token,
        user_id or "-",
        n,
        tutorial_id or "-",
        len(seen_ids) if seen_ids is not None else "-",
        cursor % n if n else 0,
        new_cursor,
        limit,
        len(out),
    )
    return out


@public_router.post(
    "/send_code",
    response_model=SendCodeResponse,
    summary="发送邮箱验证码",
    description="向邮箱发送 6 位验证码；同邮箱有发送间隔限制。登录前接口，无需 token。"
    "成功 head.status=0；邮箱非法或限流失败 status=100。",
)
def post_send_code(
    payload: SendCodeRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SendCodeResponse:
    email = _normalize_email(payload.body.email)
    if not _valid_email(email):
        log.warning("send_code invalid email")
        return send_code_error(ver=settings.server_ver, head_in=payload.head)

    now = datetime.now(timezone.utc)
    latest = (
        db.query(EmailCode)
        .filter(EmailCode.email == email)
        .order_by(EmailCode.created_at.desc())
        .first()
    )
    if latest is not None:
        created = latest.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if (now - created).total_seconds() < settings.send_code_interval_seconds:
            log.warning("send_code rate limited email=%s", email)
            return send_code_error(ver=settings.server_ver, head_in=payload.head)

    for row in (
        db.query(EmailCode)
        .filter(EmailCode.email == email, EmailCode.used_at.is_(None))
        .all()
    ):
        row.used_at = now

    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(
        EmailCode(
            email=email,
            code=code,
            expires_at=now + timedelta(seconds=settings.code_ttl_seconds),
            created_at=now,
        )
    )
    db.commit()

    try:
        send_verification_code(settings, email=email, code=code)
    except Exception:  # noqa: BLE001
        log.exception("send_code mail failed email=%s", email)
        return send_code_error(ver=settings.server_ver, head_in=payload.head)

    log.info("send_code ok email=%s", email)
    return send_code_ok(ver=settings.server_ver, head_in=payload.head)


@public_router.post(
    "/verify",
    response_model=VerifyResponse,
    summary="校验验证码并登录",
    description="校验邮箱验证码，成功返回 token、user_id、email、expires_at、needs_birthday。"
    "登录前接口，无需 token。验证码错误或过期：head.status=101。",
)
def post_verify(
    payload: VerifyRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> VerifyResponse:
    email = _normalize_email(payload.body.email)
    code = payload.body.code.strip()
    if not _valid_email(email) or not code.isdigit() or len(code) != 6:
        return verify_error(status=101, ver=settings.server_ver, head_in=payload.head)

    now = datetime.now(timezone.utc)
    row = (
        db.query(EmailCode)
        .filter(
            EmailCode.email == email,
            EmailCode.code == code,
            EmailCode.used_at.is_(None),
        )
        .order_by(EmailCode.created_at.desc())
        .first()
    )
    if row is None:
        log.warning("verify bad code email=%s", email)
        return verify_error(status=101, ver=settings.server_ver, head_in=payload.head)

    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= now:
        log.warning("verify expired code email=%s", email)
        return verify_error(status=101, ver=settings.server_ver, head_in=payload.head)

    row.used_at = now
    user = get_or_create_user(db, provider="email", subject=email)
    if not user.enabled:
        db.commit()
        log.warning("verify disabled user email=%s user_id=%s", email, user.user_id)
        return verify_error(status=101, ver=settings.server_ver, head_in=payload.head)
    db.query(UserToken).filter(UserToken.user_id == user.user_id).delete()
    token = secrets.token_urlsafe(32)
    token_expires = now + timedelta(days=settings.token_ttl_days)
    db.add(
        UserToken(
            token=token,
            user_id=user.user_id,
            created_at=now,
            expires_at=token_expires,
        )
    )
    db.commit()

    body = VerifyBodyOut(
        token=token,
        user_id=user.user_id,
        email=email,
        expires_at=token_expires.isoformat(),
        needs_birthday=needs_birthday(user),
    )
    log.info("verify ok email=%s user_id=%s", email, user.user_id)
    return verify_ok(body=body, ver=settings.server_ver, head_in=payload.head)


@public_router.post(
    "/google_login",
    response_model=GoogleLoginResponse,
    summary="Google 登录",
    description="登录前接口。body.id_token 为 Android Google Sign-In 的 ID Token；"
    "服务端校验 audience=GOOGLE_CLIENT_ID 后按 provider=google + sub 建/找用户并发会话。"
    "成功 body 与 /verify 同形（token、user_id、email、expires_at、needs_birthday）；"
    "校验失败或账号停用：status=101；未配置 GOOGLE_CLIENT_ID：status=100。",
)
def post_google_login(
    payload: GoogleLoginRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> GoogleLoginResponse:
    client_id = (settings.google_client_id or "").strip()
    if not client_id:
        log.warning("google_login client id not configured")
        return google_login_error(ver=settings.server_ver, head_in=payload.head)

    try:
        identity = verify_google_id_token(
            token=payload.body.id_token,
            client_id=client_id,
        )
    except ValueError:
        return google_login_error(status=101, ver=settings.server_ver, head_in=payload.head)

    now = datetime.now(timezone.utc)
    user = get_or_create_user(db, provider="google", subject=identity.subject)
    if not user.enabled:
        db.commit()
        log.warning(
            "google_login disabled user_id=%s sub=%s",
            user.user_id,
            identity.subject[:8],
        )
        return google_login_error(status=101, ver=settings.server_ver, head_in=payload.head)

    db.query(UserToken).filter(UserToken.user_id == user.user_id).delete()
    token = secrets.token_urlsafe(32)
    token_expires = now + timedelta(days=settings.token_ttl_days)
    db.add(
        UserToken(
            token=token,
            user_id=user.user_id,
            created_at=now,
            expires_at=token_expires,
        )
    )
    db.commit()

    body = VerifyBodyOut(
        token=token,
        user_id=user.user_id,
        email=identity.email,
        expires_at=token_expires.isoformat(),
        needs_birthday=needs_birthday(user),
    )
    log.info(
        "google_login ok user_id=%s sub=%s needs_birthday=%s",
        user.user_id,
        identity.subject[:8],
        body.needs_birthday,
    )
    return google_login_ok(body=body, ver=settings.server_ver, head_in=payload.head)


@public_router.post(
    "/video",
    response_model=VideoResponse,
    response_model_exclude_none=True,
    summary="拉取推荐视频列表",
    description="游客可访问（可不带 token）。教学片未看时置顶；否则 feed_weight 降序 + id 升序；"
    "登录用户已看沉底并可循环。"
    "成功 body.items[]（App v1.0：每 clip 的 interactions 带 on_success/on_miss；Story 可选 clip.on_end；无 transition）。"
    "单视频 video.length=1；Story 多段且入口 clip 在首位。池为空 status=100。",
)
def post_video(
    payload: VideoRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> VideoResponse:
    token = resolve_token(payload.head)
    user = resolve_current_user(request, db, token)
    limit = payload.body.limit
    video_ids = _next_video_ids(
        db,
        token=token,
        limit=limit,
        user_id=user.user_id if user else None,
    )
    if not video_ids:
        log.warning("video feed empty pool token=%s", token)
        return video_error(ver=settings.server_ver, head_in=payload.head)

    items: list[FeedItemOut] = []
    for vid in video_ids:
        row = db.get(PublishedVideo, vid)
        if row is None:
            log.warning("video feed skip missing id=%s", vid)
            continue
        item = _item_from_published(db, row)
        if item is not None:
            items.append(item)

    if not items:
        return video_error(ver=settings.server_ver, head_in=payload.head)

    body = VideoBodyOut(items=items)
    log.info("video feed ok token=%s items=%d", token, len(items))
    return video_ok(body=body, ver=settings.server_ver, head_in=payload.head)


@public_router.post(
    "/video_detail",
    response_model=VideoDetailResponse,
    response_model_exclude_none=True,
    summary="按 item_id 单拉发布单元",
    description="游客可访问（可不带 token）。body.video_id 语义为**发布单元 item_id**；"
    "成功 body.items 为单元素数组（与 /video 同形，App v1.0）。"
    "不存在或作者已停用：status=100。",
)
def post_video_detail(
    payload: VideoDetailRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> VideoDetailResponse:
    video_id = payload.body.video_id.strip()
    if not video_id:
        return video_detail_error(ver=settings.server_ver, head_in=payload.head)

    row = db.get(PublishedVideo, video_id)
    if row is None:
        log.warning("video_detail missing video_id=%s", video_id)
        return video_detail_error(ver=settings.server_ver, head_in=payload.head)

    if not is_author_visible(db, row.user_id):
        log.warning(
            "video_detail hidden disabled author video_id=%s user_id=%s",
            video_id,
            row.user_id,
        )
        return video_detail_error(ver=settings.server_ver, head_in=payload.head)

    item = _item_from_published(db, row)
    if item is None:
        return video_detail_error(ver=settings.server_ver, head_in=payload.head)

    log.info("video_detail ok video_id=%s", video_id)
    return video_detail_ok(
        body=VideoBodyOut(items=[item]),
        ver=settings.server_ver,
        head_in=payload.head,
    )


@auth_router.post(
    "/track",
    response_model=TrackResponse,
    summary="上报埋点",
    description="需登录。body 含 video_id 与 data 字符串。成功 status=0；"
    "参数非法或视频不存在 status=100；无效 token status=101。",
)
def post_track(
    payload: TrackRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TrackResponse:
    token = resolve_token(payload.head)
    video_id = payload.body.video_id.strip()
    data = payload.body.data.strip() if isinstance(payload.body.data, str) else ""
    if not video_id or not data:
        log.warning("track invalid body token=%s video_id=%s", token, video_id)
        return track_error(ver=settings.server_ver, head_in=payload.head)

    if db.get(PublishedVideo, video_id) is None:
        log.warning("track unknown video_id=%s token=%s", video_id, token)
        return track_error(ver=settings.server_ver, head_in=payload.head)

    db.add(AnalyticsLog(video_id=video_id, token=token, data=data))
    db.commit()
    log.info("track ok video_id=%s token=%s bytes=%d", video_id, token, len(data))
    return track_ok(ver=settings.server_ver, head_in=payload.head)


@auth_router.post(
    "/impression",
    response_model=ImpressionResponse,
    summary="上报曝光（沉底去重）",
    description="需登录。body.video_id 写入 Redis 去重池；Feed 将该片沉底但仍可循环。"
    "成功 status=0；视频不存在或 Redis 不可用 status=100；无效 token status=101。",
)
def post_impression(
    payload: ImpressionRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ImpressionResponse:
    token = resolve_token(payload.head)
    user = resolve_current_user(request, db, token)
    if user is None:
        # auth_router already requires login; defensive
        user = load_app_user(db, token)
    if user is None:
        return impression_error(
            status=101, ver=settings.server_ver, head_in=payload.head
        )

    video_id = payload.body.video_id.strip()
    if not video_id or db.get(PublishedVideo, video_id) is None:
        log.warning(
            "impression invalid video_id=%s user_id=%s",
            video_id,
            user.user_id,
        )
        return impression_error(ver=settings.server_ver, head_in=payload.head)

    try:
        get_impression_store().mark_seen(user_id=user.user_id, video_id=video_id)
    except ImpressionUnavailableError:
        log.warning("impression redis unavailable user_id=%s", user.user_id)
        return impression_error(ver=settings.server_ver, head_in=payload.head)

    log.info("impression ok user_id=%s video_id=%s", user.user_id, video_id)
    return impression_ok(ver=settings.server_ver, head_in=payload.head)
