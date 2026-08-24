from __future__ import annotations

import hashlib
import os
import secrets
from datetime import timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
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
from app.media_cache import (
    LocalMediaCacheError,
    commit_staged_upload,
    ensure_free_space_floor,
    ensure_upload_capacity,
    local_media_uri,
    object_lock,
    upload_staging_path,
)
from app.media_service import (
    MediaServiceError,
    create_upload_session,
    finalize_upload_session,
    now_utc,
    safe_filename,
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
from app.video_probe import VideoProbeError, probe_video

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


def _creator_upload_out(row: CreatorUpload) -> CreatorUploadOut:
    progress = {
        "pending": 0,
        "submitted": 5,
        "normalizing": 40,
        "backing_up": 85,
        "ready": 100,
        "failed": 100,
    }.get(row.normalization_status or "pending", 0)
    return CreatorUploadOut(
        upload_id=row.id,
        original_filename=row.original_filename,
        size_bytes=row.size_bytes,
        duration_ms=row.duration_ms,
        preview_url=f"/api/v1/creator/previews/{row.id}",
        created_at=_iso(row.created_at),
        upload_transport=row.upload_transport or "oss",
        normalization_status=row.normalization_status or "pending",
        normalization_progress_percent=progress,
        normalization_profile=row.normalization_profile or "mobile-v1",
        playable_size_bytes=row.playable_size_bytes,
        normalization_error=row.normalization_error or "",
    )


def _creator_local_session(
    db: Session,
    session_id: str,
    user: AppUser,
) -> MediaUploadSession:
    session = db.get(MediaUploadSession, session_id)
    if (
        session is None
        or session.actor_type != "user"
        or session.actor_id != user.user_id
        or session.purpose != "creator_video"
        or (session.context or {}).get("transport") != "local-resumable-v1"
    ):
        raise HTTPException(status_code=404, detail="upload session not found")
    expiry = session.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry < now_utc() and session.state != "ready":
        raise HTTPException(status_code=410, detail="upload session expired")
    return session


@router.get(
    "/api/v1/creator/uploads/{upload_id}",
    response_model=CreatorUploadOut,
)
def get_creator_upload(
    upload_id: str,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorUploadOut:
    row = db.get(CreatorUpload, upload_id)
    if row is None or row.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="upload not found")
    return _creator_upload_out(row)


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
    if (
        settings.creator_local_upload_enabled
        and "local-resumable-v1" in payload.supported_transports
    ):
        try:
            filename = safe_filename(payload.filename)
            ensure_upload_capacity(settings, payload.size_bytes)
        except MediaServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LocalMediaCacheError as exc:
            raise HTTPException(status_code=507, detail=str(exc)) from exc
        session_id = f"cus_{secrets.token_urlsafe(18)}"
        expires_at = now_utc() + timedelta(
            seconds=max(300, min(86400, settings.creator_local_upload_ttl_seconds))
        )
        session = MediaUploadSession(
            id=session_id,
            actor_type="user",
            actor_id=user.user_id,
            purpose="creator_video",
            state="pending",
            target_id=upload_id,
            manifest_hash="",
            context={
                "transport": "local-resumable-v1",
                "upload_id": upload_id,
                "filename": filename,
                "content_type": payload.content_type,
                "size_bytes": int(payload.size_bytes),
                "sha256": payload.sha256.lower(),
                "offset": 0,
            },
            expires_at=expires_at,
        )
        db.add(session)
        db.commit()
        return UploadSessionOut(
            session_id=session.id,
            purpose="creator_video",
            state="pending",
            expires_at=_iso(expires_at),
            transport="local-resumable-v1",
            uploads=[],
            upload={
                "method": "PATCH",
                "url": f"/api/v1/creator/uploads/{session.id}/source",
                "chunk_size": max(
                    1024 * 1024,
                    min(32 * 1024 * 1024, settings.creator_local_upload_chunk_bytes),
                ),
                "offset": 0,
                "expires_at": _iso(expires_at),
            },
        )
    if not settings.creator_legacy_oss_upload_enabled:
        raise HTTPException(status_code=426, detail="please update the app to upload videos")
    try:
        result = create_upload_session(
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
        return result.model_copy(update={"transport": "oss"})
    except (MediaServiceError, OssStorageError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.head("/api/v1/creator/uploads/{session_id}/source", status_code=204)
def inspect_creator_local_upload(
    session_id: str,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    session = _creator_local_session(db, session_id, user)
    context = dict(session.context or {})
    return Response(
        status_code=204,
        headers={
            "Upload-Offset": str(int(context.get("offset") or 0)),
            "Upload-Length": str(int(context.get("size_bytes") or 0)),
            "Upload-State": session.state,
        },
    )


@router.patch("/api/v1/creator/uploads/{session_id}/source", status_code=204)
async def upload_creator_source_locally(
    session_id: str,
    request: Request,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    session = _creator_local_session(db, session_id, user)
    if session.state == "ready":
        raise HTTPException(status_code=409, detail="upload session already finalized")
    try:
        requested_offset = int(request.headers.get("upload-offset", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid Upload-Offset") from exc
    raw_length = request.headers.get("content-length")
    try:
        content_length = int(raw_length or "")
    except ValueError as exc:
        raise HTTPException(status_code=411, detail="Content-Length is required") from exc
    chunk_limit = max(1024 * 1024, min(32 * 1024 * 1024, settings.creator_local_upload_chunk_bytes))
    if content_length <= 0 or content_length > chunk_limit:
        raise HTTPException(status_code=413, detail="upload chunk exceeds the configured limit")

    staging = upload_staging_path(settings, session.id)
    with object_lock(settings, f"upload:{session.id}"):
        db.refresh(session)
        context = dict(session.context or {})
        expected_size = int(context.get("size_bytes") or 0)
        committed_offset = int(context.get("offset") or 0)
        actual_size = staging.stat().st_size if staging.is_file() else 0
        if actual_size != committed_offset:
            if actual_size > committed_offset:
                with staging.open("r+b") as stream:
                    stream.truncate(committed_offset)
                    stream.flush()
                    os.fsync(stream.fileno())
            else:
                committed_offset = actual_size
                context["offset"] = actual_size
        if requested_offset != committed_offset:
            return Response(status_code=409, headers={"Upload-Offset": str(committed_offset)})
        if committed_offset + content_length > expected_size:
            raise HTTPException(status_code=413, detail="upload exceeds the reserved size")

        staging.parent.mkdir(parents=True, exist_ok=True)
        start_offset = committed_offset
        total = committed_offset
        try:
            with staging.open("ab") as output:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > start_offset + content_length or total > expected_size:
                        raise HTTPException(status_code=413, detail="upload chunk size mismatch")
                    output.write(chunk)
                if total != start_offset + content_length:
                    raise HTTPException(status_code=400, detail="upload chunk was incomplete")
                output.flush()
                os.fsync(output.fileno())
            ensure_free_space_floor(settings)
        except BaseException:
            if staging.exists():
                with staging.open("r+b") as output:
                    output.truncate(start_offset)
                    output.flush()
                    os.fsync(output.fileno())
            raise
        context["offset"] = total
        session.context = context
        session.state = "uploaded" if total == expected_size else "uploading"
        db.add(session)
        db.commit()
    return Response(status_code=204, headers={"Upload-Offset": str(total)})


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
        return _creator_upload_out(existing)
    if (session.context or {}).get("transport") == "local-resumable-v1":
        session = _creator_local_session(db, session_id, user)
        context = dict(session.context or {})
        expected_size = int(context.get("size_bytes") or 0)
        if session.state != "uploaded" or int(context.get("offset") or 0) != expected_size:
            raise HTTPException(status_code=409, detail="local upload is incomplete")
        staging = upload_staging_path(settings, session.id)
        try:
            metadata = probe_video(staging)
            if metadata.duration_ms > settings.creator_video_max_duration_seconds * 1000:
                raise LocalMediaCacheError(
                    f"video must be {settings.creator_video_max_duration_seconds} seconds or shorter"
                )
            commit_staged_upload(
                settings,
                staging,
                sha256=str(context.get("sha256") or ""),
                size_bytes=expected_size,
            )
        except (LocalMediaCacheError, VideoProbeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        digest = str(context["sha256"]).lower()
        row = CreatorUpload(
            id=upload_id,
            user_id=user.user_id,
            storage_key=local_media_uri(digest),
            media_object_id=None,
            source_local_uri=local_media_uri(digest),
            source_sha256=digest,
            upload_transport="local-resumable-v1",
            normalization_status="pending",
            normalization_profile="mobile-v1",
            original_filename=str(context.get("filename") or "video.mp4"),
            size_bytes=expected_size,
            duration_ms=metadata.duration_ms,
        )
        session.state = "ready"
        session.finalized_at = now_utc()
        db.add_all([row, session])
        db.commit()
        db.refresh(row)
        return _creator_upload_out(row)
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
        source_sha256=media.sha256,
        upload_transport="oss",
        normalization_status="pending",
        normalization_profile="mobile-v1",
        original_filename=media.original_filename,
        size_bytes=media.size_bytes,
        duration_ms=duration_ms,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _creator_upload_out(row)
