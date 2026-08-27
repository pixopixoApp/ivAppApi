from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.account_deletion import AccountDeletionUnavailable, delete_account_data
from app.auth_user import (
    load_app_user,
    require_app_user,
    resolve_current_user,
    resolve_multipart_token,
)
from app.avatar_storage import AvatarStorageError, store_user_avatar
from app.cdn_cache import enqueue_prefetch
from app.config import Settings, get_settings
from app.db import get_db
from app.logging_config import get_logger
from app.models import Follow, User
from app.pagination import (
    CursorError,
    datetime_from_cursor_value,
    datetime_to_cursor_value,
    decode_cursor,
    encode_cursor,
)
from app.protocol_envelope import (
    avatar_error,
    avatar_ok,
    birthday_error,
    birthday_ok,
    deactivate_error,
    deactivate_ok,
    deactivate_send_code_error,
    deactivate_send_code_ok,
    follow_error,
    follow_ok,
    followers_error,
    followers_ok,
    following_error,
    following_feed_error,
    following_feed_ok,
    following_ok,
    my_videos_error,
    my_videos_ok,
    profile_error,
    profile_ok,
    profile_update_error,
    profile_update_ok,
    resolve_token,
    unfollow_error,
    unfollow_ok,
    user_profile_error,
    user_profile_ok,
    user_videos_error,
    user_videos_ok,
)
from app.protocol_video import normalize_client_runtime_spec_versions
from app.public_origin import canonicalize_public_url
from app.routers.feed import list_published_items
from app.safety import users_blocked_between
from app.schemas import (
    AvatarResponse,
    BirthdayBodyOut,
    BirthdayRequest,
    BirthdayResponse,
    DeactivateBodyOut,
    DeactivateRequest,
    DeactivateResponse,
    DeactivateSendCodeRequest,
    DeactivateSendCodeResponse,
    FollowersBodyOut,
    FollowersRequest,
    FollowersResponse,
    FollowingBodyOut,
    FollowingFeedRequest,
    FollowingFeedResponse,
    FollowingItemOut,
    FollowingRequest,
    FollowingResponse,
    FollowRequest,
    FollowResponse,
    MyVideosRequest,
    MyVideosResponse,
    ProfileBodyOut,
    ProfileRequest,
    ProfileResponse,
    ProfileUpdateRequest,
    ProfileUpdateResponse,
    ProtocolHeadIn,
    PublicProfileBodyOut,
    UnfollowRequest,
    UnfollowResponse,
    UserProfileRequest,
    UserProfileResponse,
    UserVideosRequest,
    UserVideosResponse,
    VideoBodyOut,
)
from app.users import (
    apply_user_update,
    follow_counts,
    is_following,
    is_under_13,
    needs_birthday,
    normalize_birthday,
    to_profile_fields,
)
from app.verification_codes import (
    PURPOSE_DEACTIVATE,
    find_valid_code,
    issue_email_code,
)

auth_router = APIRouter(tags=["user"], dependencies=[Depends(require_app_user)])
# multipart 上传：token 走 form，不能挂 require_app_user（其读 JSON body）
upload_router = APIRouter(tags=["user"])
log = get_logger(__name__)

_FOLLOWEE_FEED_CAP = 500


def _profile_body(db: Session, settings: Settings, user: User) -> ProfileBodyOut:
    fields = to_profile_fields(user)
    following_count, follower_count = follow_counts(db, user.user_id)
    return ProfileBodyOut(
        user_id=str(fields["user_id"]),
        nickname=str(fields["nickname"]),
        avatar_url=canonicalize_public_url(settings, str(fields["avatar_url"])) or "",
        bio=str(fields["bio"]),
        email=str(fields["email"]),
        enabled=bool(fields["enabled"]),
        following_count=following_count,
        follower_count=follower_count,
    )


