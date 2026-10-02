import base64
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth_user import (
    AppUser,
    bearer_token_from_request,
    load_app_user,
    require_bearer_user,
)
from app.config import Settings, get_settings
from app.db import get_db
from app.deps import require_publish_key
from app.html_content import (
    CONTENT_TYPE_HTML,
    CONTENT_TYPE_RUNTIME,
    HTML_BRIDGE_VERSION,
)
from app.models import (
    Comment,
    CommentLike,
    ContentReport,
    Follow,
    PublishedVideo,
    PublishedVideoSeo,
    SocialNotification,
    SocialRateEvent,
    User,
    VideoLike,
    VideoView,
)
from app.protocol_video import normalize_client_runtime_spec_versions
from app.public_origin import canonicalize_public_url
from app.routers.feed import _item_from_published, _load_feed_item_context
from app.safety import blocked_peer_ids, users_blocked_between
from app.schemas_social import (
    CommentAuthor,
    CommentCreateRequest,
    CommentOut,
    CommentPage,
    CommentReportRequest,
    CreatorProfile,
    CreatorSocialState,
    CreatorWork,
    CreatorWorkPage,
    EngagementSummary,
    FollowMutation,
    FollowUserOut,
    FollowUserPage,
    LikeMutation,
    NotificationActor,
    NotificationOut,
    NotificationPage,
    ReadMutation,
    ReconcileResult,
    SocialCapabilities,
    SocialState,
)
from app.users import follow_counts
from app.pagination import CursorError
from app.routers.user import _follow_page
from app.web_session import optional_web_user, require_web_user

