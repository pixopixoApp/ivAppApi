from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class WebCreatorConfigOut(BaseModel):
    allowed_content_types: list[str]
    max_bytes: int
    max_duration_seconds: int
    supported_transports: list[str]
    text_to_video_enabled: bool = False
    daily_generation_quota: int = 3
    generated_duration_seconds: int = 10
    generated_ratio: str = "9:16"
    generated_resolution: str = "720p"


class WebConfigOut(BaseModel):
    google_client_id: str
    email_code_ttl_seconds: int
    email_resend_seconds: int
    creator: WebCreatorConfigOut


class WebProfileOut(BaseModel):
    user_id: str
    provider: str
    email: str
    nickname: str
    avatar_url: str
    bio: str
    following_count: int
    follower_count: int


class WebSessionOut(BaseModel):
    authenticated: bool
    user: WebProfileOut | None = None


class WebEmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=256)


class WebEmailCodeRequest(WebEmailRequest):
    code: str = Field(min_length=6, max_length=6)


class WebGoogleRequest(BaseModel):
    credential: str = Field(min_length=1, max_length=8192)


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


class WebPublicationPageOut(BaseModel):
    items: list[WebPublicationOut]
    total: int
    limit: int
    offset: int
