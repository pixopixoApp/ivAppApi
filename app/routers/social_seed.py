from __future__ import annotations

import base64
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.deps import require_publish_key
from app.models import Comment, User, VideoLike
from app.public_origin import canonicalize_public_url
from app.schemas_social import (
    CommentOut,
    LikeMutation,
    SocialSeedAccountOut,
    SocialSeedAccountPage,
    SocialSeedActorRequest,
    SocialSeedCommentRequest,
    SocialSeedEngagedOut,
    SocialSeedPreviewOut,
    SocialSeedPreviewUpdate,
)
from app.social_seed import (
    SOCIAL_SEED_PURPOSE,
    get_preview_config,
    update_preview_config,
)
from app.social_service import create_comment, mutate_video_like

router = APIRouter(
    prefix="/internal/v1",
    tags=["social-seed"],
    dependencies=[Depends(require_publish_key)],
)


def _encode_cursor(user_id: str) -> str:
    return base64.urlsafe_b64encode(user_id.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> str:
    if not cursor:
        return ""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return base64.urlsafe_b64decode(padded).decode()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid cursor") from None


def _seed_actor(db: Session, actor_user_id: str, batch_id: str) -> User:
    row = db.get(User, actor_user_id.strip())
    if (
        row is None
        or not row.enabled
        or row.source != "admin"
        or row.internal_purpose != SOCIAL_SEED_PURPOSE
        or row.internal_batch != batch_id.strip()
    ):
        raise HTTPException(status_code=403, detail="actor is not in social seed batch")
    return row


@router.get("/social-seed/accounts", response_model=SocialSeedAccountPage)
def list_social_seed_accounts(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    batch_id: Annotated[str, Query(min_length=1, max_length=64)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    enabled_only: bool = True,
) -> SocialSeedAccountPage:
    after = _decode_cursor(cursor)
    query = db.query(User).filter(
        User.source == "admin",
        User.internal_purpose == SOCIAL_SEED_PURPOSE,
        User.internal_batch == batch_id.strip(),
    )
    if enabled_only:
        query = query.filter(User.enabled.is_(True))
    if after:
        query = query.filter(User.user_id > after)
    rows = query.order_by(User.user_id.asc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return SocialSeedAccountPage(
        items=[
            SocialSeedAccountOut(
                user_id=row.user_id,
                nickname=row.nickname or "",
                avatar_url=canonicalize_public_url(settings, row.avatar_url) or "",
            )
            for row in rows
        ],
        next_cursor=_encode_cursor(rows[-1].user_id) if has_more and rows else None,
        has_more=has_more,
    )


@router.put("/social-seed/videos/{video_id}/like", response_model=LikeMutation)
def like_as_social_seed(
    video_id: str,
    payload: SocialSeedActorRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LikeMutation:
    actor = _seed_actor(db, payload.actor_user_id, payload.batch_id)
    return mutate_video_like(
        db, settings, actor_user_id=actor.user_id, video_id=video_id, active=True
    )


@router.delete("/social-seed/videos/{video_id}/like", response_model=LikeMutation)
def unlike_as_social_seed(
    video_id: str,
    payload: SocialSeedActorRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LikeMutation:
    actor = _seed_actor(db, payload.actor_user_id, payload.batch_id)
    return mutate_video_like(
        db, settings, actor_user_id=actor.user_id, video_id=video_id, active=False
    )


@router.post("/social-seed/videos/{video_id}/comments", response_model=CommentOut)
def comment_as_social_seed(
    video_id: str,
    payload: SocialSeedCommentRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CommentOut:
    actor = _seed_actor(db, payload.actor_user_id, payload.batch_id)
    return create_comment(
        db,
        settings,
        actor_user_id=actor.user_id,
        video_id=video_id,
        body=payload.body,
        idempotency_key=payload.idempotency_key,
    )


def _preview_out(row) -> SocialSeedPreviewOut:
    return SocialSeedPreviewOut(
        enabled=bool(row.enabled),
        version=row.version,
        updated_by=row.updated_by,
        updated_at=row.updated_at.isoformat(),
    )


@router.get("/social-seed-preview", response_model=SocialSeedPreviewOut)
def get_social_seed_preview(
    db: Annotated[Session, Depends(get_db)],
) -> SocialSeedPreviewOut:
    row = get_preview_config(db, create=True)
    db.commit()
    return _preview_out(row)


@router.put("/social-seed-preview", response_model=SocialSeedPreviewOut)
def put_social_seed_preview(
    payload: SocialSeedPreviewUpdate,
    db: Annotated[Session, Depends(get_db)],
) -> SocialSeedPreviewOut:
    row = update_preview_config(
        db, enabled=payload.enabled, updated_by=payload.updated_by
    )
    db.commit()
    db.refresh(row)
    return _preview_out(row)


@router.get(
    "/social-seed/videos/{video_id}/engaged",
    response_model=SocialSeedEngagedOut,
)
def list_social_seed_engaged_accounts(
    video_id: str,
    db: Annotated[Session, Depends(get_db)],
    batch_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> SocialSeedEngagedOut:
    """Seed accounts in ``batch_id`` that already liked/commented this video.

    Read-only helper so callers can inject only *new* seed interaction and
    report an accurate added count.
    """
    batch_members = {
        row.user_id
        for row in db.query(User.user_id)
        .filter(
            User.source == "admin",
            User.internal_purpose == SOCIAL_SEED_PURPOSE,
            User.internal_batch == batch_id.strip(),
        )
        .all()
    }
    if not batch_members:
        return SocialSeedEngagedOut(video_id=video_id)

    liked = {
        str(user_id)
        for (user_id,) in db.query(VideoLike.user_id)
        .filter(
            VideoLike.video_id == video_id,
            VideoLike.is_seed.is_(True),
        )
        .all()
    }
    commented = {
        str(user_id)
        for (user_id,) in db.query(Comment.author_user_id)
        .filter(
            Comment.video_id == video_id,
            Comment.is_seed.is_(True),
            Comment.deleted_at.is_(None),
        )
        .all()
    }
    return SocialSeedEngagedOut(
        video_id=video_id,
        liked_account_ids=sorted(liked & batch_members),
        commented_account_ids=sorted(commented & batch_members),
    )