public_router = APIRouter(prefix="/api/v1/public", tags=["social-public"])
app_router = APIRouter(prefix="/api/v1/social", tags=["social"])
web_router = APIRouter(prefix="/api/v1/web/social", tags=["web-social"])
operations_router = APIRouter(
    prefix="/internal/v1/social",
    tags=["social-operations"],
    dependencies=[Depends(require_publish_key)],
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return max(0, int(base64.urlsafe_b64decode(padded).decode()))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid cursor") from None


def _optional_user(request: Request, db: Session) -> AppUser | None:
    bearer = bearer_token_from_request(request)
    if bearer:
        return load_app_user(db, bearer)
    return optional_web_user(request, db)


def _enabled(settings: Settings, capability: str) -> None:
    if not bool(getattr(settings, capability, False)):
        raise HTTPException(status_code=404, detail="capability unavailable")


def _visible_video(db: Session, video_id: str) -> PublishedVideo:
    row = db.get(PublishedVideo, video_id)
    if (
        row is None
        or row.is_deleted != 0
        or row.deleted_at is not None
        or row.review_status != "approved"
        or not row.distribution_enabled
        or not row.cdn_ready
    ):
        raise HTTPException(status_code=404, detail="video not found")
    author = db.get(User, row.user_id) if row.user_id else None
    if author is None or not author.enabled:
        raise HTTPException(status_code=404, detail="video not found")
    return row


def _notify(
    db: Session,
    *,
    recipient_user_id: str | None,
    actor_user_id: str,
    kind: str,
    video_id: str | None = None,
    comment_id: str | None = None,
) -> None:
    if not recipient_user_id or recipient_user_id == actor_user_id:
        return
    target = comment_id or video_id or recipient_user_id
    key = f"{recipient_user_id}:{actor_user_id}:{kind}:{target}"
    if db.query(SocialNotification.id).filter(SocialNotification.dedupe_key == key).first():
        return
    db.add(
        SocialNotification(
            id=f"ntf_{secrets.token_urlsafe(18)}",
            recipient_user_id=recipient_user_id,
            actor_user_id=actor_user_id,
            type=kind,
            video_id=video_id,
            comment_id=comment_id,
            dedupe_key=key,
        )
    )


def _enforce_comment_rate(db: Session, user_id: str) -> None:
    now = _now()
    recent = (
        db.query(Comment.created_at)
        .filter(Comment.author_user_id == user_id)
        .order_by(Comment.created_at.desc())
        .first()
    )
    if recent and recent[0] > now - timedelta(seconds=5):
        raise HTTPException(status_code=429, detail="wait before commenting again")
    hourly = db.query(Comment.id).filter(
        Comment.author_user_id == user_id,
        Comment.created_at >= now - timedelta(hours=1),
    ).count()
    if hourly >= 30:
        raise HTTPException(status_code=429, detail="comment limit reached")


def _enforce_toggle_rate(db: Session, user_id: str, kind: str) -> None:
    since = _now() - timedelta(hours=1)
    total = db.query(SocialRateEvent.id).filter(
        SocialRateEvent.user_id == user_id,
        SocialRateEvent.created_at >= since,
    ).count()
    if total >= 120:
        raise HTTPException(status_code=429, detail="interaction limit reached")
    db.add(SocialRateEvent(user_id=user_id, kind=kind))


def _profile(db: Session, settings: Settings, row: User, viewer: AppUser | None) -> CreatorProfile:
    eligible = db.query(PublishedVideo).filter(
        PublishedVideo.user_id == row.user_id,
        PublishedVideo.is_deleted == 0,
        PublishedVideo.deleted_at.is_(None),
        PublishedVideo.review_status == "approved",
        PublishedVideo.distribution_enabled.is_(True),
        PublishedVideo.cdn_ready.is_(True),
    )
    work_count, received = eligible.with_entities(
        func.count(PublishedVideo.id), func.coalesce(func.sum(PublishedVideo.like_count), 0)
    ).one()
    following_count, follower_count = follow_counts(db, row.user_id)
    viewer_following = bool(viewer and db.query(Follow.id).filter(
        Follow.follower_user_id == viewer.user_id,
        Follow.followee_user_id == row.user_id,
    ).first())
    return CreatorProfile(
        user_id=row.user_id,
        nickname=row.nickname or "",
        avatar_url=canonicalize_public_url(settings, row.avatar_url) or "",
        bio=row.bio or "",
        work_count=int(work_count or 0),
        following_count=following_count,
        follower_count=follower_count,
        received_like_count=int(received or 0),
        viewer_following=viewer_following,
        viewer_is_owner=bool(viewer and viewer.user_id == row.user_id),
    )


@public_router.get("/capabilities", response_model=SocialCapabilities)
def capabilities(settings: Annotated[Settings, Depends(get_settings)]) -> SocialCapabilities:
    return SocialCapabilities(
        creator_profiles=settings.social_creator_profiles_enabled,
        video_likes=settings.social_video_likes_enabled,
        comments=settings.social_comments_enabled,
        notifications=settings.social_notifications_enabled,
        web_immersive_feed=settings.social_web_immersive_feed_enabled,
    )


@public_router.get("/social/state", response_model=SocialState)
def get_social_state(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    video_id: list[str] = Query(default=[]),
    creator_id: list[str] = Query(default=[]),
) -> SocialState:
    """Read viewer-aware engagement in one request for feed-sized batches."""
    if len(video_id) > 12 or len(creator_id) > 12:
        raise HTTPException(status_code=422, detail="at most 12 ids per resource type")
    videos = list(dict.fromkeys(item.strip() for item in video_id if item.strip()))
    creators = list(dict.fromkeys(item.strip() for item in creator_id if item.strip()))
    if any(len(item) > 128 for item in videos) or any(len(item) > 128 for item in creators):
        raise HTTPException(status_code=422, detail="invalid social state id")

    viewer = _optional_user(request, db)
    excluded = blocked_peer_ids(db, viewer.user_id) if viewer else set()
    visible_rows = db.query(PublishedVideo).filter(
        PublishedVideo.id.in_(videos),
        PublishedVideo.is_deleted == 0,
        PublishedVideo.deleted_at.is_(None),
        PublishedVideo.review_status == "approved",
        PublishedVideo.distribution_enabled.is_(True),
        PublishedVideo.cdn_ready.is_(True),
    ).all() if videos else []
    author_ids = {row.user_id for row in visible_rows if row.user_id}
    enabled_authors = {
        row.user_id for row in db.query(User).filter(
            User.user_id.in_(author_ids),
            User.enabled.is_(True),
            User.deletion_requested_at.is_(None),
        ).all()
    } if author_ids else set()
    visible_rows = [
        row for row in visible_rows
        if row.user_id in enabled_authors and row.user_id not in excluded
    ]
    visible_video_ids = [row.id for row in visible_rows]
    liked_ids = {
        value for (value,) in db.query(VideoLike.video_id).filter(
            VideoLike.user_id == viewer.user_id,
            VideoLike.video_id.in_(visible_video_ids),
        ).all()
    } if viewer and visible_video_ids else set()
    player_counts = {
        value: int(count) for value, count in db.query(
            VideoView.video_id, func.count(VideoView.id)
        ).filter(VideoView.video_id.in_(visible_video_ids)).group_by(VideoView.video_id).all()
    } if visible_video_ids else {}

    creator_rows = db.query(User).filter(
        User.user_id.in_(creators),
        User.enabled.is_(True),
        User.deletion_requested_at.is_(None),
    ).all() if creators else []
    creator_rows = [row for row in creator_rows if row.user_id not in excluded]
    creator_row_ids = [row.user_id for row in creator_rows]
    published_creator_ids = {
        value for (value,) in db.query(PublishedVideo.user_id).filter(
            PublishedVideo.user_id.in_(creator_row_ids)
        ).distinct().all()
    } if creator_row_ids else set()
    visible_creators = [
        row for row in creator_rows
        if row.creator_activated_at is not None or row.user_id in published_creator_ids
    ]
    visible_creator_ids = [row.user_id for row in visible_creators]
    followed_ids = {
        value for (value,) in db.query(Follow.followee_user_id).filter(
            Follow.follower_user_id == viewer.user_id,
            Follow.followee_user_id.in_(visible_creator_ids),
        ).all()
    } if viewer and visible_creators else set()
    follower_counts = {
        value: int(count) for value, count in db.query(
            Follow.followee_user_id, func.count(Follow.follower_user_id)
        ).filter(Follow.followee_user_id.in_(visible_creator_ids)).group_by(
            Follow.followee_user_id
        ).all()
    } if visible_creator_ids else {}

    return SocialState(
        videos={
            row.id: EngagementSummary(
                unique_player_count=player_counts.get(row.id, 0),
                like_count=max(0, row.like_count),
                comment_count=max(0, row.comment_count),
                viewer_liked=row.id in liked_ids,
            ) for row in visible_rows
        },
        creators={
            row.user_id: CreatorSocialState(
                follower_count=follower_counts.get(row.user_id, 0),
                viewer_following=row.user_id in followed_ids,
            ) for row in visible_creators
        },
    )


@public_router.get("/creators/{user_id}", response_model=CreatorProfile)
def get_creator(
    user_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorProfile:
    _enabled(settings, "social_creator_profiles_enabled")
    row = db.get(User, user_id)
    viewer = _optional_user(request, db)
    if row is None or not row.enabled or row.deletion_requested_at is not None:
        raise HTTPException(status_code=404, detail="creator not found")
    ever_published = db.query(PublishedVideo.id).filter(PublishedVideo.user_id == user_id).first()
    if row.creator_activated_at is None and ever_published is None:
        raise HTTPException(status_code=404, detail="creator not found")
    if viewer and users_blocked_between(db, viewer.user_id, user_id):
        raise HTTPException(status_code=404, detail="creator not found")
    return _profile(db, settings, row, viewer)


@public_router.get("/creators/{user_id}/works", response_model=CreatorWorkPage)
def get_creator_works(
    user_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=20, ge=1, le=50),
    cursor: str | None = None,
) -> CreatorWorkPage:
    get_creator(user_id, request, db, settings)
    viewer = _optional_user(request, db)
    offset = _offset(cursor)
    query = db.query(PublishedVideo).filter(
        PublishedVideo.user_id == user_id,
        PublishedVideo.is_deleted == 0,
        PublishedVideo.deleted_at.is_(None),
        PublishedVideo.review_status == "approved",
        PublishedVideo.distribution_enabled.is_(True),
        PublishedVideo.cdn_ready.is_(True),
    ).order_by(PublishedVideo.created_at.desc(), PublishedVideo.id.desc())
    rows = query.offset(offset).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    context = _load_feed_item_context(db, rows, viewer_user_id=viewer.user_id if viewer else None)
    liked_ids = set()
    if viewer and rows:
        liked_ids = {video_id for (video_id,) in db.query(VideoLike.video_id).filter(
            VideoLike.user_id == viewer.user_id,
            VideoLike.video_id.in_([row.id for row in rows]),
        ).all()}
    seo = {row.video_id: row for row in db.query(PublishedVideoSeo).filter(
        PublishedVideoSeo.video_id.in_([item.id for item in rows])
    ).all()} if rows else {}
    items: list[CreatorWork] = []
    for row in rows:
        feed_item = _item_from_published(
            db, row, settings=settings,
            viewer_user_id=viewer.user_id if viewer else None,
            context=context,
        )
        if feed_item is None:
            continue
        items.append(CreatorWork(
            video_id=row.id,
            title=row.title or "",
            description=row.description or "",
            thumbnail_url=feed_item.thumbnail_url,
            share_url=feed_item.share_url,
            interaction_types=(seo[row.id].interaction_types if row.id in seo else []),
            engagement=EngagementSummary(
                unique_player_count=context.play_counts_by_video_id.get(row.id, 0),
                like_count=max(0, row.like_count),
                comment_count=max(0, row.comment_count),
                viewer_liked=row.id in liked_ids,
            ),
            review_status=row.review_status,
            created_at=_iso(row.created_at),
        ))
    return CreatorWorkPage(
        items=items,
        next_cursor=_cursor(offset + limit) if has_more else None,
        has_more=has_more,
    )


def _comments_page(
    *, db: Session, settings: Settings, request: Request, video_id: str,
    root_comment_id: str | None, limit: int, cursor: str | None,
) -> CommentPage:
    _enabled(settings, "social_comments_enabled")
    video = _visible_video(db, video_id)
    viewer = _optional_user(request, db)
    if viewer and users_blocked_between(db, viewer.user_id, video.user_id or ""):
        raise HTTPException(status_code=404, detail="video not found")
    excluded = blocked_peer_ids(db, viewer.user_id) if viewer else set()
    offset = _offset(cursor)
    query = db.query(Comment).filter(
        Comment.video_id == video_id,
        Comment.root_comment_id.is_(None) if root_comment_id is None else Comment.root_comment_id == root_comment_id,
        Comment.moderation_status != "hidden",
    )
    if excluded:
        query = query.filter(~Comment.author_user_id.in_(excluded))
    rows = query.order_by(Comment.created_at.desc(), Comment.id.desc()).offset(offset).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    author_ids = {row.author_user_id for row in rows}
    authors = {row.user_id: row for row in db.query(User).filter(User.user_id.in_(author_ids), User.enabled.is_(True)).all()} if author_ids else {}
    liked = set()
    if viewer and rows:
        liked = {comment_id for (comment_id,) in db.query(CommentLike.comment_id).filter(
            CommentLike.user_id == viewer.user_id,
            CommentLike.comment_id.in_([row.id for row in rows]),
        ).all()}
    items: list[CommentOut] = []
    for row in rows:
        author = authors.get(row.author_user_id)
        if author is None:
            continue
        deleted = row.deleted_at is not None or row.moderation_status == "removed"
        items.append(CommentOut(
            id=row.id,
            video_id=row.video_id,
            author=CommentAuthor(
                user_id=author.user_id,
                nickname=author.nickname or "",
                avatar_url=canonicalize_public_url(settings, author.avatar_url) or "",
            ),
            body="Comment deleted" if deleted else row.body,
            root_comment_id=row.root_comment_id,
            reply_to_user_id=row.reply_to_user_id,
            like_count=max(0, row.like_count),
            reply_count=max(0, row.reply_count),
            viewer_liked=row.id in liked,
            can_delete=bool(viewer and viewer.user_id == row.author_user_id and not deleted),
            can_hide=bool(viewer and viewer.user_id == video.user_id and viewer.user_id != row.author_user_id and not deleted),
            is_deleted=deleted,
            created_at=_iso(row.created_at),
        ))
    return CommentPage(items=items, next_cursor=_cursor(offset + limit) if has_more else None, has_more=has_more)


@public_router.get("/videos/{video_id}/comments", response_model=CommentPage)
def list_comments(
    video_id: str, request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=20, ge=1, le=50), cursor: str | None = None,
) -> CommentPage:
    return _comments_page(db=db, settings=settings, request=request, video_id=video_id, root_comment_id=None, limit=limit, cursor=cursor)


