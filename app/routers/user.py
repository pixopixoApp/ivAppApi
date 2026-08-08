from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy.orm import Session

from app.auth_user import load_app_user, require_app_user, resolve_current_user
from app.avatar_storage import AvatarStorageError, save_user_avatar
from app.config import Settings, get_settings
from app.db import get_db
from app.logging_config import get_logger
from app.models import EmailCode, Follow, User, UserToken
from app.protocol_envelope import (
    avatar_error,
    avatar_ok,
    birthday_error,
    birthday_ok,
    deactivate_error,
    deactivate_ok,
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
from app.routers.feed import list_published_items
from app.schemas import (
    AvatarResponse,
    BirthdayBodyOut,
    BirthdayRequest,
    BirthdayResponse,
    DeactivateBodyOut,
    DeactivateRequest,
    DeactivateResponse,
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
    assert_min_age,
    follow_counts,
    is_following,
    needs_birthday,
    normalize_birthday,
    to_profile_fields,
)

auth_router = APIRouter(tags=["user"], dependencies=[Depends(require_app_user)])
# multipart 上传：token 走 form，不能挂 require_app_user（其读 JSON body）
upload_router = APIRouter(tags=["user"])
log = get_logger(__name__)

_FOLLOWEE_FEED_CAP = 500


def _profile_body(db: Session, user: User) -> ProfileBodyOut:
    fields = to_profile_fields(user)
    following_count, follower_count = follow_counts(db, user.user_id)
    return ProfileBodyOut(
        user_id=str(fields["user_id"]),
        nickname=str(fields["nickname"]),
        avatar_url=str(fields["avatar_url"]),
        email=str(fields["email"]),
        enabled=bool(fields["enabled"]),
        following_count=following_count,
        follower_count=follower_count,
    )


def _public_profile_body(
    db: Session, user: User, *, viewer_user_id: str
) -> PublicProfileBodyOut:
    following_count, follower_count = follow_counts(db, user.user_id)
    return PublicProfileBodyOut(
        user_id=user.user_id,
        nickname=user.nickname or "",
        avatar_url=user.avatar_url or "",
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
    return user


def _follow_list_items(
    db: Session,
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
                avatar_url=(peer.avatar_url if peer is not None else "") or "",
                created_at=row.created_at.isoformat() if row.created_at else "",
            )
        )
    return items


@upload_router.post(
    "/avatar",
    response_model=AvatarResponse,
    summary="上传当前用户头像",
    description="multipart：`token`（登录凭证）+ `file`（jpg/png/webp，最大 2MB）。"
    "落盘到 MEDIA_ROOT/avatars，更新 avatar_url 为相对路径 `/media/avatars/{user_id}.{ext}`。"
    "成功 body 与 profile 同形；非法文件 status=100；无效 token status=101。",
)
async def post_avatar(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    token: Annotated[str, Form(description="登录 token")],
    file: UploadFile = File(..., description="头像图片文件"),
) -> AvatarResponse:
    head = ProtocolHeadIn(act="avatar", token=token.strip())
    me = load_app_user(db, token.strip())
    if me is None:
        return avatar_error(status=101, ver=settings.server_ver, head_in=head)

    try:
        relative = await save_user_avatar(
            settings,
            user_id=me.user_id,
            file=file,
        )
        user = apply_user_update(
            db,
            user_id=me.user_id,
            avatar_url=relative,
            create_if_missing=False,
        )
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
        body=_profile_body(db, user),
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
        body=_profile_body(db, user),
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
        body=_profile_body(db, user),
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

    log.info("user_profile ok viewer=%s target=%s", me.user_id, user.user_id)
    return user_profile_ok(
        body=_public_profile_body(db, user, viewer_user_id=me.user_id),
        ver=settings.server_ver,
        head_in=payload.head,
    )


@auth_router.post(
    "/birthday",
    response_model=BirthdayResponse,
    summary="保存生日（年龄门）",
    description="需有效 head.token。body.birthday 为 YYYY-MM-DD；须满 13 岁（UTC）。"
    "通过：写库并回 passed=true；未满 13：status=100、passed=false、不写库；"
    "非法日期 status=100；无效 token status=101。",
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
        assert_min_age(normalized)
        user.birthday = normalized
        db.commit()
        db.refresh(user)
    except ValueError as exc:
        db.rollback()
        msg = str(exc)
        if msg == "age below minimum":
            log.info("birthday age gate fail user_id=%s", me.user_id)
            return birthday_error(
                ver=settings.server_ver,
                head_in=payload.head,
                body=BirthdayBodyOut(
                    birthday="",
                    needs_birthday=True,
                    passed=False,
                ),
            )
        return birthday_error(ver=settings.server_ver, head_in=payload.head)

    log.info("birthday ok user_id=%s birthday=%s", user.user_id, user.birthday)
    return birthday_ok(
        body=BirthdayBodyOut(
            birthday=user.birthday,
            needs_birthday=needs_birthday(user),
            passed=True,
        ),
        ver=settings.server_ver,
        head_in=payload.head,
    )


@auth_router.post(
    "/deactivate",
    response_model=DeactivateResponse,
    summary="申请删除账号（验证码 + 软删）",
    description="需有效 head.token。body.code 为邮箱验证码（先 /send_code）。"
    "仅邮箱账号；通过后 enabled=false、清 token，并写 scheduled_delete_at（默认 +30 天）。"
    "成功回 scheduled_delete_at；验证码错误/非邮箱 status=100；无效 token status=101。",
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
        log.warning("deactivate bad code user_id=%s", me.user_id)
        return deactivate_error(ver=settings.server_ver, head_in=payload.head)
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= now:
        log.warning("deactivate expired code user_id=%s", me.user_id)
        return deactivate_error(ver=settings.server_ver, head_in=payload.head)

    row.used_at = now
    scheduled = now + timedelta(days=settings.account_deletion_buffer_days)
    try:
        apply_user_update(
            db,
            user_id=me.user_id,
            enabled=False,
            create_if_missing=False,
        )
        user.deletion_requested_at = now
        user.scheduled_delete_at = scheduled
        db.query(UserToken).filter(UserToken.user_id == me.user_id).delete()
        db.commit()
    except LookupError:
        db.rollback()
        return deactivate_error(
            status=101, ver=settings.server_ver, head_in=payload.head
        )

    log.info(
        "deactivate ok user_id=%s scheduled_delete_at=%s",
        me.user_id,
        scheduled.isoformat(),
    )
    return deactivate_ok(
        body=DeactivateBodyOut(scheduled_delete_at=scheduled.isoformat()),
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
    if db.get(User, followee_id) is None:
        log.warning("follow unknown user_id=%s by=%s", followee_id, me.user_id)
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

    rows = (
        db.query(Follow)
        .filter(Follow.follower_user_id == target.user_id)
        .order_by(Follow.created_at.desc())
        .limit(payload.body.limit)
        .all()
    )
    items = _follow_list_items(db, rows, peer_attr="followee_user_id")
    log.info(
        "following ok viewer=%s target=%s count=%d",
        me.user_id,
        target.user_id,
        len(items),
    )
    return following_ok(
        body=FollowingBodyOut(items=items),
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

    rows = (
        db.query(Follow)
        .filter(Follow.followee_user_id == target.user_id)
        .order_by(Follow.created_at.desc())
        .limit(payload.body.limit)
        .all()
    )
    items = _follow_list_items(db, rows, peer_attr="follower_user_id")
    log.info(
        "followers ok viewer=%s target=%s count=%d",
        me.user_id,
        target.user_id,
        len(items),
    )
    return followers_ok(
        body=FollowersBodyOut(items=items),
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

    items = list_published_items(
        db, author_user_ids=[author.user_id], limit=payload.body.limit
    )
    log.info(
        "user_videos ok viewer=%s author=%s count=%d",
        me.user_id,
        author.user_id,
        len(items),
    )
    return user_videos_ok(
        body=VideoBodyOut(items=items),
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

    items = list_published_items(
        db, author_user_ids=[me.user_id], limit=payload.body.limit
    )
    log.info("my_videos ok user_id=%s count=%d", me.user_id, len(items))
    return my_videos_ok(
        body=VideoBodyOut(items=items),
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
    items = list_published_items(
        db, author_user_ids=followee_ids, limit=payload.body.limit
    )
    log.info(
        "following_feed ok user_id=%s followees=%d items=%d",
        me.user_id,
        len(followee_ids),
        len(items),
    )
    return following_feed_ok(
        body=VideoBodyOut(items=items),
        ver=settings.server_ver,
        head_in=payload.head,
    )
