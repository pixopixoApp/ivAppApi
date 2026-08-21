from __future__ import annotations

import secrets
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.models import Follow, User

MIN_ACCOUNT_AGE_YEARS = 13

USER_SOURCE_APP = "app"
USER_SOURCE_ADMIN = "admin"
_ALLOWED_SOURCES = frozenset({USER_SOURCE_APP, USER_SOURCE_ADMIN})


class UserIdentityConflict(Exception):
    def __init__(self, other_user_id: str):
        self.other_user_id = other_user_id
        super().__init__(f"provider+subject already used by user_id={other_user_id}")


def new_user_id() -> str:
    return secrets.token_urlsafe(16)


def get_or_create_user(db: Session, *, provider: str, subject: str) -> User:
    row = (
        db.query(User)
        .filter(User.provider == provider, User.subject == subject)
        .one_or_none()
    )
    if row is not None:
        return row
    user = User(
        user_id=new_user_id(),
        provider=provider,
        subject=subject,
        enabled=True,
        nickname="",
        avatar_url="",
        birthday="",
        source=USER_SOURCE_APP,
    )
    db.add(user)
    db.flush()
    return user


def is_author_visible(db: Session, user_id: str | None) -> bool:
    """Empty author (legacy publish) is visible; disabled/missing author is not."""
    if user_id is None or not str(user_id).strip():
        return True
    row = db.get(User, user_id.strip())
    return row is not None and bool(row.enabled)


def normalize_relative_avatar(raw: str) -> str:
    """Empty or path starting with '/'; raise ValueError if invalid."""
    s = raw.strip() if isinstance(raw, str) else ""
    if not s:
        return ""
    if not s.startswith("/"):
        raise ValueError("avatar_url must be a relative path starting with /")
    if len(s) > 512:
        raise ValueError("avatar_url too long")
    return s


def normalize_nickname(raw: str) -> str:
    s = raw.strip() if isinstance(raw, str) else ""
    if len(s) > 64:
        raise ValueError("nickname too long")
    return s


def normalize_bio(raw: str) -> str:
    s = raw.strip() if isinstance(raw, str) else ""
    if len(s) > 80:
        raise ValueError("bio too long")
    return s


def normalize_birthday(raw: str) -> str:
    """Require YYYY-MM-DD; raise ValueError if invalid."""
    s = raw.strip() if isinstance(raw, str) else ""
    if not s:
        raise ValueError("birthday required")
    try:
        born = date.fromisoformat(s)
    except ValueError as exc:
        raise ValueError("birthday must be YYYY-MM-DD") from exc
    if born > datetime.now(timezone.utc).date():
        raise ValueError("birthday cannot be in the future")
    return s


def age_years(birthday_yyyy_mm_dd: str, *, today: date | None = None) -> int:
    s = normalize_birthday(birthday_yyyy_mm_dd)
    born = date.fromisoformat(s)
    on = today or datetime.now(timezone.utc).date()
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))


def is_under_13(user: User, *, today: date | None = None) -> bool | None:
    birthday = (user.birthday or "").strip()
    if not birthday:
        return None
    return age_years(birthday, today=today) < MIN_ACCOUNT_AGE_YEARS


def assert_min_age(
    birthday_yyyy_mm_dd: str,
    *,
    min_years: int = MIN_ACCOUNT_AGE_YEARS,
    today: date | None = None,
) -> None:
    """Raise ValueError if age on ``today`` (UTC) is under min_years."""
    s = normalize_birthday(birthday_yyyy_mm_dd)
    if age_years(s, today=today) < min_years:
        raise ValueError("age below minimum")


def needs_birthday(user: User) -> bool:
    return not bool((user.birthday or "").strip())


def follow_counts(db: Session, user_id: str) -> tuple[int, int]:
    """Return (following_count, follower_count) for user_id."""
    uid = user_id.strip()
    following = (
        db.query(Follow).filter(Follow.follower_user_id == uid).count()
    )
    followers = (
        db.query(Follow).filter(Follow.followee_user_id == uid).count()
    )
    return following, followers


