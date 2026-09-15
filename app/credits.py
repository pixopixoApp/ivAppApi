from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    CreditLedgerEntry,
    CreditReservation,
    ReferralBinding,
    ReferralInvite,
    User,
)

WELCOME_CREDITS = 5
REFERRAL_CREDITS = 10
_REFERRAL_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class InsufficientCredits(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def balance(db: Session, user_id: str) -> int:
    return int(
        db.query(func.coalesce(func.sum(CreditLedgerEntry.amount), 0))
        .filter(CreditLedgerEntry.user_id == user_id)
        .scalar()
        or 0
    )


def _entry(
    db: Session,
    *,
    entry_id: str,
    user_id: str,
    kind: str,
    amount: int,
    reference_id: str = "",
    reservation_id: str | None = None,
    note: str = "",
) -> None:
    if db.get(CreditLedgerEntry, entry_id) is not None:
        return
    db.add(
        CreditLedgerEntry(
            id=entry_id,
            user_id=user_id,
            kind=kind,
            amount=amount,
            reference_id=reference_id,
            reservation_id=reservation_id,
            note=note,
            created_at=_now(),
        )
    )


def grant_welcome_credit(db: Session, user_id: str) -> None:
    _entry(
        db,
        entry_id=f"welcome:{user_id}",
        user_id=user_id,
        kind="welcome",
        amount=WELCOME_CREDITS,
        reference_id=user_id,
        note="Welcome Credits",
    )


def _new_referral_code() -> str:
    compact = "".join(secrets.choice(_REFERRAL_ALPHABET) for _ in range(10))
    return f"{compact[:5]}-{compact[5:]}"


def ensure_referral_invite(db: Session, user_id: str) -> ReferralInvite:
    existing = db.get(ReferralInvite, user_id)
    if existing is not None:
        return existing
    for _ in range(10):
        code = _new_referral_code()
        if db.query(ReferralInvite).filter(ReferralInvite.code == code).first() is None:
            row = ReferralInvite(owner_user_id=user_id, code=code, created_at=_now())
            db.add(row)
            db.flush()
            return row
    raise RuntimeError("could not allocate a referral code")


def provision_new_user(db: Session, user: User) -> None:
    """Give a newly verified user its one-time grant and personal invite code."""
    grant_welcome_credit(db, user.user_id)
    ensure_referral_invite(db, user.user_id)


def reserve(
    db: Session,
    *,
    user_id: str,
    reference_id: str,
    purpose: str,
    amount: int,
) -> CreditReservation:
    existing = (
        db.query(CreditReservation)
        .filter(
            CreditReservation.user_id == user_id,
            CreditReservation.reference_id == reference_id,
        )
        .one_or_none()
    )
    if existing is not None:
        return existing
    if amount <= 0:
        raise ValueError("reservation amount must be positive")
    if balance(db, user_id) < amount:
        raise InsufficientCredits("not enough Credits")
    row = CreditReservation(
        id=f"cred_{secrets.token_urlsafe(18)}",
        user_id=user_id,
        reference_id=reference_id,
        purpose=purpose,
        amount=amount,
        status="reserved",
        created_at=_now(),
    )
    db.add(row)
    db.flush()
    _entry(
        db,
        entry_id=f"hold:{row.id}",
        user_id=user_id,
        kind="hold",
        amount=-amount,
        reference_id=reference_id,
        reservation_id=row.id,
        note=purpose,
    )
    db.flush()
    return row


def settle(db: Session, reservation: CreditReservation) -> None:
    if reservation.status != "reserved":
        return
    reservation.status = "settled"
    reservation.settled_at = _now()
    _entry(
        db,
        entry_id=f"settle:{reservation.id}",
        user_id=reservation.user_id,
        kind="settle",
        amount=0,
        reference_id=reservation.reference_id,
        reservation_id=reservation.id,
        note=reservation.purpose,
    )
    db.add(reservation)


def release(db: Session, reservation: CreditReservation) -> None:
    if reservation.status != "reserved":
        return
    reservation.status = "released"
    reservation.released_at = _now()
    _entry(
        db,
        entry_id=f"release:{reservation.id}",
        user_id=reservation.user_id,
        kind="release",
        amount=reservation.amount,
        reference_id=reservation.reference_id,
        reservation_id=reservation.id,
        note=reservation.purpose,
    )
    db.add(reservation)


def settle_reference(db: Session, *, user_id: str, reference_id: str) -> None:
    row = (
        db.query(CreditReservation)
        .filter(
            CreditReservation.user_id == user_id,
            CreditReservation.reference_id == reference_id,
        )
        .one_or_none()
    )
    if row is not None:
        settle(db, row)


def release_reference(db: Session, *, user_id: str, reference_id: str) -> None:
    row = (
        db.query(CreditReservation)
        .filter(
            CreditReservation.user_id == user_id,
            CreditReservation.reference_id == reference_id,
        )
        .one_or_none()
    )
    if row is not None:
        release(db, row)


def bind_referral_for_new_user(db: Session, *, user: User, code: str) -> ReferralBinding:
    normalized = code.strip().upper()
    invite = db.query(ReferralInvite).filter(ReferralInvite.code == normalized).one_or_none()
    if invite is None:
        raise ValueError("invite code is invalid")
    if invite.owner_user_id == user.user_id:
        raise ValueError("you cannot use your own invite code")
    existing = db.get(ReferralBinding, user.user_id)
    if existing is not None:
        if existing.inviter_user_id != invite.owner_user_id:
            raise ValueError("this account already has an invite")
        return existing
    row = ReferralBinding(
        invitee_user_id=user.user_id,
        inviter_user_id=invite.owner_user_id,
        invite_code=invite.code,
        status="pending_activation",
        created_at=_now(),
    )
    db.add(row)
    return row


def activate_referral_from_android(db: Session, user_id: str) -> bool:
    row = db.get(ReferralBinding, user_id)
    if row is None or row.status != "pending_activation":
        return False
    if db.get(User, row.inviter_user_id) is None:
        return False
    grant_id = f"referral:{row.invitee_user_id}"
    _entry(
        db,
        entry_id=grant_id,
        user_id=row.inviter_user_id,
        kind="referral",
        amount=REFERRAL_CREDITS,
        reference_id=row.invitee_user_id,
        note="Invite activation",
    )
    row.status = "activated"
    row.activated_at = _now()
    db.add(row)
    return True


def referral_code_fingerprint(code: str) -> str:
    """Safe identifier for logs; never log the complete referral code."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]