@public_router.get("/videos/{video_id}/engagement", response_model=EngagementSummary)
def get_video_engagement(
    video_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EngagementSummary:
    video = _visible_video(db, video_id)
    viewer = _optional_user(request, db)
    if viewer and users_blocked_between(db, viewer.user_id, video.user_id or ""):
        raise HTTPException(status_code=404, detail="video not found")
    liked = bool(viewer and db.query(VideoLike.id).filter(
        VideoLike.video_id == video_id,
        VideoLike.user_id == viewer.user_id,
    ).first())
    players = db.query(VideoView.id).filter(VideoView.video_id == video_id).count()
    return EngagementSummary(
        unique_player_count=players,
        like_count=max(0, video.like_count),
        comment_count=max(0, video.comment_count),
        viewer_liked=liked,
    )


@public_router.get("/comments/{comment_id}/replies", response_model=CommentPage)
def list_replies(
    comment_id: str, request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=20, ge=1, le=50), cursor: str | None = None,
) -> CommentPage:
    root = db.get(Comment, comment_id)
    if root is None or root.root_comment_id is not None:
        raise HTTPException(status_code=404, detail="comment not found")
    return _comments_page(db=db, settings=settings, request=request, video_id=root.video_id, root_comment_id=root.id, limit=limit, cursor=cursor)


