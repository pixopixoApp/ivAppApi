from __future__ import annotations

import hashlib
import html
import re
import secrets
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.account_deletion import AccountDeletionUnavailable, delete_account_data
from app.auth_user import AppUser, require_bearer_user
from app.cdn_cache import enqueue_prefetch
from app.cdn_publication import (
    CdnPublicationError,
    activate_ready_publications,
    cancel_warming_publications,
    require_runtime_cdn_gate,
    stage_publication_gate,
)
from app.config import Settings, get_settings
from app.db import get_db
from app.deps import require_publish_key
from app.mail import send_creator_invite
from app.media_cache import local_path_for_sha256
from app.media_service import MediaServiceError, media_mode_is_oss
from app.models import (
    AppVersion,
    CdnPublicationGate,
    CreatorAccessGrant,
    CreatorApplication,
    CreatorCreation,
    CreatorInvite,
    CreatorSourceGeneration,
    CreatorUpload,
    CreatorVersion,
    MediaObject,
    PublishedVideo,
    PublishedVideoSeo,
    User,
)
from app.oss_storage import OssStorageError
from app.private_cdn import sign_private_media_url
from app.protocol_video import (
    BASE_RUNTIME_SPEC_VERSION,
    RuntimeSpecError,
    compile_runtime_spec,
    runtime_spec_version_from_compiled,
)
from app.public_origin import canonicalize_public_payload, canonicalize_public_url
from app.public_text import (
    record_creator_creation_text,
    record_creator_generation_text,
    record_creator_version_text,
    record_published_video_text,
)
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
    CreatorApplicationInviteRequest,
    CreatorApplicationInviteResponse,
    CreatorApplicationInviteResult,
    CreatorApplicationOut,
    CreatorApplicationRequest,
    CreatorCreationOut,
    CreatorCreationRequest,
    CreatorGenerationQuotaOut,
    CreatorInviteOut,
    CreatorInvitePage,
    CreatorPublishedMutationOut,
    CreatorPublishRequest,
    CreatorPublishResponse,
    CreatorSourceAcceptRequest,
    CreatorSourceGenerationOut,
    CreatorSourceRegenerateRequest,
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
from app.seo import ensure_seo_row
from app.share_urls import published_share_url, runtime_experience_url
from app.storage import LocalMediaStorage, StorageError
from app.verification_codes import PURPOSE_DEACTIVATE, find_valid_code
from app.video_probe import VideoProbeError, probe_video
from app.web_session import require_creator_user

public_router = APIRouter(prefix="/api/v1", tags=["platform"])
creator_router = APIRouter(prefix="/api/v1/creator", tags=["creator"])
operations_router = APIRouter(prefix="/internal/v1", tags=["admin"])

_ACTIVE_CREATION_STATUSES = ("queued", "running", "source_ready")
_ACTIVE_VERSION_STATUSES = ("queued", "running")
_INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _quota_window(now: datetime | None = None) -> tuple[str, datetime]:
    current = (now or _now()).astimezone(_SHANGHAI)
    next_day = current.date() + timedelta(days=1)
    reset_local = datetime.combine(next_day, time.min, tzinfo=_SHANGHAI)
    return current.date().isoformat(), reset_local.astimezone(timezone.utc)


def _generation_quota_out(
    db: Session,
    user_id: str,
    settings: Settings | None = None,
) -> CreatorGenerationQuotaOut:
    active_settings = settings or get_settings()
    quota_date, resets_at = _quota_window()
    rows = (
        db.query(CreatorSourceGeneration.quota_state, func.count(CreatorSourceGeneration.id))
        .filter(
            CreatorSourceGeneration.user_id == user_id,
            CreatorSourceGeneration.quota_date == quota_date,
            CreatorSourceGeneration.quota_state.in_(("reserved", "charged")),
        )
        .group_by(CreatorSourceGeneration.quota_state)
        .all()
    )
    counts = {str(state): int(count) for state, count in rows}
    reserved = counts.get("reserved", 0)
    used = counts.get("charged", 0)
    limit = max(0, active_settings.creator_video_daily_quota)
    enabled = bool(active_settings.creator_text_to_video_enabled)
    return CreatorGenerationQuotaOut(
        enabled=enabled,
        limit=limit,
        used=used,
        reserved=reserved,
        remaining=max(0, limit - used - reserved) if enabled else 0,
        resets_at=_iso(resets_at),
    )


def _reserve_generation_quota(
    db: Session,
    *,
    user_id: str,
    settings: Settings,
) -> tuple[str, datetime]:
    if not settings.creator_text_to_video_enabled:
        raise HTTPException(status_code=503, detail="text-to-video creation is not available")
    quota = _generation_quota_out(db, user_id, settings)
    if quota.remaining <= 0:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Daily video generation limit reached",
                "resets_at": quota.resets_at,
                "limit": quota.limit,
            },
        )
    quota_date, _resets_at = _quota_window()
    return quota_date, _now() + timedelta(days=max(1, settings.creator_video_draft_ttl_days))


