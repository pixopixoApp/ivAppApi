from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import Settings
from app.impressions import ImpressionUnavailableError, get_impression_store
from app.media_service import media_mode_is_oss
from app.models import (
    AnalyticsLog,
    Comment,
    CommentLike,
    ContentReport,
    CreatorAccessGrant,
    CreatorApplication,
    CreatorCreation,
    CreatorInvite,
    CreatorSourceGeneration,
    CreatorUpload,
    CreatorVersion,
    EmailCode,
    Follow,
    PublishedVideo,
    RecommendCursor,
    SocialNotification,
    SocialRateEvent,
    User,
    UserBlock,
    UserToken,
    VideoLike,
    VideoView,
)
from app.storage import LocalMediaStorage, StorageError


class AccountDeletionUnavailable(RuntimeError):
    """Deletion cannot complete without leaving remote creator data behind."""


def _purge_remote_creations(
    settings: Settings,
    creation_ids: list[str],
    upload_ids: list[str] | None = None,
) -> None:
    if not creation_ids and not upload_ids:
        return
    key = settings.creator_internal_key.strip()
    if not key:
        raise AccountDeletionUnavailable("creator data cleanup is not configured")
    base = settings.ivadmin_base_url.rstrip("/")
    try:
        with httpx.Client(timeout=settings.creator_ivadmin_timeout_seconds) as client:
            for creation_id in creation_ids:
                response = client.delete(
                    f"{base}/internal/v1/mobile-creator/creations/{creation_id}",
                    headers={"X-Creator-Internal-Key": key},
                )
                if response.status_code >= 400:
                    raise AccountDeletionUnavailable(
                        f"creator data cleanup failed with HTTP {response.status_code}"
                    )
            for upload_id in upload_ids or []:
                response = client.delete(
                    f"{base}/internal/v1/mobile-creator/normalizations/owners/creator_upload/{upload_id}",
                    headers={"X-Creator-Internal-Key": key},
                )
                if response.status_code >= 400:
                    raise AccountDeletionUnavailable(
                        f"creator media cleanup failed with HTTP {response.status_code}"
                    )
    except httpx.HTTPError as exc:
        raise AccountDeletionUnavailable("creator data cleanup is temporarily unavailable") from exc


def _safe_public_paths(settings: Settings, video_ids: list[str]) -> list[Path]:
    root = Path(settings.media_root).resolve()
    paths: list[Path] = []
    for video_id in video_ids:
        safe = "".join(char for char in video_id if char.isalnum() or char in "-_")
        if not safe or safe != video_id:
            continue
        paths.extend((root / f"{safe}.mp4", root / safe))
    return paths


