from __future__ import annotations

import shutil
from pathlib import Path

import httpx
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import Settings
from app.impressions import ImpressionUnavailableError, get_impression_store
from app.media_service import media_mode_is_oss
from app.models import (
    AnalyticsLog,
    ContentReport,
    CreatorAccessGrant,
    CreatorApplication,
    CreatorCreation,
    CreatorInvite,
    CreatorUpload,
    CreatorVersion,
    EmailCode,
    Follow,
    PublishedVideo,
    RecommendCursor,
    User,
    UserBlock,
    UserToken,
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
        db.query(VideoView).filter(VideoView.video_id.in_(video_ids)).delete(
            synchronize_session=False
        )
        db.query(ContentReport).filter(
            ContentReport.target_type == "video",
            ContentReport.target_id.in_(video_ids),
        ).delete(synchronize_session=False)
    db.query(VideoView).filter(VideoView.user_id == user_id).delete(synchronize_session=False)
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
    db.query(CreatorCreation).filter(CreatorCreation.user_id == user_id).delete(
        synchronize_session=False
    )
    db.query(CreatorUpload).filter(CreatorUpload.user_id == user_id).delete(
        synchronize_session=False
    )
    db.query(CreatorAccessGrant).filter(CreatorAccessGrant.user_id == user_id).delete()
    db.query(CreatorApplication).filter(CreatorApplication.user_id == user_id).delete()
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