def _like_video(db: Session, settings: Settings, user: AppUser, video_id: str, active: bool) -> LikeMutation:
    _enabled(settings, "social_video_likes_enabled")
    video = _visible_video(db, video_id)
    if users_blocked_between(db, user.user_id, video.user_id or ""):
        raise HTTPException(status_code=403, detail="interaction unavailable")
    _enforce_toggle_rate(db, user.user_id, "video_like")
    row = db.query(VideoLike).filter(VideoLike.video_id == video_id, VideoLike.user_id == user.user_id).one_or_none()
    if active and row is None:
        db.add(VideoLike(video_id=video_id, user_id=user.user_id))
        video.like_count = max(0, video.like_count) + 1
        _notify(db, recipient_user_id=video.user_id, actor_user_id=user.user_id, kind="video_like", video_id=video_id)
    elif not active and row is not None:
        db.delete(row)
        video.like_count = max(0, video.like_count - 1)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        current = db.query(VideoLike.id).filter(VideoLike.video_id == video_id, VideoLike.user_id == user.user_id).first() is not None
        video = db.get(PublishedVideo, video_id)
        return LikeMutation(active=current, like_count=max(0, video.like_count if video else 0))
    return LikeMutation(active=active, like_count=max(0, video.like_count))


def _follow(db: Session, settings: Settings, user: AppUser, target_id: str, active: bool) -> FollowMutation:
    _enabled(settings, "social_creator_profiles_enabled")
    target = db.get(User, target_id)
    if target is None or not target.enabled:
        raise HTTPException(status_code=404, detail="creator not found")
    if target_id == user.user_id:
        raise HTTPException(status_code=400, detail="cannot follow yourself")
    if users_blocked_between(db, user.user_id, target_id):
        raise HTTPException(status_code=403, detail="interaction unavailable")
    _enforce_toggle_rate(db, user.user_id, "follow")
    row = db.query(Follow).filter(Follow.follower_user_id == user.user_id, Follow.followee_user_id == target_id).one_or_none()
    if active and row is None:
        db.add(Follow(follower_user_id=user.user_id, followee_user_id=target_id))
        _notify(db, recipient_user_id=target_id, actor_user_id=user.user_id, kind="follow")
    elif not active and row is not None:
        db.delete(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    count = db.query(Follow.id).filter(Follow.followee_user_id == target_id).count()
    current = db.query(Follow.id).filter(Follow.follower_user_id == user.user_id, Follow.followee_user_id == target_id).first() is not None
    return FollowMutation(active=current, follower_count=count)


def _create_comment(db: Session, settings: Settings, user: AppUser, video_id: str, body: str, root_id: str | None = None) -> CommentOut:
    _enabled(settings, "social_comments_enabled")
    video = _visible_video(db, video_id)
    if users_blocked_between(db, user.user_id, video.user_id or ""):
        raise HTTPException(status_code=403, detail="interaction unavailable")
    _enforce_comment_rate(db, user.user_id)
    root = None
    if root_id:
        root = db.get(Comment, root_id)
        if root is None or root.video_id != video_id or root.root_comment_id is not None or root.moderation_status == "hidden":
            raise HTTPException(status_code=404, detail="comment not found")
        if users_blocked_between(db, user.user_id, root.author_user_id):
            raise HTTPException(status_code=403, detail="interaction unavailable")
    now = _now()
    row = Comment(
        id=f"cmt_{secrets.token_urlsafe(18)}",
        video_id=video_id,
        author_user_id=user.user_id,
        root_comment_id=root.id if root else None,
        reply_to_user_id=root.author_user_id if root else None,
        body=body,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    video.comment_count = max(0, video.comment_count) + 1
    if root:
        root.reply_count = max(0, root.reply_count) + 1
        recipient = root.author_user_id
        kind = "reply"
    else:
        recipient = video.user_id
        kind = "comment"
    _notify(db, recipient_user_id=recipient, actor_user_id=user.user_id, kind=kind, video_id=video_id, comment_id=row.id)
    db.commit()
    author = db.get(User, user.user_id)
    return CommentOut(
        id=row.id, video_id=video_id,
        author=CommentAuthor(user_id=user.user_id, nickname=(author.nickname if author else "") or "", avatar_url=canonicalize_public_url(settings, author.avatar_url if author else "") or ""),
        body=row.body, root_comment_id=row.root_comment_id, reply_to_user_id=row.reply_to_user_id,
        can_delete=True, created_at=_iso(row.created_at),
    )


def _like_comment(db: Session, settings: Settings, user: AppUser, comment_id: str, active: bool) -> LikeMutation:
    _enabled(settings, "social_comments_enabled")
    comment = db.get(Comment, comment_id)
    if comment is None or comment.moderation_status != "visible" or comment.deleted_at is not None:
        raise HTTPException(status_code=404, detail="comment not found")
    _visible_video(db, comment.video_id)
    if users_blocked_between(db, user.user_id, comment.author_user_id):
        raise HTTPException(status_code=403, detail="interaction unavailable")
    _enforce_toggle_rate(db, user.user_id, "comment_like")
    row = db.query(CommentLike).filter(CommentLike.comment_id == comment_id, CommentLike.user_id == user.user_id).one_or_none()
    if active and row is None:
        db.add(CommentLike(comment_id=comment_id, user_id=user.user_id))
        comment.like_count = max(0, comment.like_count) + 1
        _notify(db, recipient_user_id=comment.author_user_id, actor_user_id=user.user_id, kind="comment_like", video_id=comment.video_id, comment_id=comment_id)
    elif not active and row is not None:
        db.delete(row)
        comment.like_count = max(0, comment.like_count - 1)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    current = db.query(CommentLike.id).filter(CommentLike.comment_id == comment_id, CommentLike.user_id == user.user_id).first() is not None
    return LikeMutation(active=current, like_count=max(0, comment.like_count))


def _remove_comment(db: Session, user: AppUser, comment_id: str, *, hide: bool) -> CommentOut:
    comment = db.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="comment not found")
    video = db.get(PublishedVideo, comment.video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="comment not found")
    if hide:
        if video.user_id != user.user_id or comment.author_user_id == user.user_id:
            raise HTTPException(status_code=403, detail="only the work author can hide this comment")
        comment.moderation_status = "hidden"
    else:
        if comment.author_user_id != user.user_id:
            raise HTTPException(status_code=403, detail="only the comment author can delete it")
        comment.body = ""
        comment.deleted_at = _now()
        comment.moderation_status = "removed"
    video.comment_count = max(0, video.comment_count - 1)
    if comment.root_comment_id:
        root = db.get(Comment, comment.root_comment_id)
        if root:
            root.reply_count = max(0, root.reply_count - 1)
    db.commit()
    author = db.get(User, comment.author_user_id)
    return CommentOut(
        id=comment.id, video_id=comment.video_id,
        author=CommentAuthor(user_id=comment.author_user_id, nickname=(author.nickname if author else "") or ""),
        body="Comment deleted", root_comment_id=comment.root_comment_id,
        reply_to_user_id=comment.reply_to_user_id, like_count=max(0, comment.like_count),
        reply_count=max(0, comment.reply_count), is_deleted=True, created_at=_iso(comment.created_at),
    )


def _liked(
    db: Session,
    settings: Settings,
    user: AppUser,
    limit: int,
    cursor: str | None,
    *,
    supported_runtime_spec_versions: frozenset[str],
) -> CreatorWorkPage:
    _enabled(settings, "social_video_likes_enabled")
    offset = _offset(cursor)
    query = db.query(VideoLike, PublishedVideo).join(
        PublishedVideo, PublishedVideo.id == VideoLike.video_id
    ).filter(
        VideoLike.user_id == user.user_id,
        PublishedVideo.is_deleted == 0,
        PublishedVideo.deleted_at.is_(None),
        PublishedVideo.review_status == "approved",
        PublishedVideo.distribution_enabled.is_(True),
        PublishedVideo.cdn_ready.is_(True),
        or_(
            and_(
                PublishedVideo.content_type == CONTENT_TYPE_RUNTIME,
                PublishedVideo.runtime_spec.is_not(None),
                PublishedVideo.runtime_spec_version.in_(supported_runtime_spec_versions),
            ),
            and_(
                PublishedVideo.content_type == CONTENT_TYPE_HTML,
                PublishedVideo.html_url.is_not(None),
                PublishedVideo.bridge_version == HTML_BRIDGE_VERSION,
            ),
        ),
    )
    total_count = query.count()
    pairs = query.order_by(
        VideoLike.created_at.desc(), VideoLike.id.desc()
    ).offset(offset).limit(limit + 1).all()
    has_more = len(pairs) > limit
    rows = [pair[1] for pair in pairs[:limit]]
    context = _load_feed_item_context(db, rows, viewer_user_id=user.user_id)
    items = []
    for row in rows:
        item = _item_from_published(
            db,
            row,
            settings=settings,
            viewer_user_id=user.user_id,
            context=context,
            supported_runtime_spec_versions=supported_runtime_spec_versions,
        )
        if item:
            items.append(CreatorWork(
                video_id=row.id, title=row.title or "", description=row.description or "",
                thumbnail_url=item.thumbnail_url, share_url=item.share_url,
                engagement=EngagementSummary(unique_player_count=context.play_counts_by_video_id.get(row.id, 0), like_count=row.like_count, comment_count=row.comment_count, viewer_liked=True),
                review_status=row.review_status, created_at=_iso(row.created_at),
            ))
    return CreatorWorkPage(
        items=items,
        next_cursor=_cursor(offset + limit) if has_more else None,
        has_more=has_more,
        total_count=total_count,
    )


def _notifications(db: Session, settings: Settings, user: AppUser, limit: int, cursor: str | None) -> NotificationPage:
    _enabled(settings, "social_notifications_enabled")
    offset = _offset(cursor)
    excluded = blocked_peer_ids(db, user.user_id)
    query = db.query(SocialNotification).filter(SocialNotification.recipient_user_id == user.user_id)
    if excluded:
        query = query.filter(~SocialNotification.actor_user_id.in_(excluded))
    rows = query.order_by(SocialNotification.created_at.desc(), SocialNotification.id.desc()).offset(offset).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    actor_ids = {row.actor_user_id for row in rows}
    actors = {row.user_id: row for row in db.query(User).filter(User.user_id.in_(actor_ids), User.enabled.is_(True)).all()} if actor_ids else {}
    items = [NotificationOut(
        id=row.id, type=row.type,
        actor=NotificationActor(user_id=actor.user_id, nickname=actor.nickname or "", avatar_url=canonicalize_public_url(settings, actor.avatar_url) or ""),
        video_id=row.video_id, comment_id=row.comment_id, read=row.read_at is not None,
        created_at=_iso(row.created_at),
    ) for row in rows if (actor := actors.get(row.actor_user_id)) is not None]
    unread = query.filter(SocialNotification.read_at.is_(None)).count()
    return NotificationPage(items=items, next_cursor=_cursor(offset + limit) if has_more else None, has_more=has_more, unread_count=unread)


def _mark_read(db: Session, user: AppUser, notification_id: str | None) -> ReadMutation:
    query = db.query(SocialNotification).filter(
        SocialNotification.recipient_user_id == user.user_id,
        SocialNotification.read_at.is_(None),
    )
    if notification_id:
        query = query.filter(SocialNotification.id == notification_id)
    updated = query.update({SocialNotification.read_at: _now()}, synchronize_session=False)
    db.commit()
    unread = db.query(SocialNotification.id).filter(
        SocialNotification.recipient_user_id == user.user_id,
        SocialNotification.read_at.is_(None),
    ).count()
    return ReadMutation(updated=updated, unread_count=unread)


def _web_follow_page(
    *,
    db: Session,
    settings: Settings,
    viewer: AppUser,
    target_id: str,
    direction: str,
    limit: int,
    cursor: str | None,
) -> FollowUserPage:
    target = db.get(User, target_id)
    if (
        target is None
        or not target.enabled
        or target.deletion_requested_at is not None
        or users_blocked_between(db, viewer.user_id, target_id)
    ):
        raise HTTPException(status_code=404, detail="creator not found")
    filter_column = Follow.followee_user_id if direction == "followers" else Follow.follower_user_id
    peer_attr = "follower_user_id" if direction == "followers" else "followee_user_id"
    try:
        rows, next_cursor, has_more = _follow_page(
            db,
            filter_column=filter_column,
            target_user_id=target_id,
            cursor=cursor,
            cursor_kind=f"{direction}:{target_id}",
            cursor_secret=settings.cursor_secret or settings.publish_key,
            limit=limit,
        )
    except CursorError:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    peer_ids = [getattr(row, peer_attr) for row in rows]
    peers = {
        row.user_id: row for row in db.query(User).filter(
            User.user_id.in_(peer_ids),
            User.enabled.is_(True),
            User.deletion_requested_at.is_(None),
        ).all()
    } if peer_ids else {}
    items = []
    for relation in rows:
        peer_id = getattr(relation, peer_attr)
        peer = peers.get(peer_id)
        if peer is None or users_blocked_between(db, viewer.user_id, peer_id):
            continue
        items.append(FollowUserOut(
            user_id=peer.user_id,
            nickname=peer.nickname or "",
            avatar_url=canonicalize_public_url(settings, peer.avatar_url) or "",
            created_at=_iso(relation.created_at),
        ))
    return FollowUserPage(items=items, next_cursor=next_cursor, has_more=has_more)


@web_router.get("/creators/{user_id}/followers", response_model=FollowUserPage)
def web_creator_followers(
    user_id: str,
    user: Annotated[AppUser, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=20, ge=1, le=50),
    cursor: str | None = None,
) -> FollowUserPage:
    return _web_follow_page(
        db=db, settings=settings, viewer=user, target_id=user_id,
        direction="followers", limit=limit, cursor=cursor,
    )


@web_router.get("/creators/{user_id}/following", response_model=FollowUserPage)
def web_creator_following(
    user_id: str,
    user: Annotated[AppUser, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=20, ge=1, le=50),
    cursor: str | None = None,
) -> FollowUserPage:
    return _web_follow_page(
        db=db, settings=settings, viewer=user, target_id=user_id,
        direction="following", limit=limit, cursor=cursor,
    )


def _register_routes(router: APIRouter, auth: Callable[..., AppUser]) -> None:
    @router.put("/creators/{user_id}/follow", response_model=FollowMutation)
    def follow_creator(user_id: str, user: Annotated[AppUser, Depends(auth)], db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_settings)]) -> FollowMutation:
        return _follow(db, settings, user, user_id, True)

    @router.delete("/creators/{user_id}/follow", response_model=FollowMutation)
    def unfollow_creator(user_id: str, user: Annotated[AppUser, Depends(auth)], db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_settings)]) -> FollowMutation:
        return _follow(db, settings, user, user_id, False)

    @router.put("/videos/{video_id}/like", response_model=LikeMutation)
    def like_video(video_id: str, user: Annotated[AppUser, Depends(auth)], db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_settings)]) -> LikeMutation:
        return _like_video(db, settings, user, video_id, True)

    @router.delete("/videos/{video_id}/like", response_model=LikeMutation)
    def unlike_video(video_id: str, user: Annotated[AppUser, Depends(auth)], db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_settings)]) -> LikeMutation:
        return _like_video(db, settings, user, video_id, False)

    @router.post("/videos/{video_id}/comments", response_model=CommentOut)
    def create_comment(video_id: str, payload: CommentCreateRequest, user: Annotated[AppUser, Depends(auth)], db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_settings)]) -> CommentOut:
        return _create_comment(db, settings, user, video_id, payload.body)

    @router.post("/comments/{comment_id}/replies", response_model=CommentOut)
    def create_reply(comment_id: str, payload: CommentCreateRequest, user: Annotated[AppUser, Depends(auth)], db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_settings)]) -> CommentOut:
        root = db.get(Comment, comment_id)
        if root is None:
            raise HTTPException(status_code=404, detail="comment not found")
        return _create_comment(db, settings, user, root.video_id, payload.body, root.id)

    @router.put("/comments/{comment_id}/like", response_model=LikeMutation)
    def like_comment(comment_id: str, user: Annotated[AppUser, Depends(auth)], db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_settings)]) -> LikeMutation:
        return _like_comment(db, settings, user, comment_id, True)

    @router.delete("/comments/{comment_id}/like", response_model=LikeMutation)
    def unlike_comment(comment_id: str, user: Annotated[AppUser, Depends(auth)], db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_settings)]) -> LikeMutation:
        return _like_comment(db, settings, user, comment_id, False)

    @router.delete("/comments/{comment_id}", response_model=CommentOut)
    def delete_comment(comment_id: str, user: Annotated[AppUser, Depends(auth)], db: Annotated[Session, Depends(get_db)]) -> CommentOut:
        return _remove_comment(db, user, comment_id, hide=False)

    @router.post("/comments/{comment_id}/hide", response_model=CommentOut)
    def hide_comment(comment_id: str, user: Annotated[AppUser, Depends(auth)], db: Annotated[Session, Depends(get_db)]) -> CommentOut:
        return _remove_comment(db, user, comment_id, hide=True)

    @router.post("/comments/{comment_id}/report")
    def report_comment(comment_id: str, payload: CommentReportRequest, user: Annotated[AppUser, Depends(auth)], db: Annotated[Session, Depends(get_db)]) -> dict[str, bool]:
        comment = db.get(Comment, comment_id)
        if comment is None or comment.moderation_status == "hidden":
            raise HTTPException(status_code=404, detail="comment not found")
        if comment.author_user_id == user.user_id:
            raise HTTPException(status_code=400, detail="cannot report yourself")
        report = db.query(ContentReport).filter(
            ContentReport.reporter_user_id == user.user_id,
            ContentReport.target_type == "comment",
            ContentReport.target_id == comment_id,
        ).one_or_none()
        now = _now()
        if report is None:
            report = ContentReport(
                id=f"rpt_{secrets.token_urlsafe(18)}", reporter_user_id=user.user_id,
                target_type="comment", target_id=comment_id,
                target_user_id=comment.author_user_id, reason=payload.reason.strip(),
                details=payload.details.strip(), status="pending", created_at=now, updated_at=now,
            )
            db.add(report)
        else:
            report.reason = payload.reason.strip()
            report.details = payload.details.strip()
            report.status = "pending"
            report.updated_at = now
        db.commit()
        return {"reported": True}

    @router.get("/liked", response_model=CreatorWorkPage)
    def liked_videos(
        user: Annotated[AppUser, Depends(auth)],
        db: Annotated[Session, Depends(get_db)],
        settings: Annotated[Settings, Depends(get_settings)],
        limit: int = Query(default=20, ge=1, le=50),
        cursor: str | None = None,
        experience_spec_versions: str | None = None,
        camera_continuous_targets: str | None = None,
    ) -> CreatorWorkPage:
        supported_versions = normalize_client_runtime_spec_versions(
            experience_spec_versions.split(",") if experience_spec_versions else None,
            camera_continuous_targets.split(",") if camera_continuous_targets else None,
        )
        return _liked(
            db,
            settings,
            user,
            limit,
            cursor,
            supported_runtime_spec_versions=supported_versions,
        )

    @router.get("/notifications", response_model=NotificationPage)
    def notifications(user: Annotated[AppUser, Depends(auth)], db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_settings)], limit: int = Query(default=20, ge=1, le=50), cursor: str | None = None) -> NotificationPage:
        return _notifications(db, settings, user, limit, cursor)

    @router.post("/notifications/{notification_id}/read", response_model=ReadMutation)
    def read_notification(notification_id: str, user: Annotated[AppUser, Depends(auth)], db: Annotated[Session, Depends(get_db)]) -> ReadMutation:
        return _mark_read(db, user, notification_id)

    @router.post("/notifications/read-all", response_model=ReadMutation)
    def read_all_notifications(user: Annotated[AppUser, Depends(auth)], db: Annotated[Session, Depends(get_db)]) -> ReadMutation:
        return _mark_read(db, user, None)


