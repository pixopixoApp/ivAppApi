from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class WebCreatorConfigOut(BaseModel):
    allowed_content_types: list[str]
    max_bytes: int
    max_duration_seconds: int
    supported_transports: list[str]
    preparation_profile: str | None = None
    max_source_bytes: int | None = None
    text_to_video_enabled: bool = False
    daily_generation_quota: int = 3
    generated_duration_seconds: int = 3
    generated_ratio: str = "9:16"
    generated_resolution: str = "720p"


class WebSocialConfigOut(BaseModel):
    creator_profiles: bool = True
    video_likes: bool = True
    comments: bool = True
    notifications: bool = True
    web_immersive_feed: bool = True


class WebConfigOut(BaseModel):
    google_client_id: str
    email_code_ttl_seconds: int
    email_resend_seconds: int
    creator: WebCreatorConfigOut
    social: WebSocialConfigOut = Field(default_factory=WebSocialConfigOut)


class WebProfileOut(BaseModel):
    user_id: str
    provider: str
    email: str
    nickname: str
    avatar_url: str
    bio: str
    following_count: int
    follower_count: int
    work_count: int = 0
    received_like_count: int = 0
    unread_notification_count: int = 0


class WebReferralSnapshotOut(BaseModel):
    config_version: int
    inviter_activation_reward_credits: int
    invitee_registration_reward_credits: int


class WebSessionOut(BaseModel):
    authenticated: bool
    user: WebProfileOut | None = None
    referral: WebReferralSnapshotOut | None = None


class WebAppHandoffOut(BaseModel):
    code: str
    open_uri: str
    expires_at: str


class WebEmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=256)


class WebEmailCodeRequest(WebEmailRequest):
    code: str = Field(min_length=6, max_length=6)
    invite_code: str = Field(default="", max_length=32)


class WebGoogleRequest(BaseModel):
    credential: str = Field(min_length=1, max_length=8192)
    invite_code: str = Field(default="", max_length=32)


class WebCodeSentOut(BaseModel):
    sent: bool
    expires_in_seconds: int
    resend_after_seconds: int


class WebProfileUpdateRequest(BaseModel):
    nickname: str | None = Field(default=None, max_length=64)
    bio: str | None = Field(default=None, max_length=80)


class WebPublicationOut(BaseModel):
    video_id: str
    title: str
    description: str
    media_url: str
    share_url: str
    status: Literal["pending_review", "warming", "live", "rejected", "hidden", "deleted"]
    review_status: str
    cdn_ready: bool
    deleted: bool
    created_at: str
    updated_at: str
    unique_player_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    viewer_liked: bool = False


class WebPublicationPageOut(BaseModel):
    items: list[WebPublicationOut]
    total: int
    limit: int
    offset: int
