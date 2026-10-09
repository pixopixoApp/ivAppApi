from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import (
    CreatorExternalLink,
    CreatorHandleAlias,
    CreatorPinnedWork,
    CreatorProfileAudit,
    CreatorTopic,
    CreatorTopicAssignment,
    PublishedVideo,
    User,
)

HANDLE_RE = re.compile(r"^[a-z0-9_]{3,30}$")
LANGUAGE_RE = re.compile(
    r"^[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|[0-9]{3}))?$"
)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RESERVED_HANDLES = frozenset({
    "admin", "api", "assets", "create", "download", "explore", "help",
    "login", "me", "media", "moderator", "privacy", "settings", "support",
    "terms", "videos", "www", "pixopixo", "pixo",
})
HANDLE_COOLDOWN = timedelta(days=30)
MAX_LINKS = 5
MAX_TOPICS = 3
MAX_PINNED_WORKS = 3


class ChannelValidationError(ValueError):
    def __init__(self, code: str, message: str, **detail: object):
        self.code = code
        self.detail = {"code": code, "message": message, **detail}
        super().__init__(message)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def normalize_handle(raw: str) -> str:
    value = (raw or "").strip().lower().removeprefix("@")
    if not HANDLE_RE.fullmatch(value) or not value.strip("_"):
        raise ChannelValidationError(
            "HANDLE_INVALID",
            "Use 3–30 lowercase letters, numbers, or underscores.",
        )
    if value in RESERVED_HANDLES:
        raise ChannelValidationError("HANDLE_RESERVED", "This handle is reserved.")
    return value


def _nickname_handle(raw: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", raw or "").encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-z0-9_]+", "_", ascii_value.lower()).strip("_")
    return re.sub(r"_+", "_", value)[:30]


def _handle_taken(db: Session, handle: str, *, exclude_user_id: str | None = None) -> bool:
    query = db.query(User.user_id).filter(User.handle == handle)
    if exclude_user_id:
        query = query.filter(User.user_id != exclude_user_id)
    if query.first() is not None:
        return True
    alias = db.get(CreatorHandleAlias, handle)
    return alias is not None and alias.user_id != exclude_user_id


def should_have_public_handle(user: User) -> bool:
    return user.internal_purpose in (None, "", "social_seed")


def ensure_user_handle(db: Session, user: User) -> str:
    if user.handle:
        return user.handle
    if not should_have_public_handle(user):
        return ""
    digest = hashlib.sha256(user.user_id.encode("utf-8")).hexdigest()
    base = _nickname_handle(user.nickname)
    candidates: list[str] = []
    if len(base) >= 3 and base not in RESERVED_HANDLES:
        candidates.extend((base, f"{base[:21]}_{digest[:8]}"))
    candidates.append(f"pixo_{digest[:10]}")
    for candidate in candidates:
        if not _handle_taken(db, candidate):
            user.handle = candidate
            db.add(user)
            return candidate
    for length in range(11, 25):
        candidate = f"pixo_{digest[:length]}"[:30]
        if not _handle_taken(db, candidate):
            user.handle = candidate
            db.add(user)
            return candidate
    raise RuntimeError(f"could not allocate handle for {user.user_id}")


def resolve_handle(db: Session, raw: str) -> tuple[User | None, bool]:
    try:
        handle = normalize_handle(raw)
    except ChannelValidationError:
        return None, False
    user = db.query(User).filter(User.handle == handle).one_or_none()
    if user is not None:
        return user, False
    alias = db.get(CreatorHandleAlias, handle)
    if alias is None:
        return None, False
    return db.get(User, alias.user_id), True


def handle_change_available_at(user: User) -> datetime | None:
    if user.handle_changed_at is None:
        return None
    return _aware(user.handle_changed_at) + HANDLE_COOLDOWN


def check_handle_availability(
    db: Session, raw: str, *, user: User | None = None, now: datetime | None = None
) -> dict[str, object]:
    try:
        handle = normalize_handle(raw)
    except ChannelValidationError as exc:
        return {"handle": (raw or "").strip().lower().removeprefix("@"), "available": False, **exc.detail}
    if user is not None and user.handle == handle:
        return {"handle": handle, "available": True, "code": "HANDLE_UNCHANGED"}
    if _handle_taken(db, handle, exclude_user_id=user.user_id if user else None):
        return {
            "handle": handle, "available": False, "code": "HANDLE_TAKEN",
            "message": "This handle is unavailable.",
        }
    available_at = handle_change_available_at(user) if user else None
    checked_at = now or now_utc()
    if available_at and checked_at < available_at:
        return {
            "handle": handle, "available": False, "code": "HANDLE_COOLDOWN",
            "message": "Your handle can be changed once every 30 days.",
            "available_at": available_at.isoformat(),
        }
    return {"handle": handle, "available": True, "code": "HANDLE_AVAILABLE"}