def delete_account_data(
    db: Session,
    settings: Settings,
    *,
    user_id: str,
) -> None:
    """Permanently delete one account and data that can identify or recreate it."""
    user = db.get(User, user_id)
    if user is None:
        return
    creation_ids = [
        row.id
        for row in db.query(CreatorCreation.id).filter(CreatorCreation.user_id == user_id).all()
    ]
    upload_rows = db.query(CreatorUpload).filter(CreatorUpload.user_id == user_id).all()
    _purge_remote_creations(settings, creation_ids, [row.id for row in upload_rows])
    video_ids = [
        row.id
        for row in db.query(PublishedVideo.id).filter(PublishedVideo.user_id == user_id).all()
    ]
    tokens = [
        row.token for row in db.query(UserToken.token).filter(UserToken.user_id == user_id).all()
    ]
    public_paths = _safe_public_paths(settings, video_ids)
    avatar_paths = list((Path(settings.media_root) / "avatars").glob(f"{user_id}.*"))

    if tokens or video_ids:
        conditions = []
        if tokens:
            conditions.append(AnalyticsLog.token.in_(tokens))
        if video_ids:
            conditions.append(AnalyticsLog.video_id.in_(video_ids))
        db.query(AnalyticsLog).filter(or_(*conditions)).delete(synchronize_session=False)
    if video_ids:
        db.query(VideoLike).filter(VideoLike.video_id.in_(video_ids)).delete(synchronize_session=False)
        video_comment_ids = [row.id for row in db.query(Comment.id).filter(Comment.video_id.in_(video_ids)).all()]
        if video_comment_ids:
            db.query(CommentLike).filter(CommentLike.comment_id.in_(video_comment_ids)).delete(synchronize_session=False)
            db.query(Comment).filter(Comment.id.in_(video_comment_ids)).delete(synchronize_session=False)
        db.query(SocialNotification).filter(SocialNotification.video_id.in_(video_ids)).delete(synchronize_session=False)
        db.query(VideoView).filter(VideoView.video_id.in_(video_ids)).delete(
            synchronize_session=False
        )
        db.query(ContentReport).filter(
            ContentReport.target_type == "video",
            ContentReport.target_id.in_(video_ids),
        ).delete(synchronize_session=False)
    db.query(VideoView).filter(VideoView.user_id == user_id).delete(synchronize_session=False)
    own_likes = db.query(VideoLike).filter(VideoLike.user_id == user_id).all()
    for like in own_likes:
        video = db.get(PublishedVideo, like.video_id)
        if video is not None:
            if like.is_seed:
                video.seed_like_count = max(0, video.seed_like_count - 1)
            else:
                video.like_count = max(0, video.like_count - 1)
        db.delete(like)
    own_comment_likes = db.query(CommentLike).filter(CommentLike.user_id == user_id).all()
    for like in own_comment_likes:
        comment = db.get(Comment, like.comment_id)
        if comment is not None:
            comment.like_count = max(0, comment.like_count - 1)
        db.delete(like)
    for comment in db.query(Comment).filter(Comment.author_user_id == user_id).all():
        if comment.deleted_at is None and comment.moderation_status == "visible":
            video = db.get(PublishedVideo, comment.video_id)
            if video is not None:
                if comment.is_seed:
                    video.seed_comment_count = max(0, video.seed_comment_count - 1)
                else:
                    video.comment_count = max(0, video.comment_count - 1)
            if comment.root_comment_id:
                root = db.get(Comment, comment.root_comment_id)
                if root is not None:
                    root.reply_count = max(0, root.reply_count - 1)
        comment.body = ""
        comment.moderation_status = "removed"
        comment.deleted_at = comment.deleted_at or datetime.now(timezone.utc)
    db.query(SocialNotification).filter(
        or_(
            SocialNotification.recipient_user_id == user_id,
            SocialNotification.actor_user_id == user_id,
        )
    ).delete(synchronize_session=False)
    db.query(SocialRateEvent).filter(SocialRateEvent.user_id == user_id).delete(
        synchronize_session=False
    )
    db.query(RecommendCursor).filter(RecommendCursor.token == f"feed:user:{user_id}").delete()
    db.query(Follow).filter(
        or_(Follow.follower_user_id == user_id, Follow.followee_user_id == user_id)
    ).delete(synchronize_session=False)
    db.query(UserBlock).filter(
        or_(UserBlock.blocker_user_id == user_id, UserBlock.blocked_user_id == user_id)
    ).delete(synchronize_session=False)
    db.query(ContentReport).filter(
        or_(
            ContentReport.reporter_user_id == user_id,
            ContentReport.target_user_id == user_id,
        )
    ).delete(synchronize_session=False)
    db.query(PublishedVideo).filter(PublishedVideo.user_id == user_id).delete(
        synchronize_session=False
    )
    db.query(CreatorVersion).filter(CreatorVersion.user_id == user_id).delete(
        synchronize_session=False
    )
    db.query(CreatorSourceGeneration).filter(
        CreatorSourceGeneration.user_id == user_id
    ).delete(synchronize_session=False)
    db.query(CreatorCreation).filter(CreatorCreation.user_id == user_id).delete(
        synchronize_session=False
    )
    db.query(CreatorUpload).filter(CreatorUpload.user_id == user_id).delete(
        synchronize_session=False
    )
    db.query(CreatorAccessGrant).filter(CreatorAccessGrant.user_id == user_id).delete()
    db.query(CreatorApplication).filter(CreatorApplication.user_id == user_id).delete()
    for invite in (
        db.query(CreatorInvite).filter(CreatorInvite.assigned_user_id == user_id).all()
    ):
        invite.assigned_user_id = None
        if not invite.redeemed_by_user_id:
            invite.enabled = False
    for invite in (
        db.query(CreatorInvite).filter(CreatorInvite.redeemed_by_user_id == user_id).all()
    ):
        invite.redeemed_by_user_id = f"deleted:{invite.id}"
    db.query(UserToken).filter(UserToken.user_id == user_id).delete()
    if user.provider == "email" and user.subject:
        db.query(EmailCode).filter(EmailCode.email == user.subject.strip().lower()).delete()
    db.delete(user)
    db.commit()

    if not media_mode_is_oss(settings):
        storage = LocalMediaStorage(settings)
        for upload in upload_rows:
            try:
                storage.delete(upload.storage_key)
            except StorageError:
                pass
        for path in public_paths:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        for path in avatar_paths:
            path.unlink(missing_ok=True)
    try:
        get_impression_store().clear_user(user_id=user_id)
    except ImpressionUnavailableError:
        # Redis is a derived recommendation cache; database deletion must still succeed.
        pass