def _normalize_invite(raw: str) -> str:
    return "".join(char for char in raw.strip().upper() if char not in " -")


def _invite_hash(raw: str) -> str:
    return hashlib.sha256(_normalize_invite(raw).encode("utf-8")).hexdigest()


def _new_invite_code() -> str:
    compact = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(12))
    return f"{compact[:4]}-{compact[4:8]}-{compact[8:]}"


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _creator_application_email(raw: str, user: AppUser | None = None) -> str:
    value = (raw or (user.email if user else None) or "").strip().lower()
    if not value or len(value) > 256 or not _EMAIL_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail="a valid application email is required")
    return value


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
    upload_id: str | None,
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
        preview_url=(
            f"/api/v1/creator/previews/{upload_id}"
            if ready and upload_id
            else None
        ),
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


def _source_generation_out(
    db: Session,
    row: CreatorSourceGeneration,
) -> CreatorSourceGenerationOut:
    upload = db.get(CreatorUpload, row.upload_id) if row.upload_id else None
    preview_ready = bool(
        upload is not None
        and upload.user_id == row.user_id
        and upload.normalization_status == "ready"
    )
    return CreatorSourceGenerationOut(
        generation_id=row.id,
        attempt=row.attempt,
        original_prompt=row.original_prompt,
        prompt_summary=row.prompt_summary,
        generation_prompt=row.generation_prompt,
        interaction_brief=row.interaction_brief,
        preset=dict(row.preset_json or {}),
        status=row.status,
        progress_stage=row.progress_stage,
        progress_percent=row.progress_percent,
        provider_task_accepted=row.provider_task_accepted,
        preview_url=(
            f"/api/v1/creator/uploads/{upload.id}/media" if preview_ready else None
        ),
        error_code=row.error_code or None,
        error_message=row.error_message or None,
        expires_at=_iso(row.expires_at),
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _creation_out(
    db: Session,
    row: CreatorCreation,
    settings: Settings | None = None,
) -> CreatorCreationOut:
    active_settings = settings or get_settings()
    versions = _creation_versions(db, row.id)
    generation = (
        db.get(CreatorSourceGeneration, row.source_generation_id)
        if row.source_generation_id
        else None
    )
    ready = row.status in ("ready", "published", "pending_review")
    source_out = _source_generation_out(db, generation) if generation is not None else None
    return CreatorCreationOut(
        creation_id=row.id,
        upload_id=row.upload_id,
        source_mode=("prompt" if row.source_mode == "prompt" else "upload"),
        source_prompt=row.source_prompt,
        source_generation_id=row.source_generation_id,
        source_preview_url=source_out.preview_url if source_out is not None else None,
        source_generation=source_out,
        generation_quota=_generation_quota_out(db, row.user_id, active_settings),
        status=row.status,
        progress_stage=row.progress_stage,
        progress_percent=row.progress_percent,
        retry_count=row.retry_count,
        preview_url=(
            f"/api/v1/creator/previews/{row.upload_id}"
            if ready and row.upload_id
            else None
        ),
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


def _share_url(settings: Settings, db: Session, video_id: str) -> str:
    seo = db.get(PublishedVideoSeo, video_id)
    return published_share_url(
        content_type="runtime",
        item_id=video_id,
        public_game_base_url=settings.public_game_base_url,
        public_share_base_url=settings.public_share_base_url,
        seo_public_base_url=settings.seo_public_base_url,
        seo_slug=seo.slug if seo is not None and seo.status == "ready" else "",
    )


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
    user: Annotated[AppUser, Depends(require_creator_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorAccessOut:
    grant = _access_grant(db, user.user_id)
    application = db.get(CreatorApplication, user.user_id)
    return CreatorAccessOut(
        granted=grant is not None,
        source=grant.source if grant else None,
        granted_at=_iso(grant.granted_at) if grant else None,
        application_status=application.status if application else None,
        application_email=application.email if application else None,
        video_generation=(
            _generation_quota_out(db, user.user_id, settings) if grant else None
        ),
    )


@creator_router.post("/invites/redeem", response_model=CreatorAccessOut)
def redeem_creator_invite(
    payload: InviteRedeemRequest,
    user: Annotated[AppUser, Depends(require_creator_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorAccessOut:
    existing = _access_grant(db, user.user_id)
    if existing is not None:
        return CreatorAccessOut(
            granted=True,
            source=existing.source,
            granted_at=_iso(existing.granted_at),
            video_generation=_generation_quota_out(db, user.user_id, settings),
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
    if invite.assigned_user_id and invite.assigned_user_id != user.user_id:
        db.rollback()
        raise HTTPException(status_code=400, detail="invite code is assigned to another account")
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
    application = db.get(CreatorApplication, user.user_id)
    if application is not None:
        if application.invite_id and application.invite_id != invite.id:
            previous = db.get(CreatorInvite, application.invite_id)
            if previous is not None and not previous.redeemed_by_user_id:
                previous.enabled = False
        application.invite_id = invite.id
        application.status = "approved"
        application.last_error = ""
        application.updated_at = now
    db.commit()
    return CreatorAccessOut(
        granted=True,
        source="invite",
        granted_at=_iso(now),
        video_generation=_generation_quota_out(db, user.user_id, settings),
    )


@creator_router.post("/applications", response_model=CreatorApplicationOut)
def apply_for_creator_access(
    payload: CreatorApplicationRequest,
    user: Annotated[AppUser, Depends(require_creator_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorApplicationOut:
    if _access_grant(db, user.user_id) is not None:
        raise HTTPException(status_code=409, detail="creator access already granted")
    now = _now()
    email = _creator_application_email(payload.email, user)
    row = db.get(CreatorApplication, user.user_id)
    if row is None:
        row = CreatorApplication(
            user_id=user.user_id,
            email=email,
            message=payload.message.strip(),
            status="pending",
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    elif row.status == "pending":
        row.email = email
        row.message = payload.message.strip()
        row.last_error = ""
        row.updated_at = now
    elif row.status == "rejected":
        row.email = email
        row.message = payload.message.strip()
        row.status = "pending"
        row.invite_id = None
        row.invited_at = None
        row.email_sent_at = None
        row.last_error = ""
        row.updated_at = now
    else:
        raise HTTPException(status_code=409, detail="creator application is already being processed")
    db.commit()
    db.refresh(row)
    return _creator_application_out(db, row)


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
        code, invite = _new_creator_invite(db)
        db.add(invite)
        codes.append(code)
    db.commit()
    return InviteCreateResponse(codes=codes)


def _invite_status(row: CreatorInvite) -> Literal["unused", "redeemed", "revoked"]:
    if row.redeemed_by_user_id:
        return "redeemed"
    return "unused" if row.enabled else "revoked"


def _new_creator_invite(
    db: Session,
    *,
    assigned_user_id: str | None = None,
) -> tuple[str, CreatorInvite]:
    while True:
        code = _new_invite_code()
        digest = _invite_hash(code)
        if db.query(CreatorInvite).filter(CreatorInvite.code_hash == digest).first():
            continue
        return code, CreatorInvite(
            code_hash=digest,
            code_hint=_normalize_invite(code)[-4:],
            enabled=True,
            assigned_user_id=assigned_user_id,
        )


def _creator_application_out(db: Session, row: CreatorApplication) -> CreatorApplicationOut:
    invite = db.get(CreatorInvite, row.invite_id) if row.invite_id else None
    return CreatorApplicationOut(
        user_id=row.user_id,
        email=row.email,
        message=row.message,
        status=row.status,
        invite_id=row.invite_id,
        invite_code_hint=invite.code_hint if invite else "",
        invite_status=_invite_status(invite) if invite else None,
        invited_at=_iso(row.invited_at) or None,
        email_sent_at=_iso(row.email_sent_at) or None,
        last_error=row.last_error,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


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
        assigned_user_id=row.assigned_user_id,
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
                CreatorInvite.assigned_user_id.contains(term),
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
                CreatorVersion.status.in_(_ACTIVE_VERSION_STATUSES),
            )
            .all()
        ):
            version.cancel_requested = True
        for generation in (
            db.query(CreatorSourceGeneration)
            .filter(
                CreatorSourceGeneration.creation_id.in_(creation_ids),
                CreatorSourceGeneration.status.in_(("queued", "running")),
            )
            .all()
        ):
            if generation.status == "queued" and not generation.ivadmin_job_id:
                generation.status = "cancelled"
                generation.progress_stage = "cancelled"
                if generation.quota_state == "reserved":
                    generation.quota_state = "released"
            else:
                generation.cancel_requested = True
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
    status: Literal["pending", "invited", "approved", "rejected"] | None = None,
) -> list[CreatorApplicationOut]:
    query = db.query(CreatorApplication)
    if status:
        query = query.filter(CreatorApplication.status == status)
    rows = query.order_by(CreatorApplication.updated_at.desc()).limit(500).all()
    return [_creator_application_out(db, row) for row in rows]


@operations_router.post(
    "/creator/applications/invite",
    response_model=CreatorApplicationInviteResponse,
    dependencies=[Depends(require_publish_key)],
)
def invite_creator_applicants(
    payload: CreatorApplicationInviteRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorApplicationInviteResponse:
    requested = list(
        dict.fromkeys(user_id.strip() for user_id in payload.user_ids if user_id.strip())
    )
    if not requested:
        raise HTTPException(status_code=400, detail="at least one user_id is required")
    results: list[CreatorApplicationInviteResult] = []
    for user_id in requested:
        row = (
            db.query(CreatorApplication)
            .filter(CreatorApplication.user_id == user_id)
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            results.append(CreatorApplicationInviteResult(
                user_id=user_id,
                status="failed",
                error="Creator application not found.",
            ))
            db.rollback()
            continue
        if row.status != "pending" or row.invite_id:
            results.append(CreatorApplicationInviteResult(
                user_id=user_id,
                email=row.email,
                status="skipped",
                application_status=row.status,
                invite_id=row.invite_id,
                error="This application has already been processed.",
            ))
            db.rollback()
            continue
        try:
            email = _creator_application_email(row.email)
        except HTTPException as exc:
            row.last_error = str(exc.detail)
            row.updated_at = _now()
            db.commit()
            results.append(CreatorApplicationInviteResult(
                user_id=user_id,
                email=row.email,
                status="failed",
                application_status=row.status,
                error=str(exc.detail),
            ))
            continue

        code, invite = _new_creator_invite(db, assigned_user_id=user_id)
        db.add(invite)
        db.flush()
        now = _now()
        row.invite_id = invite.id
        row.status = "invited"
        row.invited_at = now
        row.email_sent_at = now
        row.last_error = ""
        row.updated_at = now
        try:
            send_creator_invite(settings, email=email, code=code)
        # Any delivery failure must roll back the assigned one-time code so the
        # application remains retryable by operations.
        except Exception:  # noqa: BLE001
            db.rollback()
            failed = db.get(CreatorApplication, user_id)
            if failed is not None:
                failed.last_error = "Email could not be sent. Check SMTP settings and retry."
                failed.updated_at = _now()
                db.commit()
            results.append(CreatorApplicationInviteResult(
                user_id=user_id,
                email=email,
                status="failed",
                application_status="pending",
                error="Email could not be sent. Check SMTP settings and retry.",
            ))
            continue
        db.commit()
        results.append(CreatorApplicationInviteResult(
            user_id=user_id,
            email=email,
            status="sent",
            application_status="invited",
            invite_id=invite.id,
            invite_code_hint=invite.code_hint,
        ))

    return CreatorApplicationInviteResponse(
        items=results,
        sent_count=sum(item.status == "sent" for item in results),
        skipped_count=sum(item.status == "skipped" for item in results),
        failed_count=sum(item.status == "failed" for item in results),
    )


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
    linked_invite = db.get(CreatorInvite, row.invite_id) if row.invite_id else None
    if linked_invite is not None and not linked_invite.redeemed_by_user_id:
        linked_invite.enabled = False
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
    return _creator_application_out(db, row)


@creator_router.post("/uploads", response_model=CreatorUploadOut, status_code=201)
async def upload_creator_video(
    user: Annotated[AppUser, Depends(require_creator_user)],
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
    if row.normalization_status != "ready" or not row.playable_sha256:
        raise HTTPException(status_code=425, detail="preview is still being prepared")
    if row.playable_media_object_id:
        media = db.get(MediaObject, row.playable_media_object_id)
        if media is None or media.state != "ready":
            raise HTTPException(status_code=404, detail="preview not found")
        try:
            return RedirectResponse(
                sign_private_media_url(
                    settings,
                    key=media.object_key,
                    expires_seconds=settings.private_media_cdn_ttl_seconds,
                    filename=row.original_filename,
                ),
                status_code=307,
                headers={"Cache-Control": "no-store"},
            )
        except OssStorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    path = local_path_for_sha256(
        settings,
        row.playable_sha256,
        expected_size=row.playable_size_bytes,
    )
    if path is None:
        raise HTTPException(status_code=404, detail="preview not found")
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"Cache-Control": "private, max-age=300"},
    )


@creator_router.get("/uploads/{upload_id}/media", response_class=FileResponse)
def preview_creator_upload(
    upload_id: str,
    user: Annotated[AppUser, Depends(require_creator_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    row = db.get(CreatorUpload, upload_id)
    if row is None or row.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="upload not found")
    if row.normalization_status != "ready" or not row.playable_sha256:
        raise HTTPException(status_code=425, detail="upload media is still being prepared")
    if row.playable_media_object_id:
        media = db.get(MediaObject, row.playable_media_object_id)
        if media is None or media.state != "ready":
            raise HTTPException(status_code=404, detail="upload media missing")
        try:
            return RedirectResponse(
                sign_private_media_url(
                    settings,
                    key=media.object_key,
                    expires_seconds=settings.private_media_cdn_ttl_seconds,
                    filename=row.original_filename,
                ),
                status_code=307,
                headers={"Cache-Control": "no-store"},
            )
        except OssStorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    path = local_path_for_sha256(
        settings,
        row.playable_sha256,
        expected_size=row.playable_size_bytes,
    )
    if path is None:
        raise HTTPException(status_code=404, detail="upload media missing")
    return FileResponse(path, media_type="video/mp4", filename=row.original_filename)


@creator_router.post("/creations", response_model=CreatorCreationOut, status_code=202)
def create_interactive_video(
    payload: CreatorCreationRequest,
    user: Annotated[AppUser, Depends(require_creator_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorCreationOut:
    _lock_creator(db, user.user_id)
    request_id = payload.request_id.strip() if payload.request_id else ""
    if request_id:
        existing_generation = (
            db.query(CreatorSourceGeneration)
            .filter(CreatorSourceGeneration.request_id == request_id)
            .first()
        )
        if existing_generation is not None:
            if (
                existing_generation.user_id != user.user_id
                or existing_generation.original_prompt != payload.prompt.strip()
            ):
                raise HTTPException(status_code=409, detail="request id is already in use")
            return _creation_out(
                db,
                _owned_creation(db, existing_generation.creation_id, user.user_id),
                settings,
            )
        existing_version = (
            db.query(CreatorVersion)
            .filter(CreatorVersion.request_id == request_id)
            .first()
        )
        if existing_version is not None:
            if existing_version.user_id != user.user_id:
                raise HTTPException(status_code=409, detail="request id is already in use")
            existing = _owned_creation(db, existing_version.creation_id, user.user_id)
            return _creation_out(db, existing, settings)
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
    if payload.source_mode == "prompt":
        quota_date, expires_at = _reserve_generation_quota(
            db,
            user_id=user.user_id,
            settings=settings,
        )
        generation_id = f"csg_{secrets.token_urlsafe(18)}"
        generation_request_id = request_id or f"generate:{generation_id}"
        row = CreatorCreation(
            id=creation_id,
            user_id=user.user_id,
            upload_id=None,
            source_mode="prompt",
            source_prompt=payload.prompt.strip(),
            source_generation_id=generation_id,
            brief="",
            status="queued",
            progress_stage="planning_prompt",
            progress_percent=0,
            active_version_id=None,
            created_at=now,
            updated_at=now,
        )
        generation = CreatorSourceGeneration(
            id=generation_id,
            creation_id=creation_id,
            user_id=user.user_id,
            attempt=1,
            request_id=generation_request_id,
            original_prompt=payload.prompt.strip(),
            status="queued",
            progress_stage="planning_prompt",
            progress_percent=0,
            quota_date=quota_date,
            quota_state="reserved",
            next_poll_at=now,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        db.add_all([row, generation])
        record_creator_creation_text(db, row)
        record_creator_generation_text(db, generation)
        db.commit()
        db.refresh(row)
        return _creation_out(db, row, settings)

    upload = db.get(CreatorUpload, payload.upload_id)
    if upload is None or upload.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="upload not found")
    version_id = f"cv_{secrets.token_urlsafe(18)}"
    row = CreatorCreation(
        id=creation_id,
        user_id=user.user_id,
        upload_id=upload.id,
        source_mode="upload",
        source_prompt="",
        brief=payload.brief.strip(),
        status="queued",
        progress_stage="queued",
        progress_percent=0,
        active_version_id=version_id,
        created_at=now,
        updated_at=now,
    )
    version = CreatorVersion(
        id=version_id,
        creation_id=creation_id,
        user_id=user.user_id,
        number=1,
        request_id=request_id or version_id,
        brief=payload.brief.strip(),
        status="queued",
        progress_stage="queued",
        progress_percent=0,
        created_at=now,
        updated_at=now,
    )
    db.add_all([row, version])
    record_creator_creation_text(db, row)
    record_creator_version_text(db, version)
    db.commit()
    db.refresh(row)
    return _creation_out(db, row, settings)


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


def _owned_source_generation(
    db: Session,
    generation_id: str,
    user_id: str,
) -> CreatorSourceGeneration:
    row = db.get(CreatorSourceGeneration, generation_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="source generation not found")
    return row


@creator_router.post(
    "/creations/{creation_id}/source/regenerate",
    response_model=CreatorCreationOut,
    status_code=202,
)
def regenerate_creation_source(
    creation_id: str,
    payload: CreatorSourceRegenerateRequest,
    user: Annotated[AppUser, Depends(require_creator_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorCreationOut:
    _lock_creator(db, user.user_id)
    creation = _owned_creation(db, creation_id, user.user_id)
    if creation.source_mode != "prompt":
        raise HTTPException(status_code=409, detail="uploaded sources cannot be regenerated")

    request_id = payload.request_id.strip()
    existing = (
        db.query(CreatorSourceGeneration)
        .filter(CreatorSourceGeneration.request_id == request_id)
        .first()
    )
    if existing is not None:
        if (
            existing.creation_id != creation.id
            or existing.user_id != user.user_id
            or existing.original_prompt != payload.prompt.strip()
        ):
            raise HTTPException(status_code=409, detail="request id is already in use")
        return _creation_out(db, creation, settings)

    if creation.status in {"published", "pending_review", "deleted"}:
        raise HTTPException(status_code=409, detail="this creation can no longer be regenerated")
    if _creation_versions(db, creation.id):
        raise HTTPException(
            status_code=409,
            detail="the source was already accepted; start a new creation instead",
        )
    current = (
        db.get(CreatorSourceGeneration, creation.source_generation_id)
        if creation.source_generation_id
        else None
    )
    if current is not None and current.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="source generation is still in progress")

    quota_date, expires_at = _reserve_generation_quota(
        db,
        user_id=user.user_id,
        settings=settings,
    )
    next_attempt = int(
        db.query(func.max(CreatorSourceGeneration.attempt))
        .filter(CreatorSourceGeneration.creation_id == creation.id)
        .scalar()
        or 0
    ) + 1
    now = _now()
    generation = CreatorSourceGeneration(
        id=f"csg_{secrets.token_urlsafe(18)}",
        creation_id=creation.id,
        user_id=user.user_id,
        attempt=next_attempt,
        request_id=request_id,
        original_prompt=payload.prompt.strip(),
        status="queued",
        progress_stage="planning_prompt",
        progress_percent=0,
        quota_date=quota_date,
        quota_state="reserved",
        next_poll_at=now,
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )
    creation.upload_id = None
    creation.source_prompt = generation.original_prompt
    creation.source_generation_id = generation.id
    creation.brief = ""
    creation.status = "queued"
    creation.progress_stage = "planning_prompt"
    creation.progress_percent = 0
    creation.active_version_id = None
    creation.error_code = ""
    creation.error_message = ""
    creation.updated_at = now
    db.add_all([generation, creation])
    record_creator_generation_text(db, generation)
    record_creator_creation_text(db, creation)
    db.commit()
    db.refresh(creation)
    return _creation_out(db, creation, settings)


@creator_router.post(
    "/creations/{creation_id}/source/accept",
    response_model=CreatorCreationOut,
    status_code=202,
)
def accept_creation_source(
    creation_id: str,
    payload: CreatorSourceAcceptRequest,
    user: Annotated[AppUser, Depends(require_creator_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorCreationOut:
    _lock_creator(db, user.user_id)
    creation = _owned_creation(db, creation_id, user.user_id)
    generation = _owned_source_generation(db, payload.generation_id, user.user_id)
    if generation.creation_id != creation.id or creation.source_generation_id != generation.id:
        raise HTTPException(status_code=409, detail="this is no longer the current source")

    versions = _creation_versions(db, creation.id)
    if generation.accepted_at is not None and versions:
        return _creation_out(db, creation, settings)
    if generation.status != "ready" or not generation.upload_id:
        raise HTTPException(status_code=409, detail="source video is not ready for review")
    upload = db.get(CreatorUpload, generation.upload_id)
    if (
        upload is None
        or upload.user_id != user.user_id
        or upload.normalization_status != "ready"
    ):
        raise HTTPException(status_code=409, detail="source preview is still being prepared")
    if versions:
        raise HTTPException(status_code=409, detail="this creation already has an analysis version")

    request_id = payload.request_id.strip()
    reused = db.query(CreatorVersion).filter(CreatorVersion.request_id == request_id).first()
    if reused is not None:
        raise HTTPException(status_code=409, detail="request id is already in use")
    now = _now()
    version_id = f"cv_{secrets.token_urlsafe(18)}"
    brief = generation.interaction_brief.strip() or generation.original_prompt
    version = CreatorVersion(
        id=version_id,
        creation_id=creation.id,
        user_id=user.user_id,
        number=1,
        request_id=request_id,
        brief=brief,
        status="queued",
        progress_stage="queued",
        progress_percent=0,
        created_at=now,
        updated_at=now,
    )
    generation.accepted_at = now
    generation.updated_at = now
    creation.upload_id = upload.id
    creation.brief = brief
    creation.active_version_id = version_id
    creation.status = "queued"
    creation.progress_stage = "queued"
    creation.progress_percent = 0
    creation.error_code = ""
    creation.error_message = ""
    creation.updated_at = now
    db.add_all([generation, creation, version])
    record_creator_generation_text(db, generation)
    record_creator_creation_text(db, creation)
    record_creator_version_text(db, version)
    db.commit()
    db.refresh(creation)
    return _creation_out(db, creation, settings)


@creator_router.get("/creations/active", response_model=CreatorCreationOut | None)
def get_active_creation(
    user: Annotated[AppUser, Depends(require_creator_user)],
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
    user: Annotated[AppUser, Depends(require_creator_user)],
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
    user: Annotated[AppUser, Depends(require_creator_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorCreationOut:
    _lock_creator(db, user.user_id)
    creation = _owned_creation(db, creation_id, user.user_id)
    if creation.status == "published":
        raise HTTPException(status_code=409, detail="published creations cannot be changed")
    if not creation.upload_id:
        raise HTTPException(status_code=409, detail="accept the generated source video first")
    if creation.source_mode == "prompt":
        generation = (
            db.get(CreatorSourceGeneration, creation.source_generation_id)
            if creation.source_generation_id
            else None
        )
        if generation is None or generation.accepted_at is None:
            raise HTTPException(status_code=409, detail="accept the generated source video first")
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
    version = CreatorVersion(
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
    db.add(version)
    creation.active_version_id = version_id
    creation.status = "queued"
    creation.progress_stage = "queued"
    creation.progress_percent = 0
    creation.brief = payload.brief.strip()
    creation.error_code = ""
    creation.error_message = ""
    creation.updated_at = now
    record_creator_version_text(db, version)
    record_creator_creation_text(db, creation)
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
    user: Annotated[AppUser, Depends(require_creator_user)],
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
    user: Annotated[AppUser, Depends(require_creator_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorCreationOut:
    _require_creator(db, user)
    row = _owned_creation(db, creation_id, user.user_id)
    if row.source_mode == "prompt" and row.source_generation_id:
        generation = _owned_source_generation(
            db,
            row.source_generation_id,
            user.user_id,
        )
        if generation.accepted_at is None and generation.status in {
            "queued",
            "running",
            "ready",
        }:
            if generation.status == "queued" and not generation.ivadmin_job_id:
                generation.status = "cancelled"
                generation.progress_stage = "cancelled"
                generation.error_code = "CANCELLED"
                generation.error_message = "Video generation was cancelled."
                if generation.quota_state == "reserved":
                    generation.quota_state = "released"
            elif generation.status == "ready":
                generation.status = "cancelled"
                generation.progress_stage = "cancelled"
                generation.error_code = "CANCELLED"
                generation.error_message = "Generated source was not accepted."
            else:
                generation.cancel_requested = True
            generation.updated_at = _now()
            row.status = "cancelled" if generation.status == "cancelled" else "running"
            row.progress_stage = generation.progress_stage
            row.updated_at = _now()
            db.add_all([generation, row])
            db.commit()
            db.refresh(row)
            return _creation_out(db, row)
    versions = _creation_versions(db, row.id)
    active = next((item for item in versions if item.status in _ACTIVE_VERSION_STATUSES), None)
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
    user: Annotated[AppUser, Depends(require_creator_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorCreationOut:
    _require_creator(db, user)
    row = _owned_creation(db, creation_id, user.user_id)
    if row.status == "published":
        raise HTTPException(status_code=409, detail="published creations cannot be abandoned")
    if row.source_mode == "prompt" and row.source_generation_id:
        generation = db.get(CreatorSourceGeneration, row.source_generation_id)
        if generation is not None and generation.accepted_at is None:
            if generation.status == "queued" and not generation.ivadmin_job_id:
                generation.status = "cancelled"
                generation.progress_stage = "cancelled"
                if generation.quota_state == "reserved":
                    generation.quota_state = "released"
            elif generation.status == "running":
                generation.cancel_requested = True
            generation.updated_at = _now()
            db.add(generation)
    for version in _creation_versions(db, row.id):
        if version.status in _ACTIVE_VERSION_STATUSES:
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
    user: Annotated[AppUser, Depends(require_creator_user)],
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
    user: Annotated[AppUser, Depends(require_creator_user)],
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
    user: Annotated[AppUser, Depends(require_creator_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorPublishResponse:
    _require_creator(db, user)
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="preview confirmation is required")
    row = _owned_creation(db, creation_id, user.user_id)
    if row.status in {"published", "pending_review"} and row.published_video_id:
        existing_video = db.get(PublishedVideo, row.published_video_id)
        gate = (
            db.query(CdnPublicationGate)
            .filter(CdnPublicationGate.video_id == row.published_video_id)
            .order_by(CdnPublicationGate.created_at.desc())
            .first()
        )
        cdn_status = (
            "ready"
            if existing_video is not None and bool(existing_video.cdn_ready)
            else ("failed" if gate is not None and gate.state == "failed" else "warming")
        )
        return CreatorPublishResponse(
            video_id=row.published_video_id,
            status=("published" if row.status == "published" else "pending_review"),
            runtime_spec_version=(
                row.runtime_spec_version or BASE_RUNTIME_SPEC_VERSION
            ),
            share_url=_share_url(settings, db, row.published_video_id),
            cdn_status=cdn_status,
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
    if upload.normalization_status != "ready" or not upload.playable_sha256:
        raise HTTPException(status_code=409, detail="source video is still being normalized")

    if media_mode_is_oss(settings):
        try:
            require_runtime_cdn_gate(settings)
        except CdnPublicationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    final_url = f"/media/{row.id}.mp4"
    publication_id = None
    prefetch_urls: list[str] = []
    if media_mode_is_oss(settings):
        if not upload.playable_media_object_id:
            raise HTTPException(
                status_code=409,
                detail="playable video backup is still in progress",
            )
        source_media = db.get(MediaObject, upload.playable_media_object_id)
        if source_media is None:
            raise HTTPException(status_code=409, detail="playable media object is missing")
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
    runtime_spec_version = runtime_spec_version_from_compiled(runtime_spec)

    storage = LocalMediaStorage(settings) if not media_mode_is_oss(settings) else None
    destination = None
    now = _now()
    try:
        copied_url = final_url
        if storage is not None:
            playable_path = local_path_for_sha256(
                settings,
                upload.playable_sha256,
                expected_size=upload.playable_size_bytes,
            )
            if playable_path is None:
                raise StorageError("normalized video is missing")
            destination, copied_url = storage.publish_file(source=playable_path, item_id=row.id)
        published = PublishedVideo(
            id=row.id,
            content_type="runtime",
            video_url=copied_url,
            timeline=source_timeline,
            runtime_spec=runtime_spec,
            runtime_spec_version=runtime_spec_version,
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
            cdn_ready=not media_mode_is_oss(settings),
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
        row.runtime_spec_version = runtime_spec_version
        if version is not None:
            version.status = "published"
            version.progress_stage = "published"
            version.runtime_spec = runtime_spec
            version.runtime_spec_version = runtime_spec_version
            version.updated_at = now
            db.add(version)
        row.updated_at = now
        record_published_video_text(db, published)
        ensure_seo_row(db, published)
        record_creator_creation_text(db, row)
        if version is not None:
            record_creator_version_text(db, version)
        enqueue_prefetch(db, settings, prefetch_urls)
        gate = None
        if media_mode_is_oss(settings):
            if not publication_id:
                raise CdnPublicationError("runtime publication id is missing")
            gate = stage_publication_gate(
                db,
                video_id=row.id,
                publication_id=publication_id,
                urls=prefetch_urls,
                # Moderation may change review fields while the CDN warms. The
                # creator row already has its final runtime payload, so only
                # the readiness bit is released by the gate.
                staged_payload={},
            )
            db.flush()
            activate_ready_publications(db, publication_ids=[publication_id])
        db.commit()
    except Exception:
        db.rollback()
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise
    return CreatorPublishResponse(
        video_id=row.id,
        status="pending_review",
        runtime_spec_version=runtime_spec_version,
        share_url=_share_url(settings, db, row.id),
        cdn_status=(
            "ready"
            if not media_mode_is_oss(settings) or (gate is not None and gate.state == "active")
            else ("failed" if gate is not None and gate.state == "failed" else "warming")
        ),
    )


@creator_router.delete(
    "/published/{video_id}",
    response_model=CreatorPublishedMutationOut,
)
def delete_published_video(
    video_id: str,
    user: Annotated[AppUser, Depends(require_creator_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreatorPublishedMutationOut:
    _require_creator(db, user)
    video = db.get(PublishedVideo, video_id)
    if video is None or video.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="published video not found")
    if not bool(video.is_deleted) or video.deleted_at is None:
        video.is_deleted = 1
        video.deleted_at = _now()
        cancel_warming_publications(
            db,
            video_id=video_id,
            reason="creator deleted the publication while CDN was warming",
        )
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
    user: Annotated[AppUser, Depends(require_creator_user)],
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
            creation.status = (
                "pending_review" if video.review_status == "pending" else "published"
            )
            creation.progress_stage = creation.status
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
) -> Response:
    video = db.get(PublishedVideo, video_id)
    if (
        video is None
        or video.is_deleted != 0
        or video.deleted_at is not None
        or not video.distribution_enabled
        or not video.cdn_ready
        or video.review_status not in {"approved", "pending"}
        or (video.review_status == "pending" and video.content_source != "ugc")
    ):
        raise HTTPException(status_code=404, detail="video not found")
    if video.content_type != "html":
        seo = db.get(PublishedVideoSeo, video.id)
        if seo is not None and seo.status == "ready" and seo.slug:
            return RedirectResponse(
                url=(
                    f"{settings.seo_public_base_url.rstrip('/')}/experiences/"
                    f"{quote(seo.slug, safe='-')}"
                ),
                status_code=308,
            )
        experience_url = runtime_experience_url(settings.public_game_base_url, video.id)
        if experience_url:
            return RedirectResponse(url=experience_url, status_code=308)
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