def is_following(db: Session, *, follower_user_id: str, followee_user_id: str) -> bool:
    if not follower_user_id or not followee_user_id or follower_user_id == followee_user_id:
        return False
    row = (
        db.query(Follow)
        .filter(
            Follow.follower_user_id == follower_user_id,
            Follow.followee_user_id == followee_user_id,
        )
        .one_or_none()
    )
    return row is not None


def apply_user_update(
    db: Session,
    *,
    user_id: str,
    provider: str | None = None,
    subject: str | None = None,
    enabled: bool | None = None,
    nickname: str | None = None,
    avatar_url: str | None = None,
    bio: str | None = None,
    source: str | None = None,
    create_if_missing: bool = False,
) -> User:
    """Upsert/patch user. None means leave field unchanged. Empty str clears nickname/avatar.

    ``source`` is set only on create (immutable afterward). Admin create defaults to
    ``admin``; App register uses ``get_or_create_user`` → ``app``.
    """
    uid = user_id.strip()
    if not uid:
        raise ValueError("user_id required")

    row = db.get(User, uid)
    if row is None:
        if not create_if_missing:
            raise LookupError(uid)
        p = (provider or "email").strip() or "email"
        s = (subject or "").strip()
        if not s:
            raise ValueError("subject required to create user")
        _assert_identity_free(db, provider=p, subject=s, exclude_user_id=uid)
        src = (source or USER_SOURCE_ADMIN).strip() or USER_SOURCE_ADMIN
        if src not in _ALLOWED_SOURCES:
            raise ValueError("source must be app or admin")
        now = datetime.now(timezone.utc)
        row = User(
            user_id=uid,
            provider=p,
            subject=s,
            enabled=True if enabled is None else bool(enabled),
            nickname="" if nickname is None else normalize_nickname(nickname),
            avatar_url="" if avatar_url is None else normalize_relative_avatar(avatar_url),
            bio="" if bio is None else normalize_bio(bio),
            birthday="",
            source=src,
            created_at=now,
        )
        db.add(row)
        db.flush()
        return row

    new_provider = row.provider if provider is None else ((provider or "email").strip() or "email")
    new_subject = row.subject if subject is None else subject.strip()
    if not new_subject:
        raise ValueError("subject required")
    if new_provider != row.provider or new_subject != row.subject:
        _assert_identity_free(db, provider=new_provider, subject=new_subject, exclude_user_id=uid)
        row.provider = new_provider
        row.subject = new_subject

    if enabled is not None:
        row.enabled = bool(enabled)
    if nickname is not None:
        row.nickname = normalize_nickname(nickname)
    if avatar_url is not None:
        row.avatar_url = normalize_relative_avatar(avatar_url)
    if bio is not None:
        row.bio = normalize_bio(bio)
    # source is immutable after create

    db.flush()
    return row


def _assert_identity_free(
    db: Session,
    *,
    provider: str,
    subject: str,
    exclude_user_id: str,
) -> None:
    conflict = (
        db.query(User)
        .filter(
            User.provider == provider,
            User.subject == subject,
            User.user_id != exclude_user_id,
        )
        .one_or_none()
    )
    if conflict is not None:
        raise UserIdentityConflict(conflict.user_id)


def to_profile_fields(user: User) -> dict[str, str | bool]:
    """Shared profile payload for App and admin serializers."""
    return {
        "user_id": user.user_id,
        "nickname": user.nickname or "",
        "avatar_url": user.avatar_url or "",
        "bio": user.bio or "",
        "email": user.subject if user.provider == "email" else "",
        "enabled": bool(user.enabled),
        "birthday": user.birthday or "",
        "provider": user.provider,
        "subject": user.subject,
        "source": user.source or USER_SOURCE_APP,
    }
