from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import Settings
from app.media_service import MediaServiceError, safe_id
from app.models import MediaObject, PublishedMediaAsset
from app.oss_storage import (
    OssImmutableConflictError,
    OssObjectNotFoundError,
    copy_object,
    head_object,
    object_key,
    public_url,
)


@dataclass(frozen=True)
class RuntimeSourceAsset:
    role: str
    media: MediaObject
    clip_id: str = ""


@dataclass(frozen=True)
class PublishedRuntimeAssets:
    publication_id: str
    urls: dict[str, str]
    media_objects: dict[str, MediaObject]


def runtime_publication_id(
    *,
    video_id: str,
    version: str,
    source_payload: dict,
    assets: list[RuntimeSourceAsset],
) -> str:
    material = {
        "video_id": video_id,
        "version": version,
        "source": source_payload,
        "assets": [
            {
                "role": item.role,
                "clip_id": item.clip_id,
                "object_id": item.media.id,
                "sha256": item.media.sha256,
            }
            for item in sorted(assets, key=lambda value: (value.role, value.clip_id))
        ],
    }
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"pub_{digest[:32]}"


def _public_object_id(key: str) -> str:
    return f"mop_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:40]}"


def _copy_once(
    settings: Settings,
    *,
    source: MediaObject,
    target_key: str,
) -> None:
    try:
        existing = head_object(settings, key=target_key)
    except OssObjectNotFoundError:
        existing = None
    if existing is not None:
        if existing.size_bytes != source.size_bytes:
            raise MediaServiceError("immutable publication target has a different size")
        remote_sha = existing.headers.get("x-oss-meta-sha256", "").lower()
        if not source.sha256 or remote_sha != source.sha256:
            raise MediaServiceError("immutable publication target has a different checksum")
        return
    try:
        copy_object(
            settings,
            source_key=source.object_key,
            target_key=target_key,
            content_type=source.content_type,
            public=True,
            immutable=True,
            extra_headers={
                "x-oss-meta-pixo-source-object-id": source.id,
                "x-oss-meta-sha256": source.sha256,
            },
        )
    except OssImmutableConflictError:
        # A concurrent idempotent retry may have won the immutable create.
        existing = head_object(settings, key=target_key)
        remote_sha = existing.headers.get("x-oss-meta-sha256", "").lower()
        if existing.size_bytes != source.size_bytes or remote_sha != source.sha256:
            raise MediaServiceError(
                "immutable publication target has a different identity"
            )


def publish_runtime_assets(
    db: Session,
    settings: Settings,
    *,
    video_id: str,
    version: str,
    source_payload: dict,
    assets: list[RuntimeSourceAsset],
) -> PublishedRuntimeAssets:
    item_id = safe_id(video_id, label="video_id")
    if not assets:
        raise MediaServiceError("runtime publication has no media assets")
    for item in assets:
        if item.role not in {"single", "clip"}:
            raise MediaServiceError("invalid runtime asset role")
        if item.media.purpose not in {
            # ivadmin owns these private Run sources.  They are accepted only
            # by the authenticated internal publish endpoint and are copied to
            # an immutable public runtime object below; no server-side video
            # download/re-upload hop is necessary.
            "admin_source",
            "admin_artifact",
            "runtime_asset",
            "runtime_public",
            "creator_video",
            "legacy_creator_source",
            "legacy_runtime_single",
            "legacy_runtime_clip",
        }:
            raise MediaServiceError("media object is not a runtime publication asset")
        if item.media.state != "ready":
            raise MediaServiceError("runtime source media is not ready")
        if item.media.content_type not in {"video/mp4", "application/mp4"}:
            raise MediaServiceError("runtime source media must be MP4")
        if item.role == "clip":
            safe_id(item.clip_id, label="clip_id")
    publication_id = runtime_publication_id(
        video_id=item_id,
        version=version,
        source_payload=source_payload,
        assets=assets,
    )
    urls: dict[str, str] = {}
    outputs: dict[str, MediaObject] = {}
    for item in assets:
        slot = item.clip_id if item.role == "clip" else "single"
        target_key = (
            object_key(
                settings,
                "public",
                "runtime",
                item_id,
                publication_id,
                "clips",
                f"{item.clip_id}.mp4",
            )
            if item.role == "clip"
            else object_key(
                settings,
                "public",
                "runtime",
                item_id,
                publication_id,
                "single.mp4",
            )
        )
        _copy_once(settings, source=item.media, target_key=target_key)
        public_media_id = _public_object_id(target_key)
        public_media = db.get(MediaObject, public_media_id)
        if public_media is None:
            public_media = MediaObject(
                id=public_media_id,
                upload_session_id=item.media.upload_session_id,
                purpose="runtime_public",
                origin="server_copy",
                visibility="public",
                state="ready",
                staging_key=target_key,
                object_key=target_key,
                original_filename=(f"{item.clip_id}.mp4" if item.role == "clip" else "single.mp4"),
                content_type="video/mp4",
                size_bytes=item.media.size_bytes,
                sha256=item.media.sha256,
                etag="",
                extra_json={"source_media_object_id": item.media.id},
                verified_at=item.media.verified_at,
            )
            db.add(public_media)
            db.flush()
        elif (
            public_media.object_key != target_key
            or public_media.size_bytes != item.media.size_bytes
            or public_media.sha256 != item.media.sha256
            or public_media.state != "ready"
            or public_media.visibility != "public"
        ):
            raise MediaServiceError("public media database identity mismatch")
        binding = (
            db.query(PublishedMediaAsset)
            .filter(
                PublishedMediaAsset.video_id == item_id,
                PublishedMediaAsset.publication_id == publication_id,
                PublishedMediaAsset.role == item.role,
                PublishedMediaAsset.clip_id == item.clip_id,
            )
            .one_or_none()
        )
        if binding is None:
            db.add(
                PublishedMediaAsset(
                    video_id=item_id,
                    publication_id=publication_id,
                    version=version,
                    role=item.role,
                    clip_id=item.clip_id,
                    media_object_id=public_media.id,
                )
            )
        elif binding.media_object_id != public_media.id:
            raise MediaServiceError("publication binding identity mismatch")
        urls[slot] = public_url(settings, target_key)
        outputs[slot] = public_media
    return PublishedRuntimeAssets(
        publication_id=publication_id,
        urls=urls,
        media_objects=outputs,
    )


def load_published_runtime_urls(
    db: Session,
    settings: Settings,
    *,
    video_id: str,
    publication_id: str,
) -> dict[str, str]:
    """Resolve the immutable public URL for every persisted publication slot."""
    bindings = (
        db.query(PublishedMediaAsset)
        .filter(
            PublishedMediaAsset.video_id == video_id,
            PublishedMediaAsset.publication_id == publication_id,
        )
        .order_by(PublishedMediaAsset.id.asc())
        .all()
    )
    if not bindings:
        raise MediaServiceError("runtime publication has no media bindings")
    media_by_id = {
        row.id: row
        for row in db.query(MediaObject)
        .filter(MediaObject.id.in_([item.media_object_id for item in bindings]))
        .all()
    }
    urls: dict[str, str] = {}
    for binding in bindings:
        media = media_by_id.get(binding.media_object_id)
        if (
            media is None
            or media.state != "ready"
            or media.visibility != "public"
        ):
            raise MediaServiceError("runtime publication media binding is not ready")
        slot = binding.clip_id if binding.role == "clip" else "single"
        if not slot:
            raise MediaServiceError("runtime publication media slot is invalid")
        urls[slot] = public_url(settings, media.object_key)
    return urls
