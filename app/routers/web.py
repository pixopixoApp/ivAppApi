from __future__ import annotations

import html
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
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth_user import AppUser, issue_user_token
from app.avatar_storage import AvatarStorageError, store_user_avatar
from app.config import Settings, get_settings
from app.credits import bind_referral_for_new_user
from app.db import get_db
from app.google_auth import GoogleAuthUnavailable, verify_google_id_token
from app.models import (
    AppVersion,
    PublishedVideo,
    PublishedVideoSeo,
    ReferralInvite,
    User,
    UserToken,
)
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
invite_router = APIRouter(tags=["invite"])
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _email(raw: str) -> str:
    return raw.strip().lower()


def _new_identity(db: Session, *, provider: str, subject: str) -> bool:
    return (
        db.query(User)
        .filter(User.provider == provider, User.subject == subject)
        .one_or_none()
        is None
    )


def _bind_web_invite_if_present(
    db: Session,
    *,
    user: User,
    was_new: bool,
    code: str,
) -> None:
    normalized = code.strip().upper()
    if not normalized:
        return
    if not was_new:
        raise HTTPException(status_code=409, detail="Only a new account can use an invite link.")
    try:
        bind_referral_for_new_user(db, user=user, code=normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
            preparation_profile="first-30s-v1" if transports else None,
            max_source_bytes=max(settings.creator_video_max_bytes, settings.creator_web_source_max_bytes) if transports else settings.creator_video_max_bytes,
            text_to_video_enabled=settings.creator_text_to_video_enabled,
            daily_generation_quota=max(0, settings.creator_video_daily_quota),
            generated_duration_seconds=settings.creator_video_duration_seconds,
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
    was_new = _new_identity(db, provider="email", subject=email)
    user = get_or_create_user(db, provider="email", subject=email)
    _bind_web_invite_if_present(
        db,
        user=user,
        was_new=was_new,
        code=payload.invite_code,
    )
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
    was_new = _new_identity(db, provider="google", subject=identity.subject)
    user = get_or_create_user(db, provider="google", subject=identity.subject)
    _bind_web_invite_if_present(
        db,
        user=user,
        was_new=was_new,
        code=payload.invite_code,
    )
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
    seo_slugs = {
        video_id: slug
        for video_id, slug in db.query(PublishedVideoSeo.video_id, PublishedVideoSeo.slug)
        .filter(
            PublishedVideoSeo.video_id.in_([row.id for row in rows]),
            PublishedVideoSeo.status == "ready",
        )
        .all()
    }
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
                    seo_public_base_url=settings.seo_public_base_url,
                    seo_slug=seo_slugs.get(row.id, ""),
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


@invite_router.get("/invite/{code}", response_class=HTMLResponse, include_in_schema=False)
def referral_landing_page(
    code: str,
    db: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    normalized = code.strip().upper()
    if not re.fullmatch(r"[A-Z0-9-]{6,32}", normalized):
        raise HTTPException(status_code=404, detail="invite not found")
    invite = db.query(ReferralInvite).filter(ReferralInvite.code == normalized).one_or_none()
    if invite is None:
        raise HTTPException(status_code=404, detail="invite not found")
    android = db.get(AppVersion, "android")
    store_url = (android.store_url if android is not None else "").strip()
    safe_code = html.escape(normalized)
    safe_store_url = html.escape(store_url, quote=True)
    open_uri = html.escape(f"pixo://invite/{normalized}", quote=True)
    return HTMLResponse(
        content=f"""<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Join Pixo</title>
<style>body{{margin:0;background:#0d100d;color:#f0f3eb;font:16px Inter,Arial,sans-serif}}main{{max-width:390px;min-height:100vh;margin:auto;box-sizing:border-box;padding:52px 24px;background:radial-gradient(circle at top,#1c2812,#0d100d 55%)}}.tag{{color:#c8ff3d;font-size:12px;letter-spacing:.12em;text-transform:uppercase}}h1{{font-size:34px;line-height:1.08;margin:14px 0}}p{{color:#8e9888;line-height:1.5}}.card{{margin-top:28px;padding:20px;border:1px solid #3a4237;border-radius:18px;background:#141a12}}input,button,a{{box-sizing:border-box;width:100%;border-radius:12px;font:inherit}}input{{padding:14px;margin:8px 0;background:#0d100d;color:#f0f3eb;border:1px solid #3a4237}}button,a{{display:block;padding:14px;border:0;text-align:center;text-decoration:none;font-weight:700}}button{{background:#c8ff3d;color:#10140b;cursor:pointer}}a{{margin-top:10px;background:#20281d;color:#f0f3eb}}#code{{display:none}}#message{{min-height:24px;font-size:13px}}</style>
<main><div class=\"tag\">Pixo invite</div><h1>Create together.</h1><p>Create a new Pixo account with this invitation, then sign in to the Android app to activate the invitation.</p><section class=\"card\"><div id=\"step1\"><label>Email<input id=\"email\" type=\"email\" autocomplete=\"email\" placeholder=\"you@example.com\"></label><button id=\"send\">Send code</button></div><div id=\"code\"><label>6-digit code<input id=\"otp\" inputmode=\"numeric\" maxlength=\"6\"></label><button id=\"verify\">Create account</button></div><p id=\"message\"></p><div id=\"finish\" hidden><a href=\"{open_uri}\">Open Pixo</a>{f'<a href="{safe_store_url}">Download Pixo for Android</a>' if safe_store_url else ''}</div></section></main>
<script>const invite={safe_code!r};const message=document.querySelector('#message');const csrf=()=>document.cookie.split('; ').find(x=>x.startsWith('pixo_web_csrf='))?.split('=')[1]||'';async function api(path,body){{await fetch('/api/v1/web/auth/session',{{credentials:'same-origin'}});const r=await fetch(path,{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/json','X-Pixo-CSRF':csrf()}},body:JSON.stringify(body)}});const d=await r.json().catch(()=>({{}}));if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:'Please try again.');return d}}document.querySelector('#send').onclick=async()=>{{try{{await api('/api/v1/web/auth/email/send-code',{{email:document.querySelector('#email').value}});document.querySelector('#code').style.display='block';message.textContent='Check your email for the six-digit code.'}}catch(e){{message.textContent=e.message}}}};document.querySelector('#verify').onclick=async()=>{{try{{await api('/api/v1/web/auth/email/verify',{{email:document.querySelector('#email').value,code:document.querySelector('#otp').value,invite_code:invite}});document.querySelector('#finish').hidden=false;message.textContent='Account created. Sign in to Pixo with this same email to activate the invite.'}}catch(e){{message.textContent=e.message}}}};</script></html>"""
    )
