from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PublishedVideo(Base):
    __tablename__ = "published_videos"
    __table_args__ = (
        CheckConstraint(
            "(content_type = 'runtime' AND video_url IS NOT NULL AND timeline IS NOT NULL "
            "AND runtime_spec IS NOT NULL AND runtime_spec_version IS NOT NULL "
            "AND html_url IS NULL AND bridge_version IS NULL) OR "
            "(content_type = 'html' AND video_url IS NULL AND timeline IS NULL "
            "AND runtime_spec IS NULL AND runtime_spec_version IS NULL "
            "AND html_url IS NOT NULL AND bridge_version = 1)",
            name="ck_published_videos_content_payload",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    content_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="runtime", index=True
    )
    video_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    timeline: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    # Final App playback payload compiled at publish time.  Feed/detail must only
    # read this value; source ``timeline`` is retained for editing/recompilation.
    runtime_spec: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    runtime_spec_version: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    html_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    bridge_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required_capabilities: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    active_publication_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    html_package_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    content_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="single"
    )  # single | story
    feed_weight: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # A published unit can remain available to internal operations without being exposed by any
    # mobile/public read path. This is distinct from moderation review state.
    distribution_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )
    # Origin and review state are deliberately stored on the published unit rather
    # than inferred from users/runs.  A user may later change identity and an HTML
    # package has no Run, while the feed must make one cheap, authoritative query.
    content_source: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pgc", index=True
    )  # pgc | ugc | manual_upload
    review_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="approved", index=True
    )  # pending | approved | rejected
    reviewed_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    cover_media_object_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_tutorial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 是否已删除（标记删除）：0=未删除，1=已删除。deleted_at 仅记录删除时间。
    is_deleted: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0", index=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class RecommendCursor(Base):
    __tablename__ = "recommend_cursors"

    token: Mapped[str] = mapped_column(String(256), primary_key=True)
    cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AnalyticsLog(Base):
    __tablename__ = "analytics_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(256), nullable=False, default="anonymous")
    data: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class EmailCode(Base):
    __tablename__ = "email_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(
        String(32), nullable=False, default="login", index=True
    )
    code: Mapped[str] = mapped_column(String(6), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class User(Base):
    """一人一种登录：user_id 稳定；provider+subject 为登录身份。"""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_users_provider_subject"),)

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="email", index=True)
    subject: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    nickname: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    avatar_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    avatar_media_object_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    bio: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    birthday: Mapped[str] = mapped_column(
        String(10), nullable=False, default=""
    )  # YYYY-MM-DD；空串表示未设置
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="app", index=True
    )  # app=真实用户；admin=管理后台创建
    deletion_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    scheduled_delete_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class UserToken(Base):
    """登录会话；挂 users.user_id（无数据库外键）。"""

    __tablename__ = "user_tokens"

    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Follow(Base):
    __tablename__ = "follows"
    __table_args__ = (
        UniqueConstraint("follower_user_id", "followee_user_id", name="uq_follows_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    follower_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    followee_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class UserBlock(Base):
    """A durable, bidirectional visibility boundary initiated by one user."""

    __tablename__ = "user_blocks"
    __table_args__ = (
        UniqueConstraint("blocker_user_id", "blocked_user_id", name="uq_user_blocks_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    blocker_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    blocked_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ContentReport(Base):
    """User safety report with an auditable operations decision."""

    __tablename__ = "content_reports"
    __table_args__ = (
        UniqueConstraint(
            "reporter_user_id",
            "target_type",
            "target_id",
            name="uq_content_reports_reporter_target",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    reporter_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    target_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    resolution: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    reviewed_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class VideoView(Base):
    """A distinct authenticated viewer for one published item."""

    __tablename__ = "video_views"
    __table_args__ = (
        UniqueConstraint("video_id", "user_id", name="uq_video_views_video_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    first_viewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class AppVersion(Base):
    """Operations-managed update policy, one active policy per platform."""

    __tablename__ = "app_versions"

    platform: Mapped[str] = mapped_column(String(16), primary_key=True)
    latest_version: Mapped[str] = mapped_column(String(32), nullable=False)
    latest_build: Mapped[int] = mapped_column(Integer, nullable=False)
    minimum_version: Mapped[str] = mapped_column(String(32), nullable=False)
    minimum_build: Mapped[int] = mapped_column(Integer, nullable=False)
    store_url: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    package_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    release_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class CreatorAccessGrant(Base):
    __tablename__ = "creator_access_grants"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    invite_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class CreatorInvite(Base):
    __tablename__ = "creator_invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    code_hint: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    redeemed_by_user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    redeemed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class CreatorApplication(Base):
    __tablename__ = "creator_applications"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class CreatorUpload(Base):
    __tablename__ = "creator_uploads"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    media_object_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class CreatorCreation(Base):
    __tablename__ = "creator_creations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    upload_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    brief: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="queued", index=True
    )
    progress_stage: Mapped[str] = mapped_column(
        String(64), nullable=False, default="validate_video"
    )
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    workflow_run_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    analysis_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    source_timeline: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    runtime_spec: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    runtime_spec_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    error_message: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    published_video_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    active_version_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class CreatorVersion(Base):
    """One immutable creator request/result inside a durable creation session."""

    __tablename__ = "creator_versions"
    __table_args__ = (
        UniqueConstraint("creation_id", "number", name="uq_creator_versions_number"),
        UniqueConstraint("request_id", name="uq_creator_versions_request_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    creation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    brief: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="queued", index=True
    )
    progress_stage: Mapped[str] = mapped_column(
        String(64), nullable=False, default="queued"
    )
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ivadmin_job_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    ivadmin_run_id: Mapped[str] = mapped_column(String(36), nullable=False, default="")
    ivadmin_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source_timeline: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    runtime_spec: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    runtime_spec_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    error_message: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class MediaUploadSession(Base):
    """Short-lived authority; uploaded objects themselves are retained permanently."""

    __tablename__ = "media_upload_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    purpose: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class MediaObject(Base):
    __tablename__ = "media_objects"
    __table_args__ = (
        Index(
            "uq_media_objects_staging_key",
            "staging_key",
            unique=True,
            mysql_length=768,
        ),
        Index(
            "uq_media_objects_object_key",
            "object_key",
            unique=True,
            mysql_length=768,
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    upload_session_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    purpose: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    origin: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="private")
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="declared", index=True)
    staging_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    etag: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    extra_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PublishedMediaAsset(Base):
    __tablename__ = "published_media_assets"
    __table_args__ = (
        UniqueConstraint(
            "video_id",
            "publication_id",
            "role",
            "clip_id",
            name="uq_published_media_asset_slot",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    publication_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    clip_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    media_object_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class HtmlPackage(Base):
    __tablename__ = "html_packages"
    __table_args__ = (
        UniqueConstraint("item_id", "version", name="uq_html_packages_item_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    html_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    upload_session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HtmlPackageAsset(Base):
    __tablename__ = "html_package_assets"
    __table_args__ = (
        Index(
            "uq_html_package_asset_path",
            "package_id",
            "relative_path",
            unique=True,
            mysql_length={"package_id": 64, "relative_path": 640},
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    package_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    media_object_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CdnCacheJob(Base):
    """Durable outbox entry for exact-file CDN prefetch or emergency refresh."""

    __tablename__ = "cdn_cache_jobs"
    __table_args__ = (
        UniqueConstraint("operation", "url_hash", name="uq_cdn_cache_jobs_operation_url"),
        Index("ix_cdn_cache_jobs_ready", "state", "next_attempt_at"),
        CheckConstraint(
            "operation IN ('prefetch', 'refresh')",
            name="ck_cdn_cache_jobs_operation",
        ),
        CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_cdn_cache_jobs_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_task_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    error_message: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
