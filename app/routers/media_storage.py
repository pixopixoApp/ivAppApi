from __future__ import annotations

import hashlib
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth_user import AppUser, require_bearer_user
from app.config import Settings, get_settings
from app.db import get_db
from app.deps import require_publish_key
from app.html_import_service import (
    HtmlImportError,
    archive_local_source,
    inspect_source,
    prepare_local_source,
    prepare_source,
)
from app.media_api import (
    CreatorDirectUploadRequest,
    DirectUploadObjectRequest,
    FinalizedUploadSessionOut,
    FinalizeUploadSessionRequest,
    HtmlImportInspectRequest,
    HtmlImportLocalArchiveRequest,
    HtmlImportLocalPrepareRequest,
    HtmlImportPrepareRequest,
    InternalUploadSessionRequest,
    MediaObjectDownloadOut,
    MediaObjectOut,
    RetireLegacyJsonObjectsRequest,
    UploadSessionOut,
)
from app.media_service import (
    MediaServiceError,
    create_upload_session,
    finalize_upload_session,
)
from app.models import (
    CreatorAccessGrant,
    CreatorUpload,
    HtmlPackage,
    HtmlPackageAsset,
    MediaObject,
    MediaUploadSession,
)
from app.oss_storage import OssStorageError, delete_object, public_url, sign_get_url
from app.schemas_platform import CreatorUploadOut

router = APIRouter(tags=["media-storage"])


