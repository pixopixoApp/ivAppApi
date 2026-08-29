from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

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


class CreatorGenerationQuotaOut(BaseModel):
    enabled: bool
    limit: int
    used: int
    reserved: int
    remaining: int
    resets_at: str


class CreatorAccessOut(BaseModel):
    granted: bool
    source: str | None = None
    granted_at: str | None = None
    application_status: str | None = None
    application_email: str | None = None
    video_generation: CreatorGenerationQuotaOut | None = None


class InviteRedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class CreatorApplicationRequest(BaseModel):
    email: str = Field(default="", max_length=256)
    message: str = Field(default="", max_length=500)


class CreatorApplicationOut(BaseModel):
    user_id: str
    email: str
    message: str
    status: str
    invite_id: int | None = None
    invite_code_hint: str = ""
    invite_status: Literal["unused", "redeemed", "revoked"] | None = None
    invited_at: str | None = None
    email_sent_at: str | None = None
    last_error: str = ""
    created_at: str
    updated_at: str


class CreatorApplicationInviteRequest(BaseModel):
    user_ids: list[str] = Field(min_length=1, max_length=100)


class CreatorApplicationInviteResult(BaseModel):
    user_id: str
    email: str = ""
    status: Literal["sent", "skipped", "failed"]
    application_status: str = ""
    invite_id: int | None = None
    invite_code_hint: str = ""
    error: str = ""


class CreatorApplicationInviteResponse(BaseModel):
    items: list[CreatorApplicationInviteResult]
    sent_count: int
    skipped_count: int
    failed_count: int


class InviteCreateRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=100)


class InviteCreateResponse(BaseModel):
    codes: list[str]


class CreatorInviteOut(BaseModel):
    id: int
    code_hint: str
    enabled: bool
    status: Literal["unused", "redeemed", "revoked"]
    assigned_user_id: str | None = None
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
    source_mode: Literal["upload", "prompt"] | None = None
    upload_id: str | None = Field(default=None, min_length=1, max_length=64)
    prompt: str = Field(default="", max_length=1000)
    brief: str = Field(default="", max_length=1000)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_source(self) -> CreatorCreationRequest:
        mode = self.source_mode or ("upload" if self.upload_id else "prompt")
        if mode == "upload":
            if not self.upload_id or self.prompt.strip():
                raise ValueError("upload creation requires upload_id and no prompt")
        elif self.upload_id or not self.prompt.strip():
            raise ValueError("prompt creation requires prompt and no upload_id")
        self.source_mode = mode
        return self


class CreatorSourceRegenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000)
    request_id: str = Field(min_length=1, max_length=128)


class CreatorSourceAcceptRequest(BaseModel):
    generation_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=128)


class CreatorSourceGenerationOut(BaseModel):
    generation_id: str
    attempt: int
    original_prompt: str
    prompt_summary: str
    generation_prompt: str
    interaction_brief: str
    preset: dict[str, Any] = Field(default_factory=dict)
    status: str
    progress_stage: str
    progress_percent: int
    provider_task_accepted: bool
    preview_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    expires_at: str
    created_at: str
    updated_at: str


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
    upload_id: str | None
    source_mode: Literal["upload", "prompt"] = "upload"
    source_prompt: str = ""
    source_generation_id: str | None = None
    source_preview_url: str | None = None
    source_generation: CreatorSourceGenerationOut | None = None
    generation_quota: CreatorGenerationQuotaOut | None = None
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
