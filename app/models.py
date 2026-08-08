from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PublishedVideo(Base):
    __tablename__ = "published_videos"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    video_url: Mapped[str] = mapped_column(Text, nullable=False)
    timeline: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    content_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="single"
    )  # single | story
    feed_weight: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_tutorial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
