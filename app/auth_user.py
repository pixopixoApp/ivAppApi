from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.logging_config import get_logger
from app.models import User, UserToken
from app.protocol_envelope import resolve_token
from app.schemas import ProtocolHeadIn

log = get_logger(__name__)

AUTH_FAIL_STATUS = 101

_PATH_ACT = {
    "/follow": "follow",
    "/unfollow": "unfollow",
    "/following": "following",
    "/followers": "followers",
    "/profile": "profile",
    "/profile_update": "profile_update",
    "/avatar": "avatar",
    "/video": "video",
    "/video_detail": "video_detail",
    "/track": "track",
    "/impression": "impression",
    "/user_profile": "user_profile",
    "/birthday": "birthday",
    "/deactivate": "deactivate",
    "/user_videos": "user_videos",
    "/my_videos": "my_videos",
    "/following_feed": "following_feed",
}


@dataclass(frozen=True)
class AppUser:
    user_id: str
    provider: str
    subject: str
    token: str
    channel: str = "android"

    @property
    def email(self) -> str | None:
        return self.subject if self.provider == "email" else None


class AppAuthError(Exception):
    def __init__(
        self,
        *,
        act: str,
        head_in: ProtocolHeadIn | None = None,
        status: int = AUTH_FAIL_STATUS,
    ) -> None:
        self.act = act
        self.head_in = head_in
        self.status = status


def load_app_user(db: Session, token: str) -> AppUser | None:
    if not token or token == "anonymous":
        return None
    row = db.get(UserToken, token)
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= now:
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.enabled:
        return None
    return AppUser(
        user_id=user.user_id,
        provider=user.provider,
        subject=user.subject,
        token=row.token,
    )


def issue_user_token(
    db: Session,
    *,
    user_id: str,
    token_ttl_days: int,
    now: datetime | None = None,
    max_sessions: int = 10,
) -> UserToken:
    """Issue one device session without invalidating the user's other devices."""
    issued_at = now or datetime.now(timezone.utc)
    db.query(UserToken).filter(
        UserToken.user_id == user_id,
        UserToken.expires_at <= issued_at,
    ).delete(synchronize_session=False)
    keep_existing = max(0, max_sessions - 1)
    existing = (
        db.query(UserToken)
        .filter(UserToken.user_id == user_id)
        .order_by(UserToken.created_at.desc(), UserToken.token.desc())
        .all()
    )
    for stale in existing[keep_existing:]:
        db.delete(stale)
    row = UserToken(
        token=secrets.token_urlsafe(32),
        user_id=user_id,
        created_at=issued_at,
        expires_at=issued_at + timedelta(days=token_ttl_days),
    )
    db.add(row)
    return row


def resolve_current_user(request: Request, db: Session, token: str) -> AppUser | None:
    """Prefer request.state from router auth; otherwise load from token."""
    cached = getattr(request.state, "app_user", None)
    if isinstance(cached, AppUser):
        return cached
    return load_app_user(db, token)


def _bearer_token(request: Request) -> str:
    raw = (request.headers.get("Authorization") or "").strip()
    if not raw:
        return ""
    scheme, separator, value = raw.partition(" ")
    if not separator or scheme.lower() != "bearer" or not value.strip():
        return ""
    return value.strip()


def bearer_token_from_request(request: Request) -> str:
    return _bearer_token(request)


def resolve_request_token(
    request: Request,
    head: ProtocolHeadIn,
    *,
    act: str | None = None,
) -> str:
    """Accept legacy head.token and Bearer, rejecting ambiguous identities."""
    body_token = resolve_token(head)
    if body_token == "anonymous":
        body_token = ""
    bearer = _bearer_token(request)
    if body_token and bearer and body_token != bearer:
        log.warning("auth token conflict path=%s", request.url.path)
        raise AppAuthError(
            act=act or _PATH_ACT.get(request.url.path, "auth"),
            head_in=head,
            status=AUTH_FAIL_STATUS,
        )
    return bearer or body_token or "anonymous"


def resolve_multipart_token(
    request: Request,
    *,
    form_token: str | None,
    act: str,
) -> str:
    head = ProtocolHeadIn(act=act, token=(form_token or "").strip())
    return resolve_request_token(request, head, act=act)


def _act_from_request(request: Request, head: ProtocolHeadIn) -> str:
    if isinstance(head.act, str) and head.act.strip():
        return head.act.strip()
    return _PATH_ACT.get(request.url.path, "follow")


async def require_app_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Always require valid head.token (for follow / unfollow / following)."""
    raw = await request.body()
    head = ProtocolHeadIn()
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AppAuthError(act=_PATH_ACT.get(request.url.path, "follow")) from None

    if isinstance(data, dict) and isinstance(data.get("head"), dict):
        try:
            head = ProtocolHeadIn.model_validate(data["head"])
        except Exception:  # noqa: BLE001
            head = ProtocolHeadIn()

    act = _act_from_request(request, head)
    token = resolve_request_token(request, head, act=act)
    user = load_app_user(db, token)
    if user is None:
        log.warning(
            "auth failed path=%s token=%s",
            request.url.path,
            token[:8] if token else "",
        )
        raise AppAuthError(act=act, head_in=head, status=AUTH_FAIL_STATUS)

    request.state.app_user = user
    request.state.app_token = token

    async def _receive() -> dict:
        return {"type": "http.request", "body": raw, "more_body": False}

    request._receive = _receive


async def require_bearer_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> AppUser:
    """Authentication dependency for new REST endpoints (Bearer only)."""
    token = _bearer_token(request)
    user = load_app_user(db, token)
    if user is None:
        raise HTTPException(status_code=401, detail="valid Bearer token required")
    request.state.app_user = user
    request.state.app_token = token
    return user