def change_handle(db: Session, user: User, raw: str, *, now: datetime | None = None) -> bool:
    handle = normalize_handle(raw)
    current = ensure_user_handle(db, user)
    if current == handle:
        return False
    availability = check_handle_availability(db, handle, user=user, now=now)
    if not availability["available"]:
        raise ChannelValidationError(
            str(availability["code"]),
            str(availability.get("message") or "This handle is unavailable."),
            **({"available_at": availability["available_at"]} if "available_at" in availability else {}),
        )
    changed_at = now or now_utc()
    if current:
        db.add(CreatorHandleAlias(handle=current, user_id=user.user_id, created_at=changed_at))
    user.handle = handle
    user.handle_changed_at = changed_at
    user.profile_updated_at = changed_at
    db.add(user)
    return True


def visible_length(value: str) -> int:
    return len(value)


def normalize_channel_name(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise ChannelValidationError("NAME_REQUIRED", "Enter a channel name.")
    if visible_length(value) > 40:
        raise ChannelValidationError("NAME_TOO_LONG", "Channel name must be at most 40 characters.")
    return value


def normalize_channel_bio(raw: str) -> str:
    value = (raw or "").strip()
    if visible_length(value) > 300:
        raise ChannelValidationError("BIO_TOO_LONG", "Bio must be at most 300 characters.")
    return value


def normalize_language(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    parts = value.replace("_", "-").split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        normalized.append(part.title() if len(part) == 4 else part.upper())
    value = "-".join(normalized)
    if not LANGUAGE_RE.fullmatch(value):
        raise ChannelValidationError("LANGUAGE_INVALID", "Choose a valid content language.")
    if value == "zh":
        raise ChannelValidationError(
            "LANGUAGE_INVALID", "Choose Simplified Chinese or Traditional Chinese."
        )
    return value


def normalize_email(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    if value and (len(value) > 256 or not EMAIL_RE.fullmatch(value)):
        raise ChannelValidationError("EMAIL_INVALID", "Enter a valid collaboration email.")
    return value


def normalize_links(raw_links: list[dict[str, object]]) -> list[dict[str, str]]:
    if len(raw_links) > MAX_LINKS:
        raise ChannelValidationError("LINK_LIMIT", "Add at most 5 external links.")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_links:
        label = str(raw.get("label") or "").strip()
        url = str(raw.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ChannelValidationError("LINK_INVALID", "External links must use HTTPS.")
        if len(label) > 40 or len(url) > 2048:
            raise ChannelValidationError("LINK_INVALID", "An external link is too long.")
        if url in seen:
            continue
        seen.add(url)
        result.append({"label": label, "url": url})
    return result


def visible_video_query(db: Session, user_id: str):
    return db.query(PublishedVideo).filter(
        PublishedVideo.user_id == user_id,
        PublishedVideo.is_deleted == 0,
        PublishedVideo.deleted_at.is_(None),
        PublishedVideo.review_status == "approved",
        PublishedVideo.distribution_enabled.is_(True),
        PublishedVideo.cdn_ready.is_(True),
    )


def clean_invalid_pins(db: Session, user_id: str) -> list[CreatorPinnedWork]:
    rows = db.query(CreatorPinnedWork).filter(
        CreatorPinnedWork.user_id == user_id
    ).order_by(CreatorPinnedWork.position.asc()).all()
    if not rows:
        return []
    visible_ids = {
        value for (value,) in visible_video_query(db, user_id).filter(
            PublishedVideo.id.in_([row.video_id for row in rows])
        ).with_entities(PublishedVideo.id).all()
    }
    valid: list[CreatorPinnedWork] = []
    for row in rows:
        if row.video_id not in visible_ids:
            db.delete(row)
        else:
            row.position = len(valid)
            valid.append(row)
    return valid


def replace_links(db: Session, user_id: str, links: list[dict[str, object]]) -> None:
    normalized = normalize_links(links)
    db.query(CreatorExternalLink).filter(CreatorExternalLink.user_id == user_id).delete(
        synchronize_session=False
    )
    now = now_utc()
    for position, link in enumerate(normalized):
        db.add(CreatorExternalLink(
            user_id=user_id, label=link["label"], url=link["url"], position=position,
            created_at=now, updated_at=now,
        ))


def replace_pins(db: Session, user_id: str, video_ids: list[str]) -> None:
    ordered = list(dict.fromkeys(str(value).strip() for value in video_ids if str(value).strip()))
    if len(ordered) > MAX_PINNED_WORKS:
        raise ChannelValidationError("PIN_LIMIT", "Pin at most 3 works.")
    visible = {
        value for (value,) in visible_video_query(db, user_id).filter(
            PublishedVideo.id.in_(ordered)
        ).with_entities(PublishedVideo.id).all()
    } if ordered else set()
    if set(ordered) != visible:
        raise ChannelValidationError(
            "PIN_INVALID", "Pinned works must be your currently public works."
        )
    db.query(CreatorPinnedWork).filter(CreatorPinnedWork.user_id == user_id).delete(
        synchronize_session=False
    )
    for position, video_id in enumerate(ordered):
        db.add(CreatorPinnedWork(user_id=user_id, video_id=video_id, position=position))


def unpin_video(db: Session, video_id: str) -> None:
    rows = db.query(CreatorPinnedWork).filter(
        CreatorPinnedWork.video_id == video_id
    ).all()
    affected = {row.user_id for row in rows}
    for row in rows:
        db.delete(row)
    for user_id in affected:
        remaining = db.query(CreatorPinnedWork).filter(
            CreatorPinnedWork.user_id == user_id,
            CreatorPinnedWork.video_id != video_id,
        ).order_by(CreatorPinnedWork.position.asc()).all()
        for position, row in enumerate(remaining):
            row.position = position


def replace_topics(db: Session, user_id: str, topic_ids: list[str]) -> None:
    ordered = list(dict.fromkeys(str(value).strip() for value in topic_ids if str(value).strip()))
    if len(ordered) > MAX_TOPICS:
        raise ChannelValidationError("TOPIC_LIMIT", "Choose at most 3 topics.")
    enabled = {
        value for (value,) in db.query(CreatorTopic.id).filter(
            CreatorTopic.id.in_(ordered), CreatorTopic.enabled.is_(True),
            CreatorTopic.archived_at.is_(None),
        ).all()
    } if ordered else set()
    if set(ordered) != enabled:
        raise ChannelValidationError("TOPIC_INVALID", "Choose only active creator topics.")
    db.query(CreatorTopicAssignment).filter(
        CreatorTopicAssignment.user_id == user_id
    ).delete(synchronize_session=False)
    for position, topic_id in enumerate(ordered):
        db.add(CreatorTopicAssignment(
            user_id=user_id, topic_id=topic_id, position=position
        ))


def channel_relations(db: Session, user_id: str) -> tuple[list[dict], list[dict], list[str]]:
    links = [
        {"label": row.label, "url": row.url, "position": row.position}
        for row in db.query(CreatorExternalLink).filter(
            CreatorExternalLink.user_id == user_id
        ).order_by(CreatorExternalLink.position.asc()).all()
    ]
    topic_rows = db.query(CreatorTopicAssignment, CreatorTopic).join(
        CreatorTopic, CreatorTopic.id == CreatorTopicAssignment.topic_id
    ).filter(
        CreatorTopicAssignment.user_id == user_id,
        CreatorTopic.enabled.is_(True),
        CreatorTopic.archived_at.is_(None),
    ).order_by(CreatorTopicAssignment.position.asc()).all()
    topics = [
        {"id": topic.id, "name": topic.name}
        for _assignment, topic in topic_rows
    ]
    pins = clean_invalid_pins(db, user_id)
    return links, topics, [row.video_id for row in pins]


def snapshot(db: Session, user: User) -> dict[str, object]:
    links, topics, pins = channel_relations(db, user.user_id)
    return {
        "handle": user.handle,
        "nickname": user.nickname,
        "bio": user.bio,
        "background_url": user.background_url,
        "background_mobile_url": user.background_mobile_url,
        "background_desktop_url": user.background_desktop_url,
        "background_focus_x": user.background_focus_x,
        "background_focus_y": user.background_focus_y,
        "content_language": user.content_language,
        "collaboration_email": user.collaboration_email,
        "collaboration_email_public": user.collaboration_email_public,
        "links": links,
        "topics": topics,
        "pinned_video_ids": pins,
    }


def audit(
    db: Session, *, user_id: str | None, action: str, actor_id: str,
    actor_role: str, source: str, before: dict | None, after: dict | None,
) -> None:
    db.add(CreatorProfileAudit(
        user_id=user_id, action=action, actor_id=actor_id,
        actor_role=actor_role, source=source,
        before_json=before, after_json=after,
    ))


def new_topic_id() -> str:
    return f"topic_{secrets.token_urlsafe(12)}"


def search_creators(db: Session, query: str, *, limit: int, offset: int) -> tuple[list[User], int]:
    value = (query or "").strip()
    rows = db.query(User).filter(User.enabled.is_(True), User.deletion_requested_at.is_(None))
    if value:
        like = f"%{value}%"
        rows = rows.filter(or_(User.user_id.like(like), User.nickname.like(like), User.handle.like(like)))
    total = rows.count()
    return rows.order_by(User.created_at.desc(), User.user_id.asc()).offset(offset).limit(limit).all(), total