_register_routes(app_router, require_bearer_user)
_register_routes(web_router, require_web_user)


@operations_router.post("/reconcile", response_model=ReconcileResult)
def reconcile_counts(db: Annotated[Session, Depends(get_db)]) -> ReconcileResult:
    videos_updated = 0
    for video in db.query(PublishedVideo).all():
        likes = db.query(VideoLike.id).filter(VideoLike.video_id == video.id).count()
        comments = db.query(Comment.id).filter(
            Comment.video_id == video.id,
            Comment.moderation_status == "visible",
            Comment.deleted_at.is_(None),
        ).count()
        if video.like_count != likes or video.comment_count != comments:
            video.like_count, video.comment_count = likes, comments
            videos_updated += 1
    comments_updated = 0
    for comment in db.query(Comment).all():
        likes = db.query(CommentLike.id).filter(CommentLike.comment_id == comment.id).count()
        replies = db.query(Comment.id).filter(
            Comment.root_comment_id == comment.id,
            Comment.moderation_status == "visible",
            Comment.deleted_at.is_(None),
        ).count()
        if comment.like_count != likes or comment.reply_count != replies:
            comment.like_count, comment.reply_count = likes, replies
            comments_updated += 1
    db.commit()
    return ReconcileResult(videos_updated=videos_updated, comments_updated=comments_updated)
