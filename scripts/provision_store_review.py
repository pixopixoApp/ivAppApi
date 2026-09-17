"""Run in the production API container, with the review identity JSON on stdin."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from app.config import get_settings
from app.credits import balance
from app.db import SessionLocal
from app.models import CreatorAccessGrant, CreditLedgerEntry
from app.users import get_or_create_user


def main() -> None:
    assert get_settings().pixo_environment == "production"
    identity = json.load(sys.stdin)
    email = identity["email"]
    assert email == "app-review@pixopixo.com"
    with SessionLocal() as db:
        user = get_or_create_user(db, provider="email", subject=email)
        assert user.enabled and user.nickname in ("", "Pixo Review")
        user.nickname = "Pixo Review"
        user.birthday = "1990-01-01"
        if db.get(CreatorAccessGrant, user.user_id) is None:
            db.add(CreatorAccessGrant(user_id=user.user_id, source="store_review"))
        # One bounded, idempotent allocation; repeated login never tops up Credits.
        entry_id = f"store-review-initial:{user.user_id}"
        if db.get(CreditLedgerEntry, entry_id) is None:
            db.add(CreditLedgerEntry(
                id=entry_id,
                user_id=user.user_id,
                kind="admin_grant",
                amount=max(0, 100 - balance(db, user.user_id)),
                reference_id="store-review-initial",
                note="App store review testing",
                created_at=datetime.now(timezone.utc),
            ))
        db.commit()
        print(json.dumps({
            "email": email,
            "user_id": user.user_id,
            "birthday_complete": True,
            "creator_access": True,
            "credits": balance(db, user.user_id),
        }))


if __name__ == "__main__":
    main()
