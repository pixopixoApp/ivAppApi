from __future__ import annotations

import hashlib
import html
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.account_deletion import AccountDeletionUnavailable, delete_account_data
from app.auth_user import AppUser, require_bearer_user
from app.cdn_cache import enqueue_prefetch
from app.config import Settings, get_settings
from app.db import get_db
from app.deps import require_publish_key
from app.media_service import MediaServiceError, media_mode_is_oss
from app.models import (
    AppVersion,
    CreatorAccessGrant,
    CreatorApplication,
    CreatorCreation,
    CreatorInvite,
    CreatorUpload,
    CreatorVersion,
    MediaObject,
    PublishedVideo,
    User,
)
from app.oss_storage import OssStorageError, sign_get_url
from app.protocol_video import (
    RUNTIME_SPEC_VERSION,
    RuntimeSpecError,
    compile_runtime_spec,
)
from app.public_origin import canonicalize_public_payload, canonicalize_public_url
from app.publication_service import RuntimeSourceAsset, publish_runtime_assets
from app.schemas_platform import (
    AccountDeletionRequest,
    AccountDeletionResponse,
    AppUpdateCheckRequest,
    AppUpdateCheckResponse,
    AppVersionOut,
    AppVersionUpsertRequest,
    CreatorAccessOut,
    CreatorAccessRevokeResponse,
    CreatorApplicationDecisionRequest,
    CreatorApplicationOut,
    CreatorApplicationRequest,
    CreatorCreationOut,
    CreatorCreationRequest,
    CreatorInviteOut,
    CreatorInvitePage,
    CreatorPublishedMutationOut,
    CreatorPublishRequest,
    CreatorPublishResponse,
    CreatorUploadOut,
    CreatorVersionOut,
    CreatorVersionRequest,
    InviteCreateRequest,
    InviteCreateResponse,
    InviteRedeemRequest,
    InviteRevokeRequest,
    InviteRevokeResponse,
    Platform,
)
from app.storage import LocalMediaStorage, StorageError
from app.verification_codes import PURPOSE_DEACTIVATE, find_valid_code
from app.video_probe import VideoProbeError, probe_video

public_router = APIRouter(prefix="/api/v1", tags=["platform"])
creator_router = APIRouter(prefix="/api/v1/creator", tags=["creator"])
operations_router = APIRouter(prefix="/internal/v1", tags=["admin"])

_ACTIVE_CREATION_STATUSES = ("queued", "running")
_INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _normalize_invite(raw: str) -> str:
    return "".join(char for char in raw.strip().upper() if char not in " -")


def _invite_hash(raw: str) -> str:
    return hashlib.sha256(_normalize_invite(raw).encode("utf-8")).hexdigest()


def _new_invite_code() -> str:
    compact = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(12))
    return f"{compact[:4]}-{compact[4:8]}-{compact[8:]}"


def _access_grant(db: Session, user_id: str) -> CreatorAccessGrant | None:
    return db.get(CreatorAccessGrant, user_id)


def _require_creator(db: Session, user: AppUser) -> CreatorAccessGrant:
    grant = _access_grant(db, user.user_id)
    if grant is None:
        raise HTTPException(status_code=403, detail="creator access required")
    return grant


def _lock_creator(db: Session, user_id: str) -> CreatorAccessGrant:
    """Serialize per-user creation state transitions on the permanent grant row."""
    grant = (
        db.query(CreatorAccessGrant)
        .filter(CreatorAccessGrant.user_id == user_id)
        .with_for_update()
        .one_or_none()
    )
    if grant is None:
        raise HTTPException(status_code=403, detail="creator access required")
    return grant