def _public_profile_body(
    db: Session, settings: Settings, user: User, *, viewer_user_id: str
) -> PublicProfileBodyOut:
    following_count, follower_count = follow_counts(db, user.user_id)
    return PublicProfileBodyOut(
        user_id=user.user_id,
        nickname=user.nickname or "",
        avatar_url=canonicalize_public_url(settings, user.avatar_url) or "",
        bio=user.bio or "",
        enabled=bool(user.enabled),
        following_count=following_count,
        follower_count=follower_count,
        is_following=is_following(
            db, follower_user_id=viewer_user_id, followee_user_id=user.user_id
        ),
    )


def _resolve_list_target(
    db: Session, *, me_user_id: str, raw_user_id: str | None
) -> User | None:
    uid = (raw_user_id or "").strip() or me_user_id
    user = db.get(User, uid)
    if user is None or not user.enabled:
        return None
    if uid != me_user_id and users_blocked_between(db, me_user_id, uid):
        return None
    return user


def _follow_list_items(
    db: Session,
    settings: Settings,
    rows: list[Follow],
    *,
    peer_attr: str,
) -> list[FollowingItemOut]:
    peer_ids = [getattr(row, peer_attr) for row in rows]
    by_id: dict[str, User] = {}
    if peer_ids:
        for u in db.query(User).filter(User.user_id.in_(peer_ids)).all():
            by_id[u.user_id] = u
    items: list[FollowingItemOut] = []
    for row in rows:
        peer_id = getattr(row, peer_attr)
        peer = by_id.get(peer_id)
        items.append(
            FollowingItemOut(
                user_id=peer_id,
                nickname=(peer.nickname if peer is not None else "") or "",
                avatar_url=(
                    canonicalize_public_url(settings, peer.avatar_url)
                    if peer is not None
                    else ""
                )
                or "",
                created_at=row.created_at.isoformat() if row.created_at else "",
            )
        )
    return items


