from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import UserBlock


def blocked_peer_ids(db: Session, user_id: str | None) -> set[str]:
    """Return peers hidden in either direction for the authenticated viewer."""
    uid = (user_id or "").strip()
    if not uid:
        return set()
    rows = (
        db.query(UserBlock)
        .filter(
            or_(
                UserBlock.blocker_user_id == uid,
                UserBlock.blocked_user_id == uid,
            )
        )
        .all()
    )
    return {
        row.blocked_user_id if row.blocker_user_id == uid else row.blocker_user_id
        for row in rows
    }


def users_blocked_between(db: Session, first_user_id: str, second_user_id: str) -> bool:
    first = first_user_id.strip()
    second = second_user_id.strip()
    if not first or not second or first == second:
        return False
    return (
        db.query(UserBlock.id)
        .filter(
            or_(
                (UserBlock.blocker_user_id == first) & (UserBlock.blocked_user_id == second),
                (UserBlock.blocker_user_id == second) & (UserBlock.blocked_user_id == first),
            )
        )
        .first()
        is not None
    )