@public_router.delete("/account", response_model=AccountDeletionResponse)
def delete_account(
    payload: AccountDeletionRequest,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AccountDeletionResponse:
    if payload.confirm is not True:
        raise HTTPException(status_code=400, detail="explicit confirmation required")
    now = _now()
    if user.provider == "email":
        code = (payload.verification_code or "").strip()
        if not code.isdigit() or len(code) != 6:
            raise HTTPException(status_code=400, detail="valid email verification code required")
        code_row = find_valid_code(
            db,
            email=user.subject.strip().lower(),
            code=code,
            purpose=PURPOSE_DEACTIVATE,
            now=now,
        )
        if code_row is None:
            raise HTTPException(status_code=400, detail="verification code is invalid or expired")
        code_row.used_at = now
        db.flush()
    try:
        delete_account_data(db, settings, user_id=user.user_id)
    except AccountDeletionUnavailable as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return AccountDeletionResponse(deleted=True, deleted_at=now.isoformat())


def _version_out(
    row: CreatorVersion,
    *,
    upload_id: str,
    settings: Settings | None = None,
) -> CreatorVersionOut:
    active_settings = settings or get_settings()
    ready = row.status in ("ready", "published")
    return CreatorVersionOut(
        version_id=row.id,
        number=row.number,
        request=row.brief,
        status=row.status,
        progress_stage=row.progress_stage,
        progress_percent=row.progress_percent,
        retry_count=row.retry_count,
        preview_url=(f"/api/v1/creator/previews/{upload_id}" if ready else None),
        runtime_spec=(
            canonicalize_public_payload(active_settings, row.runtime_spec)
            if ready
            else None
        ),
        runtime_spec_version=row.runtime_spec_version if ready else None,
        error_code=row.error_code or None,
        error_message=row.error_message or None,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _creation_versions(db: Session, creation_id: str) -> list[CreatorVersion]:
    return (
        db.query(CreatorVersion)
        .filter(CreatorVersion.creation_id == creation_id)
        .order_by(CreatorVersion.number.asc())
        .all()
    )


def _creation_out(
    db: Session,
    row: CreatorCreation,
    settings: Settings | None = None,
) -> CreatorCreationOut:
    active_settings = settings or get_settings()
    versions = _creation_versions(db, row.id)
    ready = row.status in ("ready", "published")
    return CreatorCreationOut(
        creation_id=row.id,
        upload_id=row.upload_id,
        status=row.status,
        progress_stage=row.progress_stage,
        progress_percent=row.progress_percent,
        retry_count=row.retry_count,
        preview_url=(f"/api/v1/creator/previews/{row.upload_id}" if ready else None),
        runtime_spec=(
            canonicalize_public_payload(active_settings, row.runtime_spec)
            if ready
            else None
        ),
        runtime_spec_version=row.runtime_spec_version if ready else None,
        error_code=row.error_code or None,
        error_message=row.error_message or None,
        published_video_id=row.published_video_id,
        active_version_id=row.active_version_id,
        versions=[
            _version_out(item, upload_id=row.upload_id, settings=active_settings)
            for item in versions
        ],
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _share_url(settings: Settings, video_id: str) -> str:
    relative = f"/api/v1/share/{video_id}"
    return f"{settings.public_share_base_url.rstrip('/')}{relative}" if settings.public_share_base_url else relative


@public_router.post(
    "/app-updates/check",
    response_model=AppUpdateCheckResponse,
    summary="Check the current App update policy",
)
def check_app_update(
    payload: AppUpdateCheckRequest,
    db: Annotated[Session, Depends(get_db)],
) -> AppUpdateCheckResponse:
    policy = db.get(AppVersion, payload.platform)
    if policy is None or not policy.enabled:
        return AppUpdateCheckResponse(
            update_available=False,
            force_update=False,
            latest_version=payload.version,
            latest_build=payload.build,
            minimum_version=payload.version,
            minimum_build=payload.build,
            store_url="",
            package_name="",
            size_bytes=0,
            release_notes="",
        )
    return AppUpdateCheckResponse(
        update_available=payload.build < policy.latest_build,
        force_update=payload.build < policy.minimum_build,
        latest_version=policy.latest_version,
        latest_build=policy.latest_build,
        minimum_version=policy.minimum_version,
        minimum_build=policy.minimum_build,
        store_url=policy.store_url,
        package_name=policy.package_name,
        size_bytes=policy.size_bytes,
        release_notes=policy.release_notes,
    )


@operations_router.put(
    "/app-versions/{platform}",
    response_model=AppVersionOut,
    dependencies=[Depends(require_publish_key)],
)
def upsert_app_version(
    platform: Platform,
    payload: AppVersionUpsertRequest,
    db: Annotated[Session, Depends(get_db)],
) -> AppVersionOut:
    if payload.minimum_build > payload.latest_build:
        raise HTTPException(status_code=400, detail="minimum_build cannot exceed latest_build")
    row = db.get(AppVersion, platform)
    if row is None:
        row = AppVersion(platform=platform, **payload.model_dump())
        db.add(row)
    else:
        for key, value in payload.model_dump().items():
            setattr(row, key, value)
        row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return AppVersionOut(
        platform=platform,
        **payload.model_dump(),
        updated_at=_iso(row.updated_at),
    )


@operations_router.get(
    "/app-versions/{platform}",
    response_model=AppVersionOut,
    dependencies=[Depends(require_publish_key)],
)
def get_app_version(
    platform: Platform,
    db: Annotated[Session, Depends(get_db)],
) -> AppVersionOut:
    row = db.get(AppVersion, platform)
    if row is None:
        raise HTTPException(status_code=404, detail="app version policy not found")
    return AppVersionOut(
        platform=platform,
        latest_version=row.latest_version,
        latest_build=row.latest_build,
        minimum_version=row.minimum_version,
        minimum_build=row.minimum_build,
        store_url=row.store_url,
        package_name=row.package_name,
        size_bytes=row.size_bytes,
        release_notes=row.release_notes,
        enabled=row.enabled,
        updated_at=_iso(row.updated_at),
    )


@creator_router.get("/access", response_model=CreatorAccessOut)
def get_creator_access(
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorAccessOut:
    grant = _access_grant(db, user.user_id)
    application = db.get(CreatorApplication, user.user_id)
    return CreatorAccessOut(
        granted=grant is not None,
        source=grant.source if grant else None,
        granted_at=_iso(grant.granted_at) if grant else None,
        application_status=application.status if application else None,
    )


@creator_router.post("/invites/redeem", response_model=CreatorAccessOut)
def redeem_creator_invite(
    payload: InviteRedeemRequest,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorAccessOut:
    existing = _access_grant(db, user.user_id)
    if existing is not None:
        return CreatorAccessOut(
            granted=True,
            source=existing.source,
            granted_at=_iso(existing.granted_at),
        )
    normalized = _normalize_invite(payload.code)
    if not normalized:
        raise HTTPException(status_code=400, detail="invalid invite code")
    invite = (
        db.query(CreatorInvite)
        .filter(CreatorInvite.code_hash == _invite_hash(normalized))
        .with_for_update()
        .one_or_none()
    )
    if invite is None or not invite.enabled or invite.redeemed_by_user_id:
        db.rollback()
        raise HTTPException(status_code=400, detail="invite code is invalid or already used")
    now = _now()
    invite.redeemed_by_user_id = user.user_id
    invite.redeemed_at = now
    grant = CreatorAccessGrant(
        user_id=user.user_id,
        source="invite",
        invite_id=invite.id,
        granted_at=now,
    )
    db.add(grant)
    db.commit()
    return CreatorAccessOut(granted=True, source="invite", granted_at=_iso(now))


@creator_router.post("/applications", response_model=CreatorApplicationOut)
def apply_for_creator_access(
    payload: CreatorApplicationRequest,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorApplicationOut:
    if _access_grant(db, user.user_id) is not None:
        raise HTTPException(status_code=409, detail="creator access already granted")
    now = _now()
    row = db.get(CreatorApplication, user.user_id)
    if row is None:
        row = CreatorApplication(
            user_id=user.user_id,
            message=payload.message.strip(),
            status="pending",
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    elif row.status == "pending":
        row.message = payload.message.strip()
        row.updated_at = now
    elif row.status == "rejected":
        row.message = payload.message.strip()
        row.status = "pending"
        row.updated_at = now
    else:
        raise HTTPException(status_code=409, detail="application already approved")
    db.commit()
    db.refresh(row)
    return CreatorApplicationOut(
        user_id=row.user_id,
        message=row.message,
        status=row.status,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


@operations_router.post(
    "/creator/invites",
    response_model=InviteCreateResponse,
    dependencies=[Depends(require_publish_key)],
)
def create_creator_invites(
    payload: InviteCreateRequest,
    db: Annotated[Session, Depends(get_db)],
) -> InviteCreateResponse:
    codes: list[str] = []
    while len(codes) < payload.count:
        code = _new_invite_code()
        digest = _invite_hash(code)
        if db.query(CreatorInvite).filter(CreatorInvite.code_hash == digest).first():
            continue
        db.add(
            CreatorInvite(
                code_hash=digest,
                code_hint=_normalize_invite(code)[-4:],
                enabled=True,
            )
        )
        codes.append(code)
    db.commit()
    return InviteCreateResponse(codes=codes)


def _invite_status(row: CreatorInvite) -> Literal["unused", "redeemed", "revoked"]:
    if row.redeemed_by_user_id:
        return "redeemed"
    return "unused" if row.enabled else "revoked"


def _invite_out(db: Session, row: CreatorInvite) -> CreatorInviteOut:
    redeemed_by = db.get(User, row.redeemed_by_user_id) if row.redeemed_by_user_id else None
    label = ""
    if redeemed_by is not None:
        label = redeemed_by.nickname or redeemed_by.subject or redeemed_by.user_id
    return CreatorInviteOut(
        id=row.id,
        code_hint=row.code_hint,
        enabled=row.enabled,
        status=_invite_status(row),
        redeemed_by_user_id=row.redeemed_by_user_id,
        redeemed_by_label=label,
        redeemed_at=_iso(row.redeemed_at) or None,
        created_at=_iso(row.created_at),
    )


@operations_router.get(
    "/creator/invites",
    response_model=CreatorInvitePage,
    dependencies=[Depends(require_publish_key)],
)
def list_creator_invites(
    db: Annotated[Session, Depends(get_db)],
    status: Literal["unused", "redeemed", "revoked"] | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> CreatorInvitePage:
    if limit < 1 or limit > 200 or offset < 0:
        raise HTTPException(status_code=400, detail="invalid pagination")
    query = db.query(CreatorInvite)
    if status == "unused":
        query = query.filter(CreatorInvite.enabled.is_(True), CreatorInvite.redeemed_by_user_id.is_(None))
    elif status == "redeemed":
        query = query.filter(CreatorInvite.redeemed_by_user_id.is_not(None))
    elif status == "revoked":
        query = query.filter(CreatorInvite.enabled.is_(False), CreatorInvite.redeemed_by_user_id.is_(None))
    term = (q or "").strip()
    if term:
        query = query.filter(
            or_(
                CreatorInvite.code_hint.contains(term.upper()),
                CreatorInvite.redeemed_by_user_id.contains(term),
            )
        )
    total = query.count()
    rows = query.order_by(CreatorInvite.created_at.desc(), CreatorInvite.id.desc()).offset(offset).limit(limit).all()
    return CreatorInvitePage(
        items=[_invite_out(db, row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@operations_router.post(
    "/creator/invites/revoke",
    response_model=InviteRevokeResponse,
    dependencies=[Depends(require_publish_key)],
)
def revoke_creator_invites(
    payload: InviteRevokeRequest,
    db: Annotated[Session, Depends(get_db)],
) -> InviteRevokeResponse:
    requested = list(dict.fromkeys(payload.invite_ids))
    rows = (
        db.query(CreatorInvite)
        .filter(CreatorInvite.id.in_(requested))
        .with_for_update()
        .all()
    )
    by_id = {row.id: row for row in rows}
    revoked: list[int] = []
    skipped: list[int] = []
    for invite_id in requested:
        row = by_id.get(invite_id)
        if row is None:
            continue
        if row.redeemed_by_user_id:
            skipped.append(invite_id)
            continue
        row.enabled = False
        revoked.append(invite_id)
    db.commit()
    return InviteRevokeResponse(
        revoked_ids=revoked,
        skipped_redeemed_ids=skipped,
        missing_ids=[invite_id for invite_id in requested if invite_id not in by_id],
    )


@operations_router.post(
    "/creator/access/{user_id}/revoke",
    response_model=CreatorAccessRevokeResponse,
    dependencies=[Depends(require_publish_key)],
)
def revoke_creator_access(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> CreatorAccessRevokeResponse:
    uid = user_id.strip()
    if db.get(User, uid) is None:
        raise HTTPException(status_code=404, detail="user not found")
    db.query(CreatorAccessGrant).filter(CreatorAccessGrant.user_id == uid).delete()
    active = (
        db.query(CreatorCreation)
        .filter(
            CreatorCreation.user_id == uid,
            CreatorCreation.status.in_(_ACTIVE_CREATION_STATUSES),
        )
        .all()
    )
    creation_ids = [row.id for row in active]
    for row in active:
        row.cancel_requested = True
    if creation_ids:
        for version in (
            db.query(CreatorVersion)
            .filter(
                CreatorVersion.creation_id.in_(creation_ids),
                CreatorVersion.status.in_(_ACTIVE_CREATION_STATUSES),
            )
            .all()
        ):
            version.cancel_requested = True
    db.commit()
    return CreatorAccessRevokeResponse(
        user_id=uid,
        granted=False,
        cancelled_creation_ids=creation_ids,
    )


@operations_router.get(
    "/creator/applications",
    response_model=list[CreatorApplicationOut],
    dependencies=[Depends(require_publish_key)],
)
def list_creator_applications(
    db: Annotated[Session, Depends(get_db)],
    status: Literal["pending", "approved", "rejected"] | None = None,
) -> list[CreatorApplicationOut]:
    query = db.query(CreatorApplication)
    if status:
        query = query.filter(CreatorApplication.status == status)
    rows = query.order_by(CreatorApplication.updated_at.desc()).limit(500).all()
    return [
        CreatorApplicationOut(
            user_id=row.user_id,
            message=row.message,
            status=row.status,
            created_at=_iso(row.created_at),
            updated_at=_iso(row.updated_at),
        )
        for row in rows
    ]


@operations_router.post(
    "/creator/applications/{user_id}/decision",
    response_model=CreatorApplicationOut,
    dependencies=[Depends(require_publish_key)],
)
def decide_creator_application(
    user_id: str,
    payload: CreatorApplicationDecisionRequest,
    db: Annotated[Session, Depends(get_db)],
) -> CreatorApplicationOut:
    row = db.get(CreatorApplication, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="creator application not found")
    row.status = payload.status
    row.updated_at = _now()
    if payload.status == "approved" and _access_grant(db, user_id) is None:
        db.add(
            CreatorAccessGrant(
                user_id=user_id,
                source="application",
                granted_at=row.updated_at,
            )
        )
    db.commit()
    db.refresh(row)
    return CreatorApplicationOut(
        user_id=row.user_id,
        message=row.message,
        status=row.status,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


@creator_router.post("/uploads", response_model=CreatorUploadOut, status_code=201)
async def upload_creator_video(
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    file: Annotated[UploadFile, File()],
) -> CreatorUploadOut:
    _require_creator(db, user)
    if media_mode_is_oss(settings):
        raise HTTPException(
            status_code=410,
            detail="multipart creator upload is disabled; use /api/v1/creator/uploads/init",
        )
    filename = Path(file.filename or "").name
    content_type = (file.content_type or "").lower()
    if not filename.lower().endswith(".mp4") and content_type not in (
        "video/mp4",
        "application/mp4",
    ):
        raise HTTPException(status_code=400, detail="an MP4 video is required")

    upload_id = f"up_{secrets.token_urlsafe(18)}"
    storage = LocalMediaStorage(settings)
    key = storage.upload_key(user_id=user.user_id, upload_id=upload_id)
    try:
        size = await storage.save_upload(
            file,
            key=key,
            max_bytes=settings.creator_video_max_bytes,
        )
        metadata = probe_video(storage.resolve(key))
        if metadata.duration_ms > settings.creator_video_max_duration_seconds * 1000:
            raise StorageError(
                f"video must be {settings.creator_video_max_duration_seconds} seconds or shorter"
            )
    except (StorageError, VideoProbeError) as exc:
        storage.delete(key)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    now = _now()
    row = CreatorUpload(
        id=upload_id,
        user_id=user.user_id,
        storage_key=key,
        original_filename=filename or "video.mp4",
        size_bytes=size,
        duration_ms=metadata.duration_ms,
        created_at=now,
    )
    db.add(row)
    try:
        db.commit()
    except Exception:
        db.rollback()
        storage.delete(key)
        raise
    return CreatorUploadOut(
        upload_id=row.id,
        original_filename=row.original_filename,
        size_bytes=row.size_bytes,
        duration_ms=row.duration_ms,
        preview_url=f"/api/v1/creator/previews/{row.id}",
        created_at=_iso(row.created_at),
    )


@public_router.get("/creator/previews/{upload_id}", response_class=FileResponse)
def public_creator_preview(
    upload_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    # upload_id contains 144 bits of randomness and acts as a read-only preview
    # capability. It is never listed publicly and cannot mutate creator state.
    row = db.get(CreatorUpload, upload_id)
    if row is None:
        raise HTTPException(status_code=404, detail="preview not found")
    if row.media_object_id:
        media = db.get(MediaObject, row.media_object_id)
        if media is None or media.state != "ready":
            raise HTTPException(status_code=404, detail="preview not found")
        try:
            return RedirectResponse(
                sign_get_url(
                    settings,
                    key=media.object_key,
                    expires_seconds=settings.oss_private_get_ttl_seconds,
                    filename=row.original_filename,
                ),
                status_code=307,
                headers={"Cache-Control": "no-store"},
            )
        except OssStorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    if media_mode_is_oss(settings) and not settings.media_read_fallback_local:
        raise HTTPException(status_code=404, detail="preview not found")
    try:
        path = LocalMediaStorage(settings).resolve(row.storage_key)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail="preview not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="preview not found")
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"Cache-Control": "private, max-age=300"},
    )


@creator_router.get("/uploads/{upload_id}/media", response_class=FileResponse)
def preview_creator_upload(
    upload_id: str,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    row = db.get(CreatorUpload, upload_id)
    if row is None or row.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="upload not found")
    if row.media_object_id:
        media = db.get(MediaObject, row.media_object_id)
        if media is None or media.state != "ready":
            raise HTTPException(status_code=404, detail="upload media missing")
        try:
            return RedirectResponse(
                sign_get_url(
                    settings,
                    key=media.object_key,
                    expires_seconds=settings.oss_private_get_ttl_seconds,
                    filename=row.original_filename,
                ),
                status_code=307,
                headers={"Cache-Control": "no-store"},
            )
        except OssStorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    if media_mode_is_oss(settings) and not settings.media_read_fallback_local:
        raise HTTPException(status_code=404, detail="upload media missing")
    try:
        path = LocalMediaStorage(settings).resolve(row.storage_key)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail="upload not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="upload media missing")
    return FileResponse(path, media_type="video/mp4", filename=row.original_filename)


@creator_router.post("/creations", response_model=CreatorCreationOut, status_code=202)
def create_interactive_video(
    payload: CreatorCreationRequest,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorCreationOut:
    _lock_creator(db, user.user_id)
    if payload.request_id:
        existing_version = (
            db.query(CreatorVersion)
            .filter(CreatorVersion.request_id == payload.request_id.strip())
            .first()
        )
        if existing_version is not None:
            existing = _owned_creation(db, existing_version.creation_id, user.user_id)
            return _creation_out(db, existing)
    upload = db.get(CreatorUpload, payload.upload_id)
    if upload is None or upload.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="upload not found")
    active = (
        db.query(CreatorCreation)
        .filter(
            CreatorCreation.user_id == user.user_id,
            CreatorCreation.status.in_(_ACTIVE_CREATION_STATUSES),
        )
        .first()
    )
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail={"message": "one creation is already in progress", "creation_id": active.id},
        )
    now = _now()
    creation_id = f"cr_{secrets.token_urlsafe(18)}"
    version_id = f"cv_{secrets.token_urlsafe(18)}"
    row = CreatorCreation(
        id=creation_id,
        user_id=user.user_id,
        upload_id=upload.id,
        brief=payload.brief.strip(),
        status="queued",
        progress_stage="queued",
        progress_percent=0,
        active_version_id=version_id,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.add(
        CreatorVersion(
            id=version_id,
            creation_id=creation_id,
            user_id=user.user_id,
            number=1,
            request_id=payload.request_id.strip() if payload.request_id else version_id,
            brief=payload.brief.strip(),
            status="queued",
            progress_stage="queued",
            progress_percent=0,
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()
    db.refresh(row)
    return _creation_out(db, row)


def _owned_creation(db: Session, creation_id: str, user_id: str) -> CreatorCreation:
    row = db.get(CreatorCreation, creation_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="creation not found")
    return row


def _owned_version(db: Session, version_id: str, user_id: str) -> CreatorVersion:
    row = db.get(CreatorVersion, version_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="creator version not found")
    return row


@creator_router.get("/creations/active", response_model=CreatorCreationOut | None)
def get_active_creation(
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorCreationOut | None:
    _require_creator(db, user)
    row = (
        db.query(CreatorCreation)
        .filter(
            CreatorCreation.user_id == user.user_id,
            CreatorCreation.status.notin_(("published", "pending_review", "rejected", "deleted", "abandoned", "cancelled")),
        )
        .order_by(CreatorCreation.updated_at.desc())
        .first()
    )
    return _creation_out(db, row) if row is not None else None


@creator_router.get("/creations/{creation_id}", response_model=CreatorCreationOut)
def get_creation(
    creation_id: str,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorCreationOut:
    _require_creator(db, user)
    return _creation_out(db, _owned_creation(db, creation_id, user.user_id))


@creator_router.post(
    "/creations/{creation_id}/versions",
    response_model=CreatorCreationOut,
    status_code=202,
)
def create_version(
    creation_id: str,
    payload: CreatorVersionRequest,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorCreationOut:
    _lock_creator(db, user.user_id)
    creation = _owned_creation(db, creation_id, user.user_id)
    if creation.status == "published":
        raise HTTPException(status_code=409, detail="published creations cannot be changed")
    if payload.request_id:
        existing = (
            db.query(CreatorVersion)
            .filter(CreatorVersion.request_id == payload.request_id.strip())
            .first()
        )
        if existing is not None:
            if existing.creation_id != creation.id or existing.user_id != user.user_id:
                raise HTTPException(status_code=409, detail="request id is already in use")
            return _creation_out(db, creation)
    next_number = int(
        db.query(func.max(CreatorVersion.number))
        .filter(CreatorVersion.creation_id == creation.id)
        .scalar()
        or 0
    ) + 1
    version_id = f"cv_{secrets.token_urlsafe(18)}"
    now = _now()
    db.add(
        CreatorVersion(
            id=version_id,
            creation_id=creation.id,
            user_id=user.user_id,
            number=next_number,
            request_id=payload.request_id.strip() if payload.request_id else version_id,
            brief=payload.brief.strip(),
            status="queued",
            progress_stage="queued",
            progress_percent=0,
            created_at=now,
            updated_at=now,
        )
    )
    creation.active_version_id = version_id
    creation.status = "queued"
    creation.progress_stage = "queued"
    creation.progress_percent = 0
    creation.brief = payload.brief.strip()
    creation.error_code = ""
    creation.error_message = ""
    creation.updated_at = now
    db.commit()
    db.refresh(creation)
    return _creation_out(db, creation)


def _cancel_version_row(db: Session, version: CreatorVersion) -> None:
    if version.status == "queued" and not version.ivadmin_job_id:
        version.status = "cancelled"
        version.progress_stage = "cancelled"
        version.error_code = "CANCELLED"
        version.error_message = "Creation was cancelled."
    elif version.status in ("queued", "running"):
        version.cancel_requested = True
    elif version.status not in ("cancelled", "failed"):
        raise HTTPException(status_code=409, detail="version can no longer be cancelled")
    version.updated_at = _now()
    db.add(version)


@creator_router.post("/versions/{version_id}/cancel", response_model=CreatorCreationOut)
def cancel_version(
    version_id: str,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorCreationOut:
    _require_creator(db, user)
    version = _owned_version(db, version_id, user.user_id)
    creation = _owned_creation(db, version.creation_id, user.user_id)
    _cancel_version_row(db, version)
    creation.status = "cancelled" if version.status == "cancelled" else version.status
    creation.progress_stage = version.progress_stage
    creation.updated_at = _now()
    db.commit()
    db.refresh(creation)
    return _creation_out(db, creation)


@creator_router.post("/creations/{creation_id}/cancel", response_model=CreatorCreationOut)
def cancel_creation(
    creation_id: str,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorCreationOut:
    _require_creator(db, user)
    row = _owned_creation(db, creation_id, user.user_id)
    versions = _creation_versions(db, row.id)
    active = next((item for item in versions if item.status in _ACTIVE_CREATION_STATUSES), None)
    if active is None:
        raise HTTPException(status_code=409, detail="creation has no cancellable version")
    _cancel_version_row(db, active)
    row.status = "cancelled" if active.status == "cancelled" else active.status
    row.progress_stage = active.progress_stage
    row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return _creation_out(db, row)


@creator_router.delete("/creations/{creation_id}", response_model=CreatorCreationOut)
def abandon_creation(
    creation_id: str,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorCreationOut:
    _require_creator(db, user)
    row = _owned_creation(db, creation_id, user.user_id)
    if row.status == "published":
        raise HTTPException(status_code=409, detail="published creations cannot be abandoned")
    for version in _creation_versions(db, row.id):
        if version.status in _ACTIVE_CREATION_STATUSES:
            if version.status == "queued" and not version.ivadmin_job_id:
                version.status = "cancelled"
                version.progress_stage = "cancelled"
            else:
                version.cancel_requested = True
            version.updated_at = _now()
            db.add(version)
    row.status = "abandoned"
    row.progress_stage = "abandoned"
    row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return _creation_out(db, row)


def _retry_version_row(db: Session, version: CreatorVersion) -> None:
    if version.status not in ("failed", "cancelled"):
        raise HTTPException(status_code=409, detail="only failed or cancelled versions can retry")
    version.status = "queued"
    version.progress_stage = "queued"
    version.progress_percent = 0
    version.cancel_requested = False
    version.retry_count += 1
    version.ivadmin_job_id = ""
    version.ivadmin_run_id = ""
    version.ivadmin_version = ""
    version.source_timeline = None
    version.runtime_spec = None
    version.runtime_spec_version = None
    version.error_code = ""
    version.error_message = ""
    version.updated_at = _now()
    db.add(version)


@creator_router.post("/versions/{version_id}/retry", response_model=CreatorCreationOut, status_code=202)
def retry_version(
    version_id: str,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorCreationOut:
    _lock_creator(db, user.user_id)
    version = _owned_version(db, version_id, user.user_id)
    creation = _owned_creation(db, version.creation_id, user.user_id)
    _retry_version_row(db, version)
    creation.active_version_id = version.id
    creation.status = "queued"
    creation.progress_stage = "queued"
    creation.progress_percent = 0
    creation.error_code = ""
    creation.error_message = ""
    creation.updated_at = _now()
    db.commit()
    db.refresh(creation)
    return _creation_out(db, creation)


@creator_router.post("/creations/{creation_id}/retry", response_model=CreatorCreationOut, status_code=202)
def retry_creation(
    creation_id: str,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorCreationOut:
    _lock_creator(db, user.user_id)
    row = _owned_creation(db, creation_id, user.user_id)
    versions = _creation_versions(db, row.id)
    target = next((item for item in reversed(versions) if item.status in ("failed", "cancelled")), None)
    if target is None:
        raise HTTPException(status_code=409, detail="creation has no retryable version")
    _retry_version_row(db, target)
    row.active_version_id = target.id
    row.status = "queued"
    row.progress_stage = "queued"
    row.progress_percent = 0
    row.cancel_requested = False
    row.retry_count = target.retry_count
    row.error_code = ""
    row.error_message = ""
    row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return _creation_out(db, row)


@creator_router.post(
    "/creations/{creation_id}/publish",
    response_model=CreatorPublishResponse,
)
def publish_creation(
    creation_id: str,
    payload: CreatorPublishRequest,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorPublishResponse:
    _require_creator(db, user)
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="preview confirmation is required")
    row = _owned_creation(db, creation_id, user.user_id)
    if row.status == "published" and row.published_video_id:
        return CreatorPublishResponse(
            video_id=row.published_video_id,
            status="published",
            runtime_spec_version=row.runtime_spec_version or RUNTIME_SPEC_VERSION,
            share_url=_share_url(settings, row.published_video_id),
        )
    versions = _creation_versions(db, row.id)
    version = (
        next((item for item in versions if item.id == payload.version_id), None)
        if payload.version_id
        else next((item for item in reversed(versions) if item.status == "ready"), None)
    )
    source_timeline = version.source_timeline if version is not None else row.source_timeline
    if version is not None and version.status != "ready":
        raise HTTPException(status_code=409, detail="selected version is not ready")
    if not isinstance(source_timeline, dict):
        raise HTTPException(status_code=409, detail="creation is not ready to publish")
    upload = db.get(CreatorUpload, row.upload_id)
    if upload is None or upload.user_id != user.user_id:
        raise HTTPException(status_code=409, detail="source upload is missing")
    if db.get(PublishedVideo, row.id) is not None:
        raise HTTPException(status_code=409, detail="published video id already exists")

    final_url = f"/media/{row.id}.mp4"
    publication_id = None
    prefetch_urls: list[str] = []
    if media_mode_is_oss(settings):
        if not upload.media_object_id:
            raise HTTPException(
                status_code=409,
                detail="source upload has not been migrated to OSS",
            )
        source_media = db.get(MediaObject, upload.media_object_id)
        if source_media is None:
            raise HTTPException(status_code=409, detail="source media object is missing")
        try:
            published_assets = publish_runtime_assets(
                db,
                settings,
                video_id=row.id,
                version=(
                    version.ivadmin_version
                    if version and version.ivadmin_version
                    else f"creator-{version.number if version else 1}"
                ),
                source_payload=source_timeline,
                assets=[RuntimeSourceAsset(role="single", media=source_media)],
            )
            final_url = published_assets.urls["single"]
            publication_id = published_assets.publication_id
            prefetch_urls = list(published_assets.urls.values())
        except (MediaServiceError, OssStorageError) as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=f"preview cannot be published: {exc}") from exc
    try:
        runtime_spec = compile_runtime_spec(
            item_id=row.id,
            content_mode="single",
            source=source_timeline,
            video_url=final_url,
        )
    except RuntimeSpecError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"preview cannot be published: {exc}") from exc

    storage = LocalMediaStorage(settings) if not media_mode_is_oss(settings) else None
    destination = None
    now = _now()
    try:
        copied_url = final_url
        if storage is not None:
            destination, copied_url = storage.publish_copy(
                source_key=upload.storage_key,
                item_id=row.id,
            )
        published = PublishedVideo(
            id=row.id,
            content_type="runtime",
            video_url=copied_url,
            timeline=source_timeline,
            runtime_spec=runtime_spec,
            runtime_spec_version=RUNTIME_SPEC_VERSION,
            html_url=None,
            bridge_version=None,
            required_capabilities=[],
            active_publication_id=publication_id,
            version=(version.ivadmin_version if version and version.ivadmin_version else f"creator-{version.number if version else 1}"),
            title=payload.title.strip(),
            description=payload.description.strip(),
            user_id=user.user_id,
            content_mode="single",
            feed_weight=0,
            content_source="ugc",
            review_status="pending",
            is_tutorial=False,
            deleted_at=None,
            created_at=now,
            updated_at=now,
        )
        db.add(published)
        row.status = "pending_review"
        row.progress_stage = "pending_review"
        row.progress_percent = 100
        row.published_video_id = row.id
        row.runtime_spec = runtime_spec
        row.runtime_spec_version = RUNTIME_SPEC_VERSION
        if version is not None:
            version.status = "published"
            version.progress_stage = "published"
            version.runtime_spec = runtime_spec
            version.runtime_spec_version = RUNTIME_SPEC_VERSION
            version.updated_at = now
            db.add(version)
        row.updated_at = now
        enqueue_prefetch(db, settings, prefetch_urls)
        db.commit()
    except Exception:
        db.rollback()
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise
    return CreatorPublishResponse(
        video_id=row.id,
        status="pending_review",
        runtime_spec_version=RUNTIME_SPEC_VERSION,
        share_url=_share_url(settings, row.id),
    )


@creator_router.delete(
    "/published/{video_id}",
    response_model=CreatorPublishedMutationOut,
)
def delete_published_video(
    video_id: str,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorPublishedMutationOut:
    _require_creator(db, user)
    video = db.get(PublishedVideo, video_id)
    if video is None or video.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="published video not found")
    if not bool(video.is_deleted) or video.deleted_at is None:
        video.is_deleted = 1
        video.deleted_at = _now()
        creation = db.get(CreatorCreation, video_id)
        if creation is not None and creation.user_id == user.user_id:
            creation.status = "deleted"
            creation.progress_stage = "deleted"
            creation.updated_at = _now()
            db.add(creation)
        db.add(video)
        db.commit()
    return CreatorPublishedMutationOut(video_id=video_id, deleted=True)


@creator_router.post(
    "/published/{video_id}/restore",
    response_model=CreatorPublishedMutationOut,
)
def restore_published_video(
    video_id: str,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorPublishedMutationOut:
    _require_creator(db, user)
    video = db.get(PublishedVideo, video_id)
    if video is None or video.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="published video not found")
    if bool(video.is_deleted) or video.deleted_at is not None:
        video.is_deleted = 0
        video.deleted_at = None
        video.updated_at = _now()
        creation = db.get(CreatorCreation, video_id)
        if creation is not None and creation.user_id == user.user_id:
            creation.status = "published"
            creation.progress_stage = "published"
            creation.updated_at = _now()
            db.add(creation)
        db.add(video)
        db.commit()
    return CreatorPublishedMutationOut(video_id=video_id, deleted=False)


@public_router.get("/share/{video_id}", response_class=HTMLResponse)
def creator_share_page(
    video_id: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    video = db.get(PublishedVideo, video_id)
    if (
        video is None
        or video.is_deleted != 0
        or video.deleted_at is not None
        or not video.distribution_enabled
    ):
        raise HTTPException(status_code=404, detail="video not found")
    title = html.escape(video.title or "Pixo interactive video")
    description = html.escape(video.description or "Play this interactive video on Pixo")
    deep_link = html.escape(f"pixo://work/{quote(video.id, safe='')}", quote=True)
    if video.content_type == "html":
        content = (
            "<div style='aspect-ratio:9/16;border-radius:20px;background:"
            "radial-gradient(circle at 30% 20%,#7c3aed,#111827 58%,#050507);"
            "display:grid;place-items:center;font-size:64px'>✦</div>"
        )
        media_meta = ""
    else:
        media = html.escape(
            canonicalize_public_url(settings, video.video_url) or "",
            quote=True,
        )
        content = (
            f"<video src='{media}' controls playsinline "
            "style='width:100%;border-radius:20px'></video>"
        )
        media_meta = f"<meta property='og:video' content='{media}'>"
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<meta property='og:title' content='{title}'>"
        f"<meta property='og:description' content='{description}'>"
        f"{media_meta}"
        f"<title>{title}</title></head><body style='margin:0;background:#08080b;color:white;"
        "font-family:system-ui;display:grid;place-items:center;min-height:100vh'>"
        f"<main style='width:min(92vw,420px)'>{content}"
        f"<h1>{title}</h1><p>{description}</p>"
        f"<a href='{deep_link}' style='display:block;text-align:center;padding:14px 18px;"
        "border-radius:999px;background:#7c3aed;color:white;text-decoration:none;"
        "font-weight:700'>Open in Pixo</a></main></body></html>"
    )