def _follow_page(
    db: Session,
    *,
    filter_column,
    target_user_id: str,
    cursor: str | None,
    cursor_kind: str,
    cursor_secret: str,
    limit: int,
) -> tuple[list[Follow], str | None, bool]:
    query = db.query(Follow).filter(filter_column == target_user_id)
    if cursor:
        values = decode_cursor(cursor=cursor, kind=cursor_kind, secret=cursor_secret)
        created_at = datetime_from_cursor_value(values.get("created_at"))
        row_id = values.get("id")
        if isinstance(row_id, bool) or not isinstance(row_id, int) or row_id < 1:
            raise CursorError("cursor relation id missing")
        query = query.filter(
            or_(
                Follow.created_at < created_at,
                and_(Follow.created_at == created_at, Follow.id < row_id),
            )
        )
    rows = (
        query.order_by(Follow.created_at.desc(), Follow.id.desc())
        .limit(limit + 1)
        .all()
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(
            kind=cursor_kind,
            values={
                "created_at": datetime_to_cursor_value(last.created_at),
                "id": last.id,
            },
            secret=cursor_secret,
        )
    return page, next_cursor, has_more


@upload_router.post(
    "/avatar",
    response_model=AvatarResponse,
    summary="上传当前用户头像",
    description="multipart：`token`（登录凭证）+ `file`（jpg/png/webp，最大 2MB）。"
    "落盘到 MEDIA_ROOT/avatars，更新 avatar_url 为相对路径 `/media/avatars/{user_id}.{ext}`。"
    "成功 body 与 profile 同形；非法文件 status=100；无效 token status=101。",
)
async def post_avatar(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    file: Annotated[UploadFile, File(description="头像图片文件")],
    token: Annotated[
        str | None,
        Form(description="兼容旧客户端的登录 token；新客户端可使用 Authorization: Bearer"),
    ] = None,
) -> AvatarResponse:
    effective_token = resolve_multipart_token(
        request,
        form_token=token,
        act="avatar",
    )
    head = ProtocolHeadIn(act="avatar", token=effective_token)
    me = load_app_user(db, effective_token)
    if me is None:
        return avatar_error(status=101, ver=settings.server_ver, head_in=head)

    try:
        raw = await file.read()
        relative, media_object_id = store_user_avatar(
            db,
            settings,
            user_id=me.user_id,
            raw=raw,
            filename=file.filename,
            content_type=file.content_type,
        )
        user = db.get(User, me.user_id)
        if user is None:
            raise LookupError(me.user_id)
        user.avatar_url = relative
        user.avatar_media_object_id = media_object_id
        db.add(user)
        enqueue_prefetch(db, settings, [relative])
        db.commit()
        db.refresh(user)
    except AvatarStorageError:
        db.rollback()
        return avatar_error(ver=settings.server_ver, head_in=head)
    except LookupError:
        db.rollback()
        return avatar_error(status=101, ver=settings.server_ver, head_in=head)
    except ValueError:
        db.rollback()
        return avatar_error(ver=settings.server_ver, head_in=head)

    log.info("avatar ok user_id=%s avatar_url=%s", user.user_id, user.avatar_url)
    return avatar_ok(
        body=_profile_body(db, settings, user),
        ver=settings.server_ver,
        head_in=head,
    )


@auth_router.post(
    "/profile",
    response_model=ProfileResponse,
    summary="查询当前用户资料",
    description="需有效 head.token。返回 user_id、nickname、avatar_url、email、enabled、"
    "following_count、follower_count。无效 token：status=101。",
)
def post_profile(
    request: Request,
    payload: ProfileRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ProfileResponse:
    me = resolve_current_user(request, db, resolve_token(payload.head))
    if me is None:
        return profile_error(status=101, ver=settings.server_ver, head_in=payload.head)

    user = db.get(User, me.user_id)
    if user is None:
        return profile_error(status=101, ver=settings.server_ver, head_in=payload.head)

    log.info("profile ok user_id=%s", user.user_id)
    return profile_ok(
        body=_profile_body(db, settings, user),
        ver=settings.server_ver,
        head_in=payload.head,
    )


@auth_router.post(
    "/profile_update",
    response_model=ProfileUpdateResponse,
    summary="更新昵称与头像",
    description="需有效 head.token。仅可改 nickname、avatar_url（相对路径，以 / 开头）。"
    "与管理端共用写入内核；不可改身份与 enabled。非法参数 status=100；无效 token status=101。",
)
def post_profile_update(
    request: Request,
    payload: ProfileUpdateRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ProfileUpdateResponse:
    me = resolve_current_user(request, db, resolve_token(payload.head))
    if me is None:
        return profile_update_error(
            status=101, ver=settings.server_ver, head_in=payload.head
        )

    try:
        user = apply_user_update(
            db,
            user_id=me.user_id,
            nickname=payload.body.nickname,
            avatar_url=payload.body.avatar_url,
            bio=payload.body.bio,
            create_if_missing=False,
        )
        db.commit()
        db.refresh(user)
    except LookupError:
        db.rollback()
        return profile_update_error(
            status=101, ver=settings.server_ver, head_in=payload.head
        )
    except ValueError:
        db.rollback()
        return profile_update_error(ver=settings.server_ver, head_in=payload.head)

    log.info(
        "profile_update ok user_id=%s nickname=%s avatar_url=%s",
        user.user_id,
        user.nickname,
        user.avatar_url,
    )
    return profile_update_ok(
        body=_profile_body(db, settings, user),
        ver=settings.server_ver,
        head_in=payload.head,
    )


@auth_router.post(
    "/user_profile",
    response_model=UserProfileResponse,
    summary="查询他人公开资料",
    description="需有效 head.token。body.user_id 为目标用户。"
    "成功返回公开字段（不含邮箱）；用户不存在或已停用 status=100；无效 token status=101。",
)
def post_user_profile(
    request: Request,
    payload: UserProfileRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UserProfileResponse:
    me = resolve_current_user(request, db, resolve_token(payload.head))
    if me is None:
        return user_profile_error(
            status=101, ver=settings.server_ver, head_in=payload.head
        )

    uid = payload.body.user_id.strip()
    user = db.get(User, uid) if uid else None
    if user is None or not user.enabled:
        return user_profile_error(ver=settings.server_ver, head_in=payload.head)
    if user.user_id != me.user_id and users_blocked_between(db, me.user_id, user.user_id):
        return user_profile_error(ver=settings.server_ver, head_in=payload.head)

    log.info("user_profile ok viewer=%s target=%s", me.user_id, user.user_id)
    return user_profile_ok(
        body=_public_profile_body(db, settings, user, viewer_user_id=me.user_id),
        ver=settings.server_ver,
        head_in=payload.head,
    )


@auth_router.post(
    "/birthday",
    response_model=BirthdayResponse,
    summary="保存生日（年龄门）",
    description="需有效 head.token。body.birthday 为 YYYY-MM-DD。"
    "合法日期一律持久化并返回 status=0；passed 表示按 UTC 是否已满 13 岁，"
    "未满 13 岁时 passed=false。非法日期 status=100；无效 token status=101。",
)
def post_birthday(
    request: Request,
    payload: BirthdayRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BirthdayResponse:
    me = resolve_current_user(request, db, resolve_token(payload.head))
    if me is None:
        return birthday_error(status=101, ver=settings.server_ver, head_in=payload.head)

    user = db.get(User, me.user_id)
    if user is None:
        return birthday_error(status=101, ver=settings.server_ver, head_in=payload.head)

    try:
        normalized = normalize_birthday(payload.body.birthday)
        user.birthday = normalized
        db.commit()
        db.refresh(user)
    except ValueError:
        db.rollback()
        return birthday_error(ver=settings.server_ver, head_in=payload.head)

    under_13 = bool(is_under_13(user))
    log.info("birthday ok user_id=%s birthday=%s", user.user_id, user.birthday)
    return birthday_ok(
        body=BirthdayBodyOut(
            birthday=user.birthday,
            needs_birthday=needs_birthday(user),
            passed=not under_13,
        ),
        ver=settings.server_ver,
        head_in=payload.head,
    )


@auth_router.post(
    "/deactivate/send_code",
    response_model=DeactivateSendCodeResponse,
    summary="发送账号注销验证码",
    description="需登录。验证码用途与登录完全隔离，不共享频控或校验范围。",
)
def post_deactivate_send_code(
    request: Request,
    payload: DeactivateSendCodeRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DeactivateSendCodeResponse:
    me = resolve_current_user(request, db, resolve_token(payload.head))
    if me is None:
        return deactivate_send_code_error(
            status=101,
            ver=settings.server_ver,
            head_in=payload.head,
            error_code="AUTH_REQUIRED",
            message="Please sign in again.",
        )
    user = db.get(User, me.user_id)
    if user is None or user.provider != "email" or not user.subject.strip():
        return deactivate_send_code_error(
            ver=settings.server_ver,
            head_in=payload.head,
            error_code="EMAIL_ACCOUNT_REQUIRED",
            message="This account doesn’t use email sign-in.",
        )

    result = issue_email_code(
        db,
        settings,
        email=user.subject.strip().lower(),
        purpose=PURPOSE_DEACTIVATE,
    )
    if result.ok:
        return deactivate_send_code_ok(ver=settings.server_ver, head_in=payload.head)
    if result.error_code == "CODE_RATE_LIMITED":
        return deactivate_send_code_error(
            ver=settings.server_ver,
            head_in=payload.head,
            error_code=result.error_code,
            message="A code was just sent. Please wait a moment and try again.",
            retry_after_seconds=result.retry_after_seconds,
        )
    return deactivate_send_code_error(
        ver=settings.server_ver,
        head_in=payload.head,
        error_code=result.error_code,
        message="We couldn’t send the email. Please try again shortly.",
    )


@auth_router.post(
    "/deactivate",
    response_model=DeactivateResponse,
    summary="立即删除账号（兼容旧客户端）",
    description="需有效 head.token。邮箱账号验证码通过后立即永久删除账号及关联数据。"
    "Google 账号请使用新版 DELETE /api/v1/account。",
)
def post_deactivate(
    request: Request,
    payload: DeactivateRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DeactivateResponse:
    me = resolve_current_user(request, db, resolve_token(payload.head))
    if me is None:
        return deactivate_error(
            status=101, ver=settings.server_ver, head_in=payload.head
        )

    user = db.get(User, me.user_id)
    if user is None:
        return deactivate_error(
            status=101, ver=settings.server_ver, head_in=payload.head
        )
    if user.provider != "email" or not (user.subject or "").strip():
        log.warning("deactivate non-email user_id=%s provider=%s", me.user_id, user.provider)
        return deactivate_error(ver=settings.server_ver, head_in=payload.head)

    email = user.subject.strip().lower()
    code = payload.body.code.strip()
    if not code.isdigit() or len(code) != 6:
        return deactivate_error(ver=settings.server_ver, head_in=payload.head)

    now = datetime.now(timezone.utc)
    row = find_valid_code(
        db,
        email=email,
        code=code,
        purpose=PURPOSE_DEACTIVATE,
        now=now,
    )
    if row is None:
        log.warning("deactivate bad code user_id=%s", me.user_id)
        return deactivate_error(ver=settings.server_ver, head_in=payload.head)
    row.used_at = now
    try:
        delete_account_data(db, settings, user_id=me.user_id)
    except AccountDeletionUnavailable:
        db.rollback()
        return deactivate_error(
            ver=settings.server_ver, head_in=payload.head
        )

    log.info("deactivate immediate-delete ok user_id=%s", me.user_id)
    return deactivate_ok(
        body=DeactivateBodyOut(scheduled_delete_at=now.isoformat()),
        ver=settings.server_ver,
        head_in=payload.head,
    )


@auth_router.post(
    "/follow",
    response_model=FollowResponse,
    summary="关注用户",
    description="需有效 head.token。body.user_id 为对方。已关注则幂等成功。"
    "不能关注自己或对方不存在：status=100；无效 token：status=101。",
)
def post_follow(
    request: Request,
    payload: FollowRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FollowResponse:
    me = resolve_current_user(request, db, resolve_token(payload.head))
    if me is None:
        return follow_error(status=101, ver=settings.server_ver, head_in=payload.head)

    followee_id = payload.body.user_id.strip()
    if not followee_id or followee_id == me.user_id:
        return follow_error(ver=settings.server_ver, head_in=payload.head)
    followee = db.get(User, followee_id)
    if followee is None or not followee.enabled:
        log.warning("follow unknown user_id=%s by=%s", followee_id, me.user_id)
        return follow_error(ver=settings.server_ver, head_in=payload.head)
    if users_blocked_between(db, me.user_id, followee_id):
        return follow_error(ver=settings.server_ver, head_in=payload.head)

    exists = (
        db.query(Follow)
        .filter(
            Follow.follower_user_id == me.user_id,
            Follow.followee_user_id == followee_id,
        )
        .one_or_none()
    )
    if exists is None:
        db.add(
            Follow(
                follower_user_id=me.user_id,
                followee_user_id=followee_id,
            )
        )
        db.commit()
    log.info("follow ok follower=%s followee=%s", me.user_id, followee_id)
    return follow_ok(ver=settings.server_ver, head_in=payload.head)


@auth_router.post(
    "/unfollow",
    response_model=UnfollowResponse,
    summary="取消关注",
    description="需有效 head.token。body.user_id 为对方。未关注则幂等成功。"
    "无效 token：status=101。",
)
def post_unfollow(
    request: Request,
    payload: UnfollowRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UnfollowResponse:
    me = resolve_current_user(request, db, resolve_token(payload.head))
    if me is None:
        return unfollow_error(status=101, ver=settings.server_ver, head_in=payload.head)

    followee_id = payload.body.user_id.strip()
    if not followee_id:
        return unfollow_error(ver=settings.server_ver, head_in=payload.head)

    db.query(Follow).filter(
        Follow.follower_user_id == me.user_id,
        Follow.followee_user_id == followee_id,
    ).delete()
    db.commit()
    log.info("unfollow ok follower=%s followee=%s", me.user_id, followee_id)
    return unfollow_ok(ver=settings.server_ver, head_in=payload.head)


@auth_router.post(
    "/following",
    response_model=FollowingResponse,
    summary="关注列表",
    description="需有效 head.token。可选 body.user_id（空=自己）、body.limit（默认 50）。"
    "成功 body.items[{ user_id, nickname, avatar_url, created_at }]；"
    "目标不存在/停用 status=100；无效 token：status=101。",
)
def post_following(
    request: Request,
    payload: FollowingRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FollowingResponse:
    me = resolve_current_user(request, db, resolve_token(payload.head))
    if me is None:
        return following_error(status=101, ver=settings.server_ver, head_in=payload.head)

    target = _resolve_list_target(db, me_user_id=me.user_id, raw_user_id=payload.body.user_id)
    if target is None:
        return following_error(ver=settings.server_ver, head_in=payload.head)

    try:
        rows, next_cursor, has_more = _follow_page(
            db,
            filter_column=Follow.follower_user_id,
            target_user_id=target.user_id,
            cursor=payload.body.cursor,
            cursor_kind=f"following:{target.user_id}",
            cursor_secret=settings.cursor_secret or settings.publish_key,
            limit=payload.body.limit,
        )
    except CursorError:
        return following_error(ver=settings.server_ver, head_in=payload.head)
    items = _follow_list_items(db, settings, rows, peer_attr="followee_user_id")
    log.info(
        "following ok viewer=%s target=%s count=%d",
        me.user_id,
        target.user_id,
        len(items),
    )
    return following_ok(
        body=FollowingBodyOut(
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
        ),
        ver=settings.server_ver,
        head_in=payload.head,
    )


@auth_router.post(
    "/followers",
    response_model=FollowersResponse,
    summary="粉丝列表",
    description="需有效 head.token。可选 body.user_id（空=自己）、body.limit（默认 50）。"
    "成功 body.items[{ user_id, nickname, avatar_url, created_at }]；"
    "目标不存在/停用 status=100；无效 token：status=101。",
)
def post_followers(
    request: Request,
    payload: FollowersRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FollowersResponse:
    me = resolve_current_user(request, db, resolve_token(payload.head))
    if me is None:
        return followers_error(status=101, ver=settings.server_ver, head_in=payload.head)

    target = _resolve_list_target(db, me_user_id=me.user_id, raw_user_id=payload.body.user_id)
    if target is None:
        return followers_error(ver=settings.server_ver, head_in=payload.head)

    try:
        rows, next_cursor, has_more = _follow_page(
            db,
            filter_column=Follow.followee_user_id,
            target_user_id=target.user_id,
            cursor=payload.body.cursor,
            cursor_kind=f"followers:{target.user_id}",
            cursor_secret=settings.cursor_secret or settings.publish_key,
            limit=payload.body.limit,
        )
    except CursorError:
        return followers_error(ver=settings.server_ver, head_in=payload.head)
    items = _follow_list_items(db, settings, rows, peer_attr="follower_user_id")
    log.info(
        "followers ok viewer=%s target=%s count=%d",
        me.user_id,
        target.user_id,
        len(items),
    )
    return followers_ok(
        body=FollowersBodyOut(
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
        ),
        ver=settings.server_ver,
        head_in=payload.head,
    )


@auth_router.post(
    "/user_videos",
    response_model=UserVideosResponse,
    response_model_exclude_none=True,
    summary="按作者查询公开作品",
    description="需有效 head.token。body.user_id + 可选 limit。"
    "成功 body.items 与 /video 同形；作者不存在或已停用 status=100。",
)
def post_user_videos(
    request: Request,
    payload: UserVideosRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UserVideosResponse:
    me = resolve_current_user(request, db, resolve_token(payload.head))
    if me is None:
        return user_videos_error(
            status=101, ver=settings.server_ver, head_in=payload.head
        )

    uid = payload.body.user_id.strip()
    author = db.get(User, uid) if uid else None
    if author is None or not author.enabled:
        return user_videos_error(ver=settings.server_ver, head_in=payload.head)
    if author.user_id != me.user_id and users_blocked_between(db, me.user_id, author.user_id):
        return user_videos_error(ver=settings.server_ver, head_in=payload.head)

    try:
        page = list_published_items(
            db,
            settings=settings,
            author_user_ids=[author.user_id],
            viewer_user_id=me.user_id,
            limit=payload.body.limit,
            cursor=payload.body.cursor,
            cursor_kind=f"user_videos:{author.user_id}",
            cursor_secret=settings.cursor_secret or settings.publish_key,
            public_share_base_url=settings.public_share_base_url,
            supported_runtime_spec_versions=normalize_client_runtime_spec_versions(
                payload.body.supported_experience_spec_versions
            ),
        )
    except CursorError:
        return user_videos_error(ver=settings.server_ver, head_in=payload.head)
    log.info(
        "user_videos ok viewer=%s author=%s count=%d",
        me.user_id,
        author.user_id,
        len(page.items),
    )
    return user_videos_ok(
        body=VideoBodyOut(
            items=page.items,
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        ),
        ver=settings.server_ver,
        head_in=payload.head,
    )


@auth_router.post(
    "/my_videos",
    response_model=MyVideosResponse,
    response_model_exclude_none=True,
    summary="当前用户作品列表",
    description="需有效 head.token。可选 body.limit。成功 body.items 与 /video 同形。",
)
def post_my_videos(
    request: Request,
    payload: MyVideosRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MyVideosResponse:
    me = resolve_current_user(request, db, resolve_token(payload.head))
    if me is None:
        return my_videos_error(status=101, ver=settings.server_ver, head_in=payload.head)

    try:
        page = list_published_items(
            db,
            settings=settings,
            author_user_ids=[me.user_id],
            viewer_user_id=me.user_id,
            limit=payload.body.limit,
            cursor=payload.body.cursor,
            cursor_kind=f"my_videos:{me.user_id}",
            cursor_secret=settings.cursor_secret or settings.publish_key,
            public_share_base_url=settings.public_share_base_url,
            supported_runtime_spec_versions=normalize_client_runtime_spec_versions(
                payload.body.supported_experience_spec_versions
            ),
        )
    except CursorError:
        return my_videos_error(ver=settings.server_ver, head_in=payload.head)
    log.info("my_videos ok user_id=%s count=%d", me.user_id, len(page.items))
    return my_videos_ok(
        body=VideoBodyOut(
            items=page.items,
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        ),
        ver=settings.server_ver,
        head_in=payload.head,
    )


@auth_router.post(
    "/following_feed",
    response_model=FollowingFeedResponse,
    response_model_exclude_none=True,
    summary="关注作者作品流",
    description="需有效 head.token。可选 body.limit。"
    "返回我所关注作者的公开作品（按发布时间倒序）；无关注则空列表。",
)
def post_following_feed(
    request: Request,
    payload: FollowingFeedRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FollowingFeedResponse:
    me = resolve_current_user(request, db, resolve_token(payload.head))
    if me is None:
        return following_feed_error(
            status=101, ver=settings.server_ver, head_in=payload.head
        )

    followee_ids = [
        row.followee_user_id
        for row in (
            db.query(Follow)
            .filter(Follow.follower_user_id == me.user_id)
            .order_by(Follow.created_at.desc())
            .limit(_FOLLOWEE_FEED_CAP)
            .all()
        )
    ]
    try:
        page = list_published_items(
            db,
            settings=settings,
            author_user_ids=followee_ids,
            viewer_user_id=me.user_id,
            limit=payload.body.limit,
            cursor=payload.body.cursor,
            cursor_kind=f"following_feed:{me.user_id}",
            cursor_secret=settings.cursor_secret or settings.publish_key,
            public_share_base_url=settings.public_share_base_url,
            supported_runtime_spec_versions=normalize_client_runtime_spec_versions(
                payload.body.supported_experience_spec_versions
            ),
        )
    except CursorError:
        return following_feed_error(ver=settings.server_ver, head_in=payload.head)
    log.info(
        "following_feed ok user_id=%s followees=%d items=%d",
        me.user_id,
        len(followee_ids),
        len(page.items),
    )
    return following_feed_ok(
        body=VideoBodyOut(
            items=page.items,
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        ),
        ver=settings.server_ver,
        head_in=payload.head,
    )
