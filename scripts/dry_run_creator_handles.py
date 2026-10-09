"""Report the deterministic creator-handle backfill without changing the database."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import func

from app.creator_channels import (
    RESERVED_HANDLES,
    _nickname_handle,
    should_have_public_handle,
)
from app.db import SessionLocal
from app.models import CreatorHandleAlias, User


def main() -> None:
    with SessionLocal() as db:
        users = db.query(User).order_by(User.created_at.asc(), User.user_id.asc()).all()
        occupied = {value for (value,) in db.query(User.handle).filter(User.handle.is_not(None)).all() if value}
        occupied.update(value for (value,) in db.query(CreatorHandleAlias.handle).all())
        generated: list[dict[str, str]] = []
        excluded = 0
        conflicts = 0
        for user in users:
            if user.handle:
                continue
            if not should_have_public_handle(user):
                excluded += 1
                continue
            digest = hashlib.sha256(user.user_id.encode("utf-8")).hexdigest()
            base = _nickname_handle(user.nickname)
            candidates = []
            if len(base) >= 3 and base not in RESERVED_HANDLES:
                candidates.extend((base, f"{base[:21]}_{digest[:8]}"))
            candidates.append(f"pixo_{digest[:10]}")
            handle = next((value for value in candidates if value not in occupied), "")
            if not handle:
                conflicts += 1
                handle = f"pixo_{digest[:24]}"[:30]
            occupied.add(handle)
            generated.append({"user_id": user.user_id, "handle": handle})

        duplicates = db.query(User.handle).filter(User.handle.is_not(None)).group_by(
            User.handle
        ).having(func.count(User.user_id) > 1).count()
        print(json.dumps({
            "account_count": len(users),
            "generated_handle_count": len(generated),
            "conflict_fallback_count": conflicts,
            "excluded_account_count": excluded,
            "duplicate_count": duplicates,
            "sample": generated[:20],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
