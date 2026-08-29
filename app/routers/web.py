from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from sqlalchemy.orm import Session

from app.auth_user import AppUser, issue_user_token
from app.avatar_storage import AvatarStorageError, store_user_avatar
from app.cdn_cache import enqueue_prefetch
from app.config import Settings, get_settings
from app.db import get_db
from app.google_auth import GoogleAuthUnavailable, verify_google_id_token
from app.models import PublishedVideo, User, UserToken
from app.public_origin import canonicalize_public_url
from app.schemas_web import (
    WebCodeSentOut,
    WebConfigOut,
    WebCreatorConfigOut,
    WebEmailCodeRequest,
    WebEmailRequest,
    WebGoogleRequest,
    WebProfileOut,
    WebProfileUpdateRequest,
    WebPublicationOut,
    WebPublicationPageOut,
    WebSessionOut,
)
from app.share_urls import published_share_url
from app.users import (
    follow_counts,
    get_or_create_user,
    normalize_bio,
    normalize_nickname,
)
from app.verification_codes import PURPOSE_LOGIN, find_valid_code, issue_email_code
from app.web_session import (
    WEB_CSRF_COOKIE,
    clear_session_cookies,
    new_csrf_token,
    optional_web_user,
    require_web_user,
    set_csrf_cookie,
    set_session_cookies,
    verify_web_csrf,
)

router = APIRouter(prefix="/api/v1/web", tags=["web"])
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _email(raw: str) -> str:
    return raw.strip().lower()


def _profile(db: Session, settings: Settings, user: User) -> WebProfileOut:
    following_count, follower_count = follow_counts(db, user.user_id)
    return WebProfileOut(
        user_id=user.user_id,
        provider=user.provider,
        email=user.subject if user.provider == "email" else "",
        nickname=user.nickname or "",
        avatar_url=canonicalize_public_url(settings, user.avatar_url) or "",
        bio=user.bio or "",
        following_count=following_count,
        follower_count=follower_count,
    )


def _session(db: Session, settings: Settings, user: AppUser) -> WebSessionOut:
    row = db.get(User, user.user_id)
    if row is None:
        return WebSessionOut(authenticated=False)
    return WebSessionOut(authenticated=True, user=_profile(db, settings, row))


def _login(
    response: Response,
    db: Session,
    settings: Settings,
    user: User,
    *,
    now: datetime,
) -> WebSessionOut:
    session = issue_user_token(
        db,
        user_id=user.user_id,
        token_ttl_days=settings.token_ttl_days,
        now=now,
    )
    db.commit()
    set_session_cookies(
        response,
        settings,
        session_token=session.token,
        csrf_token=new_csrf_token(),
    )
    return WebSessionOut(authenticated=True, user=_profile(db, settings, user))


