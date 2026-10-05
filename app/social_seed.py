from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import PublishedVideo, SocialSeedPreviewConfig, User

SOCIAL_SEED_PURPOSE = "social_seed"
DEFAULT_SOCIAL_SEED_BATCH = "prelaunch-v1"


def get_preview_config(db: Session, *, create: bool = False) -> SocialSeedPreviewConfig:
    row = db.get(SocialSeedPreviewConfig, 1)
    if row is None:
        row = SocialSeedPreviewConfig(
            id=1,
            enabled=False,
            version=1,
            updated_by="system",
            updated_at=datetime.now(timezone.utc),
        )
        if create:
            db.add(row)
            db.flush()
    return row


def preview_enabled(db: Session) -> bool:
    row = db.get(SocialSeedPreviewConfig, 1)
    return bool(row and row.enabled)


def update_preview_config(
    db: Session, *, enabled: bool, updated_by: str
) -> SocialSeedPreviewConfig:
    row = get_preview_config(db, create=True)
    row.enabled = bool(enabled)
    row.version = max(1, int(row.version or 0) + 1)
    row.updated_by = updated_by.strip() or "system"
    row.updated_at = datetime.now(timezone.utc)
    db.flush()
    return row


def is_seed_user(user: User | None) -> bool:
    return bool(user and user.internal_purpose == SOCIAL_SEED_PURPOSE)


def displayed_like_count(video: PublishedVideo, enabled: bool) -> int:
    real = max(0, int(video.like_count or 0))
    seed = max(0, int(video.seed_like_count or 0))
    return real + seed if enabled else real


def displayed_comment_count(video: PublishedVideo, enabled: bool) -> int:
    real = max(0, int(video.comment_count or 0))
    seed = max(0, int(video.seed_comment_count or 0))
    return real + seed if enabled else real
