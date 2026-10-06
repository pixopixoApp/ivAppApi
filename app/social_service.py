from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Comment,
    PublishedVideo,
    SocialNotification,
    SocialRateEvent,
    User,
    VideoLike,
)
from app.public_origin import canonicalize_public_url
from app.safety import users_blocked_between
from app.schemas_social import CommentAuthor, CommentOut, LikeMutation
from app.social_seed import (
    displayed_like_count,
    is_seed_user,
    preview_enabled,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def visible_video(
    db: Session, video_id: str, *, for_update: bool = False
) -> PublishedVideo:
    query = db.query(PublishedVideo).filter(PublishedVideo.id == video_id)
    if for_update:
        query = query.with_for_update()
    row = query.one_or_none()
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


def _require_capability(settings: Settings, name: str) -> None:
    if not bool(getattr(settings, name, False)):
        raise HTTPException(status_code=404, detail="capability unavailable")


def _enforce_toggle_rate(db: Session, user_id: str, kind: str) -> None:
    since = _now() - timedelta(hours=1)
    total = db.query(SocialRateEvent.id).filter(
        SocialRateEvent.user_id == user_id,
        SocialRateEvent.created_at >= since,
    ).count()
    if total >= 120:
        raise HTTPException(status_code=429, detail="interaction limit reached")
    db.add(SocialRateEvent(user_id=user_id, kind=kind))


def _enforce_comment_rate(db: Session, user_id: str) -> None:
    now = _now()
    recent = db.query(Comment.created_at).filter(
        Comment.author_user_id == user_id
    ).order_by(Comment.created_at.desc()).first()
    recent_at = recent[0] if recent else None
    if recent_at is not None and recent_at.tzinfo is None:
        # MySQL returns naive datetimes; the column stores UTC.
        recent_at = recent_at.replace(tzinfo=timezone.utc)
    if recent_at is not None and recent_at > now - timedelta(seconds=5):
        raise HTTPException(status_code=429, detail="wait before commenting again")
    hourly = db.query(Comment.id).filter(
        Comment.author_user_id == user_id,
        Comment.created_at >= now - timedelta(hours=1),
    ).count()
    if hourly >= 30:
        raise HTTPException(status_code=429, detail="comment limit reached")


def _notify(
    db: Session,
    *,
    recipient_user_id: str | None,
    actor_user_id: str,
    kind: str,
    video_id: str,
    comment_id: str | None = None,
) -> None:
    if not recipient_user_id or recipient_user_id == actor_user_id:
        return
    target = comment_id or video_id
    key = f"{recipient_user_id}:{actor_user_id}:{kind}:{target}"
    if db.query(SocialNotification.id).filter(
        SocialNotification.dedupe_key == key
    ).first():
        return
    db.add(SocialNotification(
        id=f"ntf_{secrets.token_urlsafe(18)}",
        recipient_user_id=recipient_user_id,
        actor_user_id=actor_user_id,
        type=kind,
        video_id=video_id,
        comment_id=comment_id,
        dedupe_key=key,
    ))


def _actor(db: Session, actor_user_id: str) -> User:
    row = db.get(User, actor_user_id)
    if row is None or not row.enabled or row.deletion_requested_at is not None:
        raise HTTPException(status_code=403, detail="actor unavailable")
    return row


def mutate_video_like(
    db: Session,
    settings: Settings,
    *,
    actor_user_id: str,
    video_id: str,
    active: bool,
) -> LikeMutation:
    _require_capability(settings, "social_video_likes_enabled")
    actor = _actor(db, actor_user_id)
    video = visible_video(db, video_id, for_update=True)
    if users_blocked_between(db, actor_user_id, video.user_id or ""):
        raise HTTPException(status_code=403, detail="interaction unavailable")
    _enforce_toggle_rate(db, actor_user_id, "video_like")
    seed = is_seed_user(actor)
    row = db.query(VideoLike).filter(
        VideoLike.video_id == video_id,
        VideoLike.user_id == actor_user_id,
    ).one_or_none()
    if active and row is None:
        db.add(VideoLike(video_id=video_id, user_id=actor_user_id, is_seed=seed))
        if seed:
            video.seed_like_count = max(0, video.seed_like_count) + 1
        else:
            video.like_count = max(0, video.like_count) + 1
            _notify(
                db,
                recipient_user_id=video.user_id,
                actor_user_id=actor_user_id,
                kind="video_like",
                video_id=video_id,
            )
    elif not active and row is not None:
        db.delete(row)
        if row.is_seed:
            video.seed_like_count = max(0, video.seed_like_count - 1)
        else:
            video.like_count = max(0, video.like_count - 1)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    current = db.query(VideoLike.id).filter(
        VideoLike.video_id == video_id,
        VideoLike.user_id == actor_user_id,
    ).first() is not None
    video = db.get(PublishedVideo, video_id)
    count = displayed_like_count(video, preview_enabled(db)) if video else 0
    return LikeMutation(active=current, like_count=count)


def comment_out(db: Session, settings: Settings, row: Comment) -> CommentOut:
    author = db.get(User, row.author_user_id)
    return CommentOut(
        id=row.id,
        video_id=row.video_id,
        author=CommentAuthor(
            user_id=row.author_user_id,
            nickname=(author.nickname if author else "") or "",
            avatar_url=canonicalize_public_url(
                settings, author.avatar_url if author else ""
            ) or "",
        ),
        body=row.body,
        root_comment_id=row.root_comment_id,
        reply_to_user_id=row.reply_to_user_id,
        can_delete=True,
        created_at=row.created_at.isoformat(),
    )


def create_comment(
    db: Session,
    settings: Settings,
    *,
    actor_user_id: str,
    video_id: str,
    body: str,
    root_id: str | None = None,
    idempotency_key: str | None = None,
) -> CommentOut:
    _require_capability(settings, "social_comments_enabled")
    actor = _actor(db, actor_user_id)
    seed = is_seed_user(actor)
    if seed and root_id is not None:
        raise HTTPException(status_code=400, detail="seed comments must be top-level")
    normalized_key = idempotency_key.strip() if idempotency_key else None
    if seed and not normalized_key:
        raise HTTPException(status_code=422, detail="idempotency_key required")
    if normalized_key:
        existing = db.query(Comment).filter(
            Comment.author_user_id == actor_user_id,
            Comment.idempotency_key == normalized_key,
        ).one_or_none()
        if existing is not None:
            if existing.video_id != video_id or existing.body != body:
                raise HTTPException(status_code=409, detail="idempotency key payload mismatch")
            return comment_out(db, settings, existing)

    video = visible_video(db, video_id, for_update=True)
    if users_blocked_between(db, actor_user_id, video.user_id or ""):
        raise HTTPException(status_code=403, detail="interaction unavailable")
    if normalized_key:
        existing = db.query(Comment).filter(
            Comment.author_user_id == actor_user_id,
            Comment.idempotency_key == normalized_key,
        ).one_or_none()
        if existing is not None:
            if existing.video_id != video_id or existing.body != body:
                raise HTTPException(status_code=409, detail="idempotency key payload mismatch")
            return comment_out(db, settings, existing)
    _enforce_comment_rate(db, actor_user_id)
    root = None
    if root_id:
        root = db.get(Comment, root_id)
        if (
            root is None
            or root.video_id != video_id
            or root.root_comment_id is not None
            or root.moderation_status == "hidden"
        ):
            raise HTTPException(status_code=404, detail="comment not found")
        if users_blocked_between(db, actor_user_id, root.author_user_id):
            raise HTTPException(status_code=403, detail="interaction unavailable")
    now = _now()
    row = Comment(
        id=f"cmt_{secrets.token_urlsafe(18)}",
        video_id=video_id,
        author_user_id=actor_user_id,
        root_comment_id=root.id if root else None,
        reply_to_user_id=root.author_user_id if root else None,
        body=body,
        is_seed=seed,
        idempotency_key=normalized_key,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    if seed:
        video.seed_comment_count = max(0, video.seed_comment_count) + 1
    else:
        video.comment_count = max(0, video.comment_count) + 1
    if root:
        root.reply_count = max(0, root.reply_count) + 1
    if not seed:
        _notify(
            db,
            recipient_user_id=root.author_user_id if root else video.user_id,
            actor_user_id=actor_user_id,
            kind="reply" if root else "comment",
            video_id=video_id,
            comment_id=row.id,
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if normalized_key:
            existing = db.query(Comment).filter(
                Comment.author_user_id == actor_user_id,
                Comment.idempotency_key == normalized_key,
            ).one_or_none()
            if existing is not None:
                return comment_out(db, settings, existing)
        raise
    return comment_out(db, settings, row)
