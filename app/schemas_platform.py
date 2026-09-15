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
    unlimited: bool = True
    enabled: bool
    limit: int
    used: int
    reserved: int
    remaining: int
    resets_at: str


class CreatorInteractionCapabilityOut(BaseModel):
    type: str
    lifecycle: Literal["discrete", "sustained"]
    capability: Literal["touch", "device_motion", "microphone", "vision"]
    story_enabled: bool


class CreatorInteractionPresetOut(BaseModel):
    id: str
    type: str
    label: str
    instruction: str
    category: Literal["screen", "camera", "device_motion", "microphone"]
    family: str
    variant_group: str | None = None
    lifecycle: Literal["discrete", "sustained"]
    capability: Literal["touch", "device_motion", "microphone", "vision"]
    story_enabled: bool
    minimum_runtime_version: str
    recommended: bool = False


class CreatorCapabilitiesOut(BaseModel):
    creator_contract_version: str = "2"
    ai_source_enabled: bool = False
    branch_story_enabled: bool = False
    ai_source_duration_seconds: int = 3
    ai_ending_duration_seconds: int = 3
    credit_per_generated_second: int = 1
    referral_reward_credits: int = 10
    supported_interactions: list[CreatorInteractionCapabilityOut] = Field(default_factory=list)
    interaction_presets: list[CreatorInteractionPresetOut] = Field(default_factory=list)


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
    original_duration_ms: int | None = None
    was_trimmed: bool = False
    prepared_source_url: str | None = None
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
    defer_analysis: bool = False
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
    defer_analysis: bool = False
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


class CreatorInteractionEdit(BaseModel):
    model_config = {"extra": "forbid"}
    interaction_id: str = Field(min_length=1, max_length=64)
    clip_id: str | None = Field(default=None, max_length=64)
    type: str = Field(min_length=1, max_length=64)
    preset_id: str | None = Field(default=None, min_length=1, max_length=96)
    pinch_direction: Literal["inward", "outward"] | None = None
    rotation_direction: Literal["clockwise", "counterclockwise"] | None = None
    vision_target: str | None = Field(default=None, min_length=1, max_length=64)


class CreatorManualEditRequest(BaseModel):
    model_config = {"extra": "forbid"}
    base_version_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=128)
    edits: list[CreatorInteractionEdit] = Field(min_length=1, max_length=64)
    preview_interactions: list[dict[str, Any]] = Field(min_length=1, max_length=64)
    previewed_paths: list[Literal["B", "C"]] = Field(default_factory=list, max_length=2)


class CreatorVersionOut(BaseModel):
    previewed_paths: list[str] = Field(default_factory=list)
    experience_mode: str = "auto"
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
    manual_edit_options: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    created_at: str
    updated_at: str


class CreatorDraftSummaryOut(BaseModel):
    creation_id: str
    initial_request_id: str | None = None
    title: str
    source_mode: str
    experience_mode: str
    status: str
    progress_stage: str
    progress_percent: int
    duration_ms: int
    updated_at: str


class CreatorDraftPageOut(BaseModel):
    items: list[CreatorDraftSummaryOut]
    total: int
    next_cursor: str | None = None


class CreatorCreationOut(BaseModel):
    experience_mode: str = "auto"
    source_duration_ms: int = 0
    story: dict[str, Any] | None = None
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


class CreatorStoryPlanRequest(BaseModel):
    model_config = {"extra": "forbid"}
    revision: int = Field(default=0, ge=0)
    interaction_type: str = Field(min_length=1, max_length=64)
    interaction_preset_id: str | None = Field(default=None, min_length=1, max_length=96)
    pinch_direction: Literal["inward", "outward"] | None = None
    rotation_direction: Literal["clockwise", "counterclockwise"] | None = None
    vision_target: str | None = Field(default=None, min_length=1, max_length=64)
    success_prompt: str = Field(default="", max_length=1000)
    miss_prompt: str = Field(default="", max_length=1000)
    source_tail_owner: Literal["B", "C"] | None = None
    split_ms: int = Field(default=0, ge=0)


class CreatorStoryGenerationRequest(BaseModel):
    model_config = {"extra": "forbid"}
    revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=100)
    targets: list[Literal["B", "C"]] = Field(min_length=1, max_length=2)
    operation: Literal["generate", "retry", "regenerate"] = "generate"


class CreatorPreviewConfirmationRequest(BaseModel):
    version_id: str = Field(min_length=1, max_length=64)
    path: Literal["B", "C"]


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


class CreditLedgerEntryOut(BaseModel):
    id: str
    kind: str
    amount: int
    note: str
    created_at: str


class CreditBalanceOut(BaseModel):
    balance: int
    entries: list[CreditLedgerEntryOut] = Field(default_factory=list)


class ReferralInviteOut(BaseModel):
    code: str
    url: str
    status: Literal["none", "pending_activation", "activated"] = "none"