@router.post("/internal/v1/html-imports/inspect", dependencies=[Depends(require_publish_key)])
def inspect_html_import(
    payload: HtmlImportInspectRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    try:
        return inspect_source(db, settings, source_object_id=payload.source_object_id)
    except (HtmlImportError, OssStorageError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/internal/v1/html-imports/prepare", dependencies=[Depends(require_publish_key)])
def prepare_html_import(
    payload: HtmlImportPrepareRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    try:
        return prepare_source(
            db, settings, source_object_id=payload.source_object_id, item_id=payload.item_id,
            entry=payload.entry, title=payload.title, description=payload.description,
            user_id=payload.user_id, required_capabilities=payload.required_capabilities,
        )
    except (HtmlImportError, OssStorageError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/internal/v1/html-imports/prepare-local",
    dependencies=[Depends(require_publish_key)],
)
def prepare_local_html_import(
    payload: HtmlImportLocalPrepareRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    try:
        return prepare_local_source(
            db,
            settings,
            import_id=payload.import_id,
            attempt_id=payload.attempt_id,
            source_sha256=payload.source_sha256,
            source_bytes=payload.source_bytes,
            item_id=payload.item_id,
            entry=payload.entry,
            title=payload.title,
            description=payload.description,
            user_id=payload.user_id,
            required_capabilities=payload.required_capabilities,
        )
    except (HtmlImportError, OssStorageError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/internal/v1/html-imports/archive-local",
    dependencies=[Depends(require_publish_key)],
)
def archive_local_html_import(
    payload: HtmlImportLocalArchiveRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    try:
        return archive_local_source(
            db,
            settings,
            import_id=payload.import_id,
            source_sha256=payload.source_sha256,
            source_bytes=payload.source_bytes,
            filename=payload.filename,
        )
    except (HtmlImportError, OssStorageError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _iso(value) -> str:
    return value.isoformat() if value is not None else ""


def _media_object_out(settings: Settings, row: MediaObject) -> MediaObjectOut:
    return MediaObjectOut(
        object_id=row.id,
        purpose=row.purpose,
        origin=row.origin,
        visibility=row.visibility,
        state=row.state,
        object_key=row.object_key,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        public_url=(public_url(settings, row.object_key) if row.visibility == "public" else None),
    )


@router.post(
    "/internal/v1/media/upload-sessions",
    response_model=UploadSessionOut,
    dependencies=[Depends(require_publish_key)],
)
def create_internal_upload_session(
    payload: InternalUploadSessionRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UploadSessionOut:
    if payload.purpose == "html_asset":
        version = str(payload.context.get("version") or "")
        entry_path = str(payload.context.get("entry_path") or "")
        if len(version) != 64 or any(character not in "0123456789abcdef" for character in version):
            raise HTTPException(status_code=400, detail="html version must be lowercase sha256")
        if not entry_path or not any(item.relative_path == entry_path for item in payload.objects):
            raise HTTPException(status_code=400, detail="html entry_path must be present in objects")
    try:
        session_id = None
        if payload.idempotency_key:
            digest = hashlib.sha256(
                f"internal:publish-key:{payload.idempotency_key}".encode()
            ).hexdigest()
            session_id = f"mus_i_{digest[:40]}"
        return create_upload_session(
            db,
            settings,
            actor_type="internal",
            actor_id="publish-key",
            purpose=payload.purpose,
            target_id=payload.target_id,
            context=payload.context,
            objects=payload.objects,
            session_id=session_id,
        )
    except (MediaServiceError, OssStorageError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/internal/v1/media/upload-sessions/{session_id}/finalize",
    response_model=FinalizedUploadSessionOut,
    dependencies=[Depends(require_publish_key)],
)
def finalize_internal_upload_session(
    session_id: str,
    payload: FinalizeUploadSessionRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FinalizedUploadSessionOut:
    declared_session = db.get(MediaUploadSession, session_id)
    if declared_session is not None and declared_session.purpose == "html_asset":
        declared_version = str((declared_session.context or {}).get("version") or "")
        if payload.manifest_hash.strip().lower() != declared_version:
            raise HTTPException(
                status_code=400,
                detail="HTML manifest_hash must match the immutable package version",
            )
    try:
        result = finalize_upload_session(
            db,
            settings,
            session_id=session_id,
            actor_type="internal",
            actor_id="publish-key",
            manifest_hash=payload.manifest_hash,
        )
    except (MediaServiceError, OssStorageError) as exc:
        db.rollback()
        detail = str(exc)
        status = 404 if "not found" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    session = db.get(MediaUploadSession, session_id)
    if session is not None and session.purpose == "html_asset":
        context = session.context or {}
        item_id = session.target_id
        version = str(context.get("version") or "")
        entry_path = str(context.get("entry_path") or "")
        digest = hashlib.sha256(f"{item_id}:{version}".encode()).hexdigest()
        package_id = f"hp_{digest[:40]}"
        package = db.get(HtmlPackage, package_id)
        if package is None:
            media_by_id = {
                item.object_id: db.get(MediaObject, item.object_id)
                for item in result.objects
            }
            if any(media is None for media in media_by_id.values()):
                raise HTTPException(status_code=409, detail="finalized HTML asset is missing")
            entry_object = next(
                (
                    item
                    for item in result.objects
                    if str((media_by_id[item.object_id].extra_json or {}).get("relative_path"))
                    == entry_path  # type: ignore[union-attr]
                ),
                None,
            )
            if entry_object is None or entry_object.content_type not in {
                "text/html",
                "application/xhtml+xml",
            }:
                raise HTTPException(status_code=400, detail="HTML package entry is not text/html")
            package = HtmlPackage(
                id=package_id,
                item_id=item_id,
                version=version,
                entry_path=entry_path,
                state="ready",
                manifest_hash=payload.manifest_hash or version,
                html_url=entry_object.public_url or "",
                upload_session_id=session.id,
                verified_at=session.finalized_at,
            )
            db.add(package)
            db.flush()
            for item in result.objects:
                media = media_by_id[item.object_id]
                db.add(
                    HtmlPackageAsset(
                        package_id=package_id,
                        relative_path=str((media.extra_json or {}).get("relative_path") or ""),  # type: ignore[union-attr]
                        media_object_id=item.object_id,
                    )
                )
            db.commit()
        result.package_id = package_id
    return result


@router.get(
    "/internal/v1/media/objects/{object_id}",
    response_model=MediaObjectOut,
    dependencies=[Depends(require_publish_key)],
)
def get_internal_media_object(
    object_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MediaObjectOut:
    row = db.get(MediaObject, object_id)
    if row is None:
        raise HTTPException(status_code=404, detail="media object not found")
    try:
        return _media_object_out(settings, row)
    except OssStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/internal/v1/media/objects/{object_id}/download-url",
    response_model=MediaObjectDownloadOut,
    dependencies=[Depends(require_publish_key)],
)
def get_internal_media_download_url(
    object_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MediaObjectDownloadOut:
    row = db.get(MediaObject, object_id)
    if row is None or row.state != "ready":
        raise HTTPException(status_code=404, detail="ready media object not found")
    ttl = max(30, min(3600, settings.oss_private_get_ttl_seconds))
    try:
        url = (
            public_url(settings, row.object_key)
            if row.visibility == "public"
            else sign_get_url(
                settings,
                key=row.object_key,
                expires_seconds=ttl,
                filename=row.original_filename,
            )
        )
    except OssStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return MediaObjectDownloadOut(object_id=row.id, url=url, expires_in=ttl)


@router.post(
    "/internal/v1/media/retire-legacy-json",
    dependencies=[Depends(require_publish_key)],
)
def retire_legacy_run_json(
    payload: RetireLegacyJsonObjectsRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, int]:
    """Retire only the audited legacy JSON objects from Run storage V1.

    This endpoint intentionally cannot delete images, videos, HTML packages or
    V2 objects.  The caller has to provide the exact backfill allowlist.
    """
    rows = db.query(MediaObject).filter(MediaObject.id.in_(payload.object_ids)).all()
    if len(rows) != len(set(payload.object_ids)):
        raise HTTPException(status_code=404, detail="one or more media objects were not found")
    retired = 0
    for row in rows:
        is_legacy_json = (
            row.purpose == "admin_artifact"
            and (row.content_type == "application/json" or row.size_bytes == 0)
            and "/private/admin-runs/" in f"/{row.object_key}"
        )
        if not is_legacy_json:
            raise HTTPException(status_code=400, detail="object is not a retireable legacy Run JSON")
        if row.state == "retired":
            continue
        try:
            delete_object(settings, key=row.object_key)
        except OssStorageError as exc:
            db.rollback()
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        row.state = "retired"
        db.add(row)
        retired += 1
    db.commit()
    return {"retired": retired}


@router.post(
    "/api/v1/creator/uploads/init",
    response_model=UploadSessionOut,
    status_code=201,
)
def init_creator_upload(
    payload: CreatorDirectUploadRequest,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UploadSessionOut:
    if db.get(CreatorAccessGrant, user.user_id) is None:
        raise HTTPException(status_code=403, detail="creator access required")
    if payload.size_bytes > settings.creator_video_max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"video exceeds {settings.creator_video_max_bytes} bytes",
        )
    upload_id = f"up_{secrets.token_urlsafe(18)}"
    try:
        return create_upload_session(
            db,
            settings,
            actor_type="user",
            actor_id=user.user_id,
            purpose="creator_video",
            target_id=upload_id,
            context={"upload_id": upload_id},
            objects=[
                DirectUploadObjectRequest(
                    client_ref="video",
                    filename=payload.filename,
                    content_type=payload.content_type,
                    size_bytes=payload.size_bytes,
                    sha256=payload.sha256,
                )
            ],
        )
    except (MediaServiceError, OssStorageError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/api/v1/creator/uploads/{session_id}/finalize",
    response_model=CreatorUploadOut,
    status_code=201,
)
def finalize_creator_upload(
    session_id: str,
    payload: FinalizeUploadSessionRequest,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorUploadOut:
    session = db.get(MediaUploadSession, session_id)
    if session is None or session.actor_type != "user" or session.actor_id != user.user_id:
        raise HTTPException(status_code=404, detail="upload session not found")
    upload_id = str((session.context or {}).get("upload_id") or "")
    existing = db.get(CreatorUpload, upload_id) if upload_id else None
    if existing is not None:
        return CreatorUploadOut(
            upload_id=existing.id,
            original_filename=existing.original_filename,
            size_bytes=existing.size_bytes,
            duration_ms=existing.duration_ms,
            preview_url=f"/api/v1/creator/previews/{existing.id}",
            created_at=_iso(existing.created_at),
        )
    try:
        result = finalize_upload_session(
            db,
            settings,
            session_id=session_id,
            actor_type="user",
            actor_id=user.user_id,
            manifest_hash=payload.manifest_hash,
        )
    except (MediaServiceError, OssStorageError) as exc:
        db.rollback()
        detail = str(exc)
        status = 404 if "not found" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    if len(result.objects) != 1:
        raise HTTPException(status_code=500, detail="creator upload manifest is invalid")
    media = db.get(MediaObject, result.objects[0].object_id)
    if media is None:
        raise HTTPException(status_code=500, detail="creator media object is missing")
    upload_id = str((session.context or {}).get("upload_id") or "")
    duration_ms = int((media.extra_json or {}).get("duration_ms") or 0)
    row = CreatorUpload(
        id=upload_id,
        user_id=user.user_id,
        storage_key=media.object_key,
        media_object_id=media.id,
        original_filename=media.original_filename,
        size_bytes=media.size_bytes,
        duration_ms=duration_ms,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return CreatorUploadOut(
        upload_id=row.id,
        original_filename=row.original_filename,
        size_bytes=row.size_bytes,
        duration_ms=row.duration_ms,
        preview_url=f"/api/v1/creator/previews/{row.id}",
        created_at=_iso(row.created_at),
    )
