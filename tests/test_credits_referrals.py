from __future__ import annotations

from app.credits import (
    activate_referral_from_android,
    balance,
    bind_referral_for_new_user,
    ensure_referral_invite,
    release,
    reserve,
    settle,
)
from app.users import get_or_create_user


def test_credit_reservation_settlement_and_release_are_idempotent(db) -> None:
    user = get_or_create_user(db, provider="email", subject="creator@example.com")
    db.commit()
    assert balance(db, user.user_id) == 5

    failed = reserve(
        db,
        user_id=user.user_id,
        reference_id="source:csg-one",
        purpose="AI source video · 5 seconds",
        amount=5,
    )
    assert balance(db, user.user_id) == 0
    release(db, failed)
    release(db, failed)
    db.commit()
    assert balance(db, user.user_id) == 5

    source = reserve(
        db,
        user_id=user.user_id,
        reference_id="source:csg-two",
        purpose="AI source video · 5 seconds",
        amount=5,
    )
    settle(db, source)
    settle(db, source)
    db.commit()
    assert balance(db, user.user_id) == 0


def test_referral_activates_once_after_android_login(db) -> None:
    inviter = get_or_create_user(db, provider="email", subject="inviter@example.com")
    invitee = get_or_create_user(db, provider="email", subject="invitee@example.com")
    invite = ensure_referral_invite(db, inviter.user_id)
    binding = bind_referral_for_new_user(db, user=invitee, code=invite.code)
    db.commit()

    assert binding.status == "pending_activation"
    assert balance(db, inviter.user_id) == 5
    assert activate_referral_from_android(db, invitee.user_id) is True
    assert activate_referral_from_android(db, invitee.user_id) is False
    db.commit()
    assert balance(db, inviter.user_id) == 15
    assert balance(db, invitee.user_id) == 5
