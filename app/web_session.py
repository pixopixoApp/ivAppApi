from __future__ import annotations

import secrets
from dataclasses import replace
from datetime import datetime, timezone
from hmac import compare_digest
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth_user import AppUser, bearer_token_from_request, load_app_user
from app.config import Settings, get_settings
from app.db import get_db
from app.models import CreatorAccessGrant

WEB_SESSION_COOKIE = "pixo_web_session"
WEB_CSRF_COOKIE = "pixo_web_csrf"
WEB_CSRF_HEADER = "X-Pixo-CSRF"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _secure_cookie(settings: Settings) -> bool:
    return settings.pixo_environment == "production"


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        WEB_CSRF_COOKIE,
        token,
        max_age=max(300, settings.token_ttl_days * 86400),
        secure=_secure_cookie(settings),
        httponly=False,
        samesite="lax",
        path="/",
    )


def set_session_cookies(
    response: Response,
    settings: Settings,
    *,
    session_token: str,
    csrf_token: str,
) -> None:
    max_age = max(300, settings.token_ttl_days * 86400)
    response.set_cookie(
        WEB_SESSION_COOKIE,
        session_token,
        max_age=max_age,
        secure=_secure_cookie(settings),
        httponly=True,
        samesite="lax",
        path="/",
    )
    set_csrf_cookie(response, settings, csrf_token)


def clear_session_cookies(response: Response, settings: Settings) -> None:
    for name in (WEB_SESSION_COOKIE, WEB_CSRF_COOKIE):
        response.delete_cookie(
            name,
            secure=_secure_cookie(settings),
            httponly=name == WEB_SESSION_COOKIE,
            samesite="lax",
            path="/",
        )


def verify_web_csrf(request: Request) -> None:
    if request.method.upper() in _SAFE_METHODS:
        return
    cookie = (request.cookies.get(WEB_CSRF_COOKIE) or "").strip()
    header = (request.headers.get(WEB_CSRF_HEADER) or "").strip()
    if not cookie or not header or not compare_digest(cookie, header):
        raise HTTPException(status_code=403, detail="invalid CSRF token")


def optional_web_user(request: Request, db: Session) -> AppUser | None:
    token = (request.cookies.get(WEB_SESSION_COOKIE) or "").strip()
    user = load_app_user(db, token)
    return replace(user, channel="web") if user is not None else None


def require_web_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> AppUser:
    verify_web_csrf(request)
    user = optional_web_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    request.state.app_user = user
    request.state.app_token = user.token
    return user


def _policy_allows(settings: Settings, channel: str) -> bool:
    mode = settings.creator_access_mode
    return mode == "all_open" or mode == f"{channel}_open"


def _grant_policy_access(
    db: Session,
    settings: Settings,
    user: AppUser,
) -> None:
    if db.get(CreatorAccessGrant, user.user_id) is not None:
        return
    if not _policy_allows(settings, user.channel):
        return
    db.add(
        CreatorAccessGrant(
            user_id=user.user_id,
            source=f"{user.channel}_open",
            granted_at=datetime.now(timezone.utc),
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.get(CreatorAccessGrant, user.user_id) is None:
            raise


def require_creator_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AppUser:
    bearer = bearer_token_from_request(request)
    if bearer:
        loaded = load_app_user(db, bearer)
        if loaded is None:
            raise HTTPException(status_code=401, detail="valid Bearer token required")
        user = replace(loaded, channel="android")
    else:
        verify_web_csrf(request)
        user = optional_web_user(request, db)
        if user is None:
            raise HTTPException(status_code=401, detail="sign in required")
    _grant_policy_access(db, settings, user)
    request.state.app_user = user
    request.state.app_token = user.token
    return user

