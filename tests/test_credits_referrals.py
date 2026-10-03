from __future__ import annotations

from app.credits import (
    activate_referral_from_android,
    balance,
    bind_referral_for_new_user,
    ensure_referral_invite,
    grant_welcome_credit,
    release,
    reserve,
    settle,
    update_referral_reward_config,
)
from app.models import CreditLedgerEntry
from app.users import get_or_create_user


def test_credit_reservation_settlement_and_release_are_idempotent(db) -> None:
    user = get_or_create_user(db, provider="email", subject="creator@example.com")
    grant_welcome_credit(db, user.user_id)
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
    assert binding.inviter_reward_credits == 5
    assert binding.invitee_reward_credits == 5
    assert balance(db, inviter.user_id) == 0
    assert balance(db, invitee.user_id) == 5
    assert activate_referral_from_android(db, invitee.user_id) is True
    assert activate_referral_from_android(db, invitee.user_id) is False
    db.commit()
    assert balance(db, inviter.user_id) == 5
    assert balance(db, invitee.user_id) == 5


def test_normal_registration_has_no_welcome_credit(db) -> None:
    user = get_or_create_user(db, provider="email", subject="plain@example.com")
    db.commit()

    assert balance(db, user.user_id) == 0
    assert db.query(CreditLedgerEntry).filter_by(user_id=user.user_id).count() == 0


def test_referral_binding_keeps_reward_snapshot_after_config_change(db) -> None:
    inviter = get_or_create_user(db, provider="email", subject="snapshot-inviter@example.com")
    invite = ensure_referral_invite(db, inviter.user_id)
    first = get_or_create_user(db, provider="email", subject="first@example.com")
    first_binding = bind_referral_for_new_user(db, user=first, code=invite.code)
    update_referral_reward_config(
        db,
        inviter_activation_reward_credits=9,
        invitee_registration_reward_credits=7,
        updated_by="test-admin",
    )
    second = get_or_create_user(db, provider="email", subject="second@example.com")
    second_binding = bind_referral_for_new_user(db, user=second, code=invite.code)
    db.commit()

    assert (first_binding.config_version, first_binding.inviter_reward_credits, first_binding.invitee_reward_credits) == (1, 5, 5)
    assert (second_binding.config_version, second_binding.inviter_reward_credits, second_binding.invitee_reward_credits) == (2, 9, 7)
    assert balance(db, first.user_id) == 5
    assert balance(db, second.user_id) == 7
    activate_referral_from_android(db, first.user_id)
    activate_referral_from_android(db, second.user_id)
    db.commit()
    assert balance(db, inviter.user_id) == 14


def test_zero_referral_rewards_create_no_ledger_entries(db) -> None:
    update_referral_reward_config(
        db,
        inviter_activation_reward_credits=0,
        invitee_registration_reward_credits=0,
        updated_by="test-admin",
    )
    inviter = get_or_create_user(db, provider="email", subject="zero-inviter@example.com")
    invitee = get_or_create_user(db, provider="email", subject="zero-invitee@example.com")
    invite = ensure_referral_invite(db, inviter.user_id)
    bind_referral_for_new_user(db, user=invitee, code=invite.code)
    assert activate_referral_from_android(db, invitee.user_id) is True
    db.commit()

    assert balance(db, inviter.user_id) == 0
    assert balance(db, invitee.user_id) == 0
    assert db.query(CreditLedgerEntry).count() == 0
