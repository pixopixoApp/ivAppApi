from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Request
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


def resolve_current_user(request: Request, db: Session, token: str) -> AppUser | None:
    """Prefer request.state from router auth; otherwise load from token."""
    cached = getattr(request.state, "app_user", None)
    if isinstance(cached, AppUser):
        return cached
    return load_app_user(db, token)


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
    token = resolve_token(head)
    user = load_app_user(db, token)
    if user is None:
        log.warning(
            "auth failed path=%s token=%s",
            request.url.path,
            token[:8] if token else "",
        )
        raise AppAuthError(act=act, head_in=head, status=AUTH_FAIL_STATUS)

    request.state.app_user = user

    async def _receive() -> dict:
        return {"type": "http.request", "body": raw, "more_body": False}

    request._receive = _receive  # noqa: SLF001 — replay body for FastAPI parsing
