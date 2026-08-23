from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class DirectUploadObjectRequest(BaseModel):
    client_ref: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=255)
    relative_path: str | None = Field(default=None, max_length=1024)
    content_type: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=0, le=2 * 1024 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class InternalUploadSessionRequest(BaseModel):
    purpose: Literal[
        "admin_source",
        "admin_artifact",
        "runtime_asset",
        "html_asset",
        "html_import_source",
        "migration_import",
    ]
    target_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)
    context: dict[str, Any] = Field(default_factory=dict)
    objects: list[DirectUploadObjectRequest] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_refs_and_paths(self) -> InternalUploadSessionRequest:
        refs = [item.client_ref for item in self.objects]
        if len(refs) != len(set(refs)):
            raise ValueError("client_ref values must be unique")
        paths = [item.relative_path for item in self.objects if item.relative_path]
        if len(paths) != len(set(paths)):
            raise ValueError("relative_path values must be unique")
        return self


class CreatorDirectUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="video/mp4", min_length=1, max_length=128)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class DirectUploadPolicy(BaseModel):
    object_id: str
    client_ref: str
    method: Literal["POST"] = "POST"
    url: str
    fields: dict[str, str]
    expires_at: str


class UploadSessionOut(BaseModel):
    session_id: str
    purpose: str
    state: str
    expires_at: str
    uploads: list[DirectUploadPolicy]


class FinalizeUploadSessionRequest(BaseModel):
    manifest_hash: str = Field(default="", max_length=64)


class FinalizedMediaObjectOut(BaseModel):
    object_id: str
    client_ref: str
    purpose: str
    state: str
    object_key: str
    content_type: str
    size_bytes: int
    sha256: str
    public_url: str | None = None


class FinalizedUploadSessionOut(BaseModel):
    session_id: str
    purpose: str
    state: Literal["ready"]
    objects: list[FinalizedMediaObjectOut]
    package_id: str | None = None


class MediaObjectOut(BaseModel):
    object_id: str
    purpose: str
    origin: str
    visibility: str
    state: str
    object_key: str
    content_type: str
    size_bytes: int
    sha256: str
    public_url: str | None = None


class MediaObjectDownloadOut(BaseModel):
    object_id: str
    url: str
    expires_in: int


class RetireLegacyJsonObjectsRequest(BaseModel):
    """Narrow internal cleanup contract for V2 Run JSON backfill only."""

    object_ids: list[str] = Field(min_length=1, max_length=500)
    reason: Literal["run_json_backfill_v2"]


class HtmlImportInspectRequest(BaseModel):
    source_object_id: str = Field(min_length=1, max_length=64)


class HtmlImportPrepareRequest(HtmlImportInspectRequest):
    item_id: str = Field(min_length=1, max_length=128)
    entry: str | None = Field(default=None, max_length=1024)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1200)
    user_id: str = Field(min_length=1, max_length=64)
    required_capabilities: list[str] = Field(default_factory=list, max_length=5)


class HtmlImportLocalPrepareRequest(BaseModel):
    import_id: str = Field(pattern=r"^him_[A-Za-z0-9_-]{1,56}$")
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_bytes: int = Field(gt=0, le=2 * 1024 * 1024 * 1024)
    item_id: str = Field(min_length=1, max_length=128)
    entry: str | None = Field(default=None, max_length=1024)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1200)
    user_id: str = Field(min_length=1, max_length=64)
    required_capabilities: list[str] = Field(default_factory=list, max_length=5)


class HtmlImportLocalArchiveRequest(BaseModel):
    import_id: str = Field(pattern=r"^him_[A-Za-z0-9_-]{1,56}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_bytes: int = Field(gt=0, le=2 * 1024 * 1024 * 1024)
    filename: str = Field(min_length=1, max_length=255)


class RuntimePublishAssetRequest(BaseModel):
    role: Literal["single", "clip"]
    media_object_id: str = Field(min_length=1, max_length=64)
    clip_id: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def validate_role(self) -> RuntimePublishAssetRequest:
        if self.role == "single" and self.clip_id:
            raise ValueError("single asset must not have clip_id")
        if self.role == "clip" and not self.clip_id:
            raise ValueError("clip asset requires clip_id")
        return self


class RuntimeObjectPublishRequest(BaseModel):
    video_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    content_mode: Literal["single", "story"] = "single"
    timeline: dict[str, Any] | None = None
    story: dict[str, Any] | None = None
    assets: list[RuntimePublishAssetRequest] = Field(min_length=1, max_length=200)
    title: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=1200)
    feed_weight: int | None = None
    is_tutorial: bool | None = None
    # Optional cover media object id (uploaded via /internal/v1/publish-cover).
    cover_media_object_id: str | None = None
    # Optional source timestamp supplied by ivadmin (runs.created_at). When set,
    # it becomes the published_videos.created_at so admin list ordering is stable.
    created_at: datetime | None = None


class RuntimePreviewRequest(BaseModel):
    """Compile a non-persistent runtime payload for an authenticated admin preview.

    This deliberately mirrors the media/timeline half of publication, but has no
    author, feed or publication fields.  It must never create PublishedVideo or
    PublishedMediaAsset rows.
    """

    preview_id: str = Field(min_length=1, max_length=128)
    content_mode: Literal["single", "story"] = "single"
    timeline: dict[str, Any] | None = None
    story: dict[str, Any] | None = None
    assets: list[RuntimePublishAssetRequest] = Field(min_length=1, max_length=200)