@router.get("/config", response_model=WebConfigOut)
def get_web_config(
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebConfigOut:
    if not request.cookies.get(WEB_CSRF_COOKIE):
        set_csrf_cookie(response, settings, new_csrf_token())
    transports = ["local-resumable-v1"] if settings.creator_local_upload_enabled else []
    return WebConfigOut(
        google_client_id=(settings.web_google_client_id or settings.google_client_id).strip(),
        email_code_ttl_seconds=settings.code_ttl_seconds,
        email_resend_seconds=settings.send_code_interval_seconds,
        creator=WebCreatorConfigOut(
            allowed_content_types=["video/mp4"],
            max_bytes=settings.creator_video_max_bytes,
            max_duration_seconds=settings.creator_video_max_duration_seconds,
            supported_transports=transports,
            text_to_video_enabled=settings.creator_text_to_video_enabled,
            daily_generation_quota=max(0, settings.creator_video_daily_quota),
            generated_duration_seconds=10,
            generated_ratio="9:16",
            generated_resolution="720p",
        ),
    )


@router.get("/auth/session", response_model=WebSessionOut)
def get_web_session(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebSessionOut:
    if not request.cookies.get(WEB_CSRF_COOKIE):
        set_csrf_cookie(response, settings, new_csrf_token())
    user = optional_web_user(request, db)
    return _session(db, settings, user) if user is not None else WebSessionOut(authenticated=False)


@router.post("/auth/email/send-code", response_model=WebCodeSentOut)
def send_web_email_code(
    payload: WebEmailRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebCodeSentOut:
    verify_web_csrf(request)
    email = _email(payload.email)
    if not _EMAIL_RE.fullmatch(email):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")
    result = issue_email_code(db, settings, email=email, purpose=PURPOSE_LOGIN)
    if not result.ok:
        if result.error_code == "CODE_RATE_LIMITED":
            raise HTTPException(
                status_code=429,
                detail={
                    "message": "A code was just sent. Please wait before trying again.",
                    "retry_after_seconds": result.retry_after_seconds,
                },
            )
        raise HTTPException(status_code=503, detail="We couldn't send the email right now.")
    return WebCodeSentOut(
        sent=True,
        expires_in_seconds=settings.code_ttl_seconds,
        resend_after_seconds=settings.send_code_interval_seconds,
    )


@router.post("/auth/email/verify", response_model=WebSessionOut)
def verify_web_email_code(
    payload: WebEmailCodeRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebSessionOut:
    verify_web_csrf(request)
    email = _email(payload.email)
    code = payload.code.strip()
    if not _EMAIL_RE.fullmatch(email) or not code.isdigit():
        raise HTTPException(status_code=400, detail="Enter the six-digit code from your email.")
    now = datetime.now(timezone.utc)
    code_row = find_valid_code(
        db,
        email=email,
        code=code,
        purpose=PURPOSE_LOGIN,
        now=now,
    )
    if code_row is None:
        raise HTTPException(status_code=400, detail="That code is invalid or has expired.")
    code_row.used_at = now
    user = get_or_create_user(db, provider="email", subject=email)
    if not user.enabled:
        db.commit()
        raise HTTPException(status_code=403, detail="This account is unavailable.")
    return _login(response, db, settings, user, now=now)


@router.post("/auth/google", response_model=WebSessionOut)
def login_web_google(
    payload: WebGoogleRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebSessionOut:
    verify_web_csrf(request)
    client_id = (settings.web_google_client_id or settings.google_client_id).strip()
    if not client_id:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured.")
    try:
        identity = verify_google_id_token(
            token=payload.credential,
            client_ids=(client_id,),
            timeout_seconds=settings.google_timeout_seconds,
        )
    except GoogleAuthUnavailable as exc:
        raise HTTPException(status_code=503, detail="Google sign-in is temporarily unavailable.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Google sign-in could not be verified.") from exc
    now = datetime.now(timezone.utc)
    user = get_or_create_user(db, provider="google", subject=identity.subject)
    if not user.enabled:
        db.commit()
        raise HTTPException(status_code=403, detail="This account is unavailable.")
    return _login(response, db, settings, user, now=now)


@router.post("/auth/logout", status_code=204)
def logout_web(
    response: Response,
    user: Annotated[AppUser, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    row = db.get(UserToken, user.token)
    if row is not None:
        db.delete(row)
        db.commit()
    clear_session_cookies(response, settings)
    response.status_code = 204
    return response


@router.get("/me", response_model=WebProfileOut)
def get_web_profile(
    user: Annotated[AppUser, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebProfileOut:
    row = db.get(User, user.user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Profile not found.")
    return _profile(db, settings, row)


@router.patch("/me", response_model=WebProfileOut)
def update_web_profile(
    payload: WebProfileUpdateRequest,
    user: Annotated[AppUser, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebProfileOut:
    row = db.get(User, user.user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Profile not found.")
    try:
        if payload.nickname is not None:
            row.nickname = normalize_nickname(payload.nickname)
        if payload.bio is not None:
            row.bio = normalize_bio(payload.bio)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(row)
    db.commit()
    db.refresh(row)
    return _profile(db, settings, row)


@router.post("/me/avatar", response_model=WebProfileOut)
async def update_web_avatar(
    user: Annotated[AppUser, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    file: Annotated[UploadFile, File()],
) -> WebProfileOut:
    row = db.get(User, user.user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Profile not found.")
    try:
        relative, media_object_id = store_user_avatar(
            db,
            settings,
            user_id=user.user_id,
            raw=await file.read(),
            filename=file.filename,
            content_type=file.content_type,
        )
    except (AvatarStorageError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row.avatar_url = relative
    row.avatar_media_object_id = media_object_id
    enqueue_prefetch(db, settings, [relative])
    db.add(row)
    db.commit()
    db.refresh(row)
    return _profile(db, settings, row)


def _publication_status(row: PublishedVideo) -> str:
    if bool(row.is_deleted) or row.deleted_at is not None:
        return "deleted"
    if row.review_status == "rejected":
        return "rejected"
    if row.review_status != "approved":
        return "pending_review"
    if not row.cdn_ready:
        return "warming"
    if not row.distribution_enabled:
        return "hidden"
    return "live"


@router.get("/me/publications", response_model=WebPublicationPageOut)
def list_web_publications(
    user: Annotated[AppUser, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WebPublicationPageOut:
    base = db.query(PublishedVideo).filter(PublishedVideo.user_id == user.user_id)
    total = base.count()
    rows = (
        base.order_by(PublishedVideo.created_at.desc(), PublishedVideo.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return WebPublicationPageOut(
        items=[
            WebPublicationOut(
                video_id=row.id,
                title=row.title or "Untitled experience",
                description=row.description or "",
                media_url=canonicalize_public_url(settings, row.video_url) or "",
                share_url=published_share_url(
                    content_type=row.content_type,
                    item_id=row.id,
                    public_game_base_url=settings.public_game_base_url,
                    public_share_base_url=settings.public_share_base_url,
                ),
                status=_publication_status(row),
                review_status=row.review_status,
                cdn_ready=bool(row.cdn_ready),
                deleted=bool(row.is_deleted) or row.deleted_at is not None,
                created_at=row.created_at.isoformat() if row.created_at else "",
                updated_at=row.updated_at.isoformat() if row.updated_at else "",
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
