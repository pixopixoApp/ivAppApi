from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Platform = Literal["ios", "android"]


class AppUpdateCheckRequest(BaseModel):
    platform: Platform
    version: str = Field(min_length=1, max_length=32)
    build: int = Field(ge=0)


class AppUpdateCheckResponse(BaseModel):
    update_available: bool
    force_update: bool
    latest_version: str
    latest_build: int
    minimum_version: str
    minimum_build: int
    store_url: str
    package_name: str
    size_bytes: int
    release_notes: str


class AppVersionUpsertRequest(BaseModel):
    latest_version: str = Field(min_length=1, max_length=32)
    latest_build: int = Field(ge=0)
    minimum_version: str = Field(min_length=1, max_length=32)
    minimum_build: int = Field(ge=0)
    store_url: str = Field(default="", max_length=1024)
    package_name: str = Field(default="", max_length=255)
    size_bytes: int = Field(default=0, ge=0)
    release_notes: str = Field(default="", max_length=10000)
    enabled: bool = True


class AppVersionOut(AppVersionUpsertRequest):
    platform: Platform
    updated_at: str


class AccountDeletionRequest(BaseModel):
    confirm: bool
    verification_code: str | None = Field(default=None, max_length=16)


class AccountDeletionResponse(BaseModel):
    deleted: bool
    deleted_at: str


class CreatorAccessOut(BaseModel):
    granted: bool
    source: str | None = None
    granted_at: str | None = None
    application_status: str | None = None


class InviteRedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class CreatorApplicationRequest(BaseModel):
    message: str = Field(default="", max_length=500)


class CreatorApplicationOut(BaseModel):
    user_id: str
    message: str
    status: str
    created_at: str
    updated_at: str


class InviteCreateRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=100)


class InviteCreateResponse(BaseModel):
    codes: list[str]


class CreatorInviteOut(BaseModel):
    id: int
    code_hint: str
    enabled: bool
    status: Literal["unused", "redeemed", "revoked"]
    redeemed_by_user_id: str | None = None
    redeemed_by_label: str = ""
    redeemed_at: str | None = None
    created_at: str


class CreatorInvitePage(BaseModel):
    items: list[CreatorInviteOut]
    total: int
    limit: int
    offset: int


class InviteRevokeRequest(BaseModel):
    invite_ids: list[int] = Field(min_length=1, max_length=100)


class InviteRevokeResponse(BaseModel):
    revoked_ids: list[int]
    skipped_redeemed_ids: list[int]
    missing_ids: list[int]


class CreatorAccessRevokeResponse(BaseModel):
    user_id: str
    granted: bool
    cancelled_creation_ids: list[str]


class CreatorApplicationDecisionRequest(BaseModel):
    status: Literal["approved", "rejected"]


class CreatorUploadOut(BaseModel):
    upload_id: str
    original_filename: str
    size_bytes: int
    duration_ms: int
    preview_url: str
    created_at: str
    upload_transport: str = "oss"
    normalization_status: str = "pending"
    normalization_progress_percent: int = 0
    normalization_profile: str = "mobile-v1"
    playable_size_bytes: int | None = None
    normalization_error: str = ""


class CreatorCreationRequest(BaseModel):
    upload_id: str = Field(min_length=1, max_length=64)
    brief: str = Field(default="", max_length=1000)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)


class CreatorVersionRequest(BaseModel):
    brief: str = Field(min_length=1, max_length=1000)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)


class CreatorVersionOut(BaseModel):
    version_id: str
    number: int
    request: str
    status: str
    progress_stage: str
    progress_percent: int
    retry_count: int
    preview_url: str | None = None
    runtime_spec: dict[str, Any] | None = None
    runtime_spec_version: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str
    updated_at: str


class CreatorCreationOut(BaseModel):
    creation_id: str
    upload_id: str
    status: str
    progress_stage: str
    progress_percent: int
    retry_count: int
    preview_url: str | None = None
    runtime_spec: dict[str, Any] | None = None
    runtime_spec_version: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    published_video_id: str | None = None
    active_version_id: str | None = None
    versions: list[CreatorVersionOut] = Field(default_factory=list)
    created_at: str
    updated_at: str


class CreatorPublishRequest(BaseModel):
    confirm: bool = Field(description="Must be true after the user reviews the preview")
    version_id: str | None = Field(default=None, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1200)


class CreatorPublishResponse(BaseModel):
    video_id: str
    status: Literal["published", "pending_review"]
    runtime_spec_version: str
    share_url: str
    cdn_status: Literal["ready", "warming", "failed"] = "ready"


class CreatorPublishedMutationOut(BaseModel):
    video_id: str
    deleted: bool
