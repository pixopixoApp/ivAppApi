from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class SocialCapabilities(BaseModel):
    creator_profiles: bool = True
    video_likes: bool = True
    comments: bool = True
    notifications: bool = True
    web_immersive_feed: bool = True


class EngagementSummary(BaseModel):
    unique_player_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    viewer_liked: bool = False


class CreatorSocialState(BaseModel):
    follower_count: int = 0
    viewer_following: bool = False


class SocialState(BaseModel):
    videos: dict[str, EngagementSummary] = Field(default_factory=dict)
    creators: dict[str, CreatorSocialState] = Field(default_factory=dict)


class FollowUserOut(BaseModel):
    user_id: str
    nickname: str = ""
    handle: str = ""
    avatar_url: str = ""
    created_at: str


class FollowUserPage(BaseModel):
    items: list[FollowUserOut] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class CreatorTopicOut(BaseModel):
    id: str
    name: str


class CreatorExternalLinkOut(BaseModel):
    label: str = ""
    url: str
    position: int = 0


class CreatorProfile(BaseModel):
    user_id: str
    nickname: str
    handle: str = ""
    share_url: str = ""
    avatar_url: str
    bio: str
    background_url: str = ""
    background_mobile_url: str = ""
    background_desktop_url: str = ""
    background_focus_x: float = 0.5
    background_focus_y: float = 0.5
    topics: list[CreatorTopicOut] = Field(default_factory=list)
    content_language: str = ""
    external_links: list[CreatorExternalLinkOut] = Field(default_factory=list)
    collaboration_email: str | None = None
    profile_updated_at: str | None = None
    pinned_video_ids: list[str] = Field(default_factory=list)
    work_count: int = 0
    following_count: int = 0
    follower_count: int = 0
    received_like_count: int = 0
    viewer_following: bool = False
    viewer_is_owner: bool = False


class CreatorWork(BaseModel):
    video_id: str
    title: str
    description: str
    thumbnail_url: str = ""
    share_url: str = ""
    interaction_types: list[str] = Field(default_factory=list)
    duration_seconds: float | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    engagement: EngagementSummary
    review_status: str = "approved"
    is_pinned: bool = False
    created_at: str


class CreatorWorkPage(BaseModel):
    items: list[CreatorWork]
    next_cursor: str | None = None
    has_more: bool = False
    total_count: int | None = None


class CommentAuthor(BaseModel):
    user_id: str
    nickname: str
    handle: str = ""
    avatar_url: str = ""


class CommentOut(BaseModel):
    id: str
    video_id: str
    author: CommentAuthor
    body: str
    root_comment_id: str | None = None
    reply_to_user_id: str | None = None
    like_count: int = 0
    reply_count: int = 0
    viewer_liked: bool = False
    can_delete: bool = False
    can_hide: bool = False
    is_deleted: bool = False
    created_at: str


class CommentPage(BaseModel):
    items: list[CommentOut]
    next_cursor: str | None = None
    has_more: bool = False


class CommentCreateRequest(BaseModel):
    body: str = Field(min_length=1, max_length=1120)

    @field_validator("body")
    @classmethod
    def validate_unicode_length(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("comment cannot be empty")
        if len(normalized) > 280:
            raise ValueError("comment must be at most 280 characters")
        return normalized


class CommentReportRequest(BaseModel):
    reason: str = Field(default="harassment", min_length=1, max_length=64)
    details: str = Field(default="", max_length=500)


class LikeMutation(BaseModel):
    active: bool
    like_count: int


class FollowMutation(BaseModel):
    active: bool
    follower_count: int


class NotificationActor(BaseModel):
    user_id: str
    nickname: str
    handle: str = ""
    avatar_url: str = ""


class NotificationOut(BaseModel):
    id: str
    type: Literal["follow", "video_like", "comment", "reply", "comment_like"]
    actor: NotificationActor
    video_id: str | None = None
    comment_id: str | None = None
    read: bool = False
    created_at: str


class NotificationPage(BaseModel):
    items: list[NotificationOut]
    next_cursor: str | None = None
    has_more: bool = False
    unread_count: int = 0


class ReadMutation(BaseModel):
    updated: int
    unread_count: int


class ReconcileResult(BaseModel):
    videos_updated: int
    comments_updated: int


class SocialSeedAccountOut(BaseModel):
    user_id: str
    nickname: str
    avatar_url: str


class SocialSeedAccountPage(BaseModel):
    items: list[SocialSeedAccountOut] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class SocialSeedActorRequest(BaseModel):
    actor_user_id: str = Field(min_length=1, max_length=64)
    batch_id: str = Field(min_length=1, max_length=64)


class SocialSeedEngagedOut(BaseModel):
    """Seed accounts that already acted on one video (for idempotent injection)."""

    video_id: str
    liked_account_ids: list[str] = Field(default_factory=list)
    commented_account_ids: list[str] = Field(default_factory=list)


class SocialSeedCommentRequest(SocialSeedActorRequest):
    body: str = Field(min_length=1, max_length=1120)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("body")
    @classmethod
    def validate_seed_comment(cls, value: str) -> str:
        return CommentCreateRequest.validate_unicode_length(value)


class SocialSeedPreviewUpdate(BaseModel):
    enabled: bool
    updated_by: str = Field(min_length=1, max_length=128)


class SocialSeedPreviewOut(BaseModel):
    enabled: bool
    version: int
    updated_by: str
    updated_at: str
