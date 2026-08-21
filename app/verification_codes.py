from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import Settings
from app.logging_config import get_logger
from app.mail import send_verification_code
from app.models import EmailCode

log = get_logger(__name__)

PURPOSE_LOGIN = "login"
PURPOSE_DEACTIVATE = "deactivate"
ALLOWED_PURPOSES = frozenset({PURPOSE_LOGIN, PURPOSE_DEACTIVATE})


@dataclass(frozen=True)
class IssueCodeResult:
    ok: bool
    retry_after_seconds: int | None = None
    error_code: str | None = None


def issue_email_code(
    db: Session,
    settings: Settings,
    *,
    email: str,
    purpose: str,
) -> IssueCodeResult:
    """Send and then commit a purpose-scoped code.

    The transaction is rolled back when SMTP fails, so a message the user never
    received cannot consume the cooldown window.
    """
    if purpose not in ALLOWED_PURPOSES:
        raise ValueError(f"unsupported verification purpose: {purpose}")

    now = datetime.now(timezone.utc)
    latest = (
        db.query(EmailCode)
        .filter(EmailCode.email == email, EmailCode.purpose == purpose)
        .order_by(EmailCode.created_at.desc())
        .first()
    )
    if latest is not None:
        created = latest.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        elapsed = max(0, int((now - created).total_seconds()))
        interval = settings.send_code_interval_seconds
        if elapsed < interval:
            return IssueCodeResult(
                ok=False,
                retry_after_seconds=max(1, interval - elapsed),
                error_code="CODE_RATE_LIMITED",
            )

    for row in (
        db.query(EmailCode)
        .filter(
            EmailCode.email == email,
            EmailCode.purpose == purpose,
            EmailCode.used_at.is_(None),
        )
        .all()
    ):
        row.used_at = now

    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(
        EmailCode(
            email=email,
            purpose=purpose,
            code=code,
            expires_at=now + timedelta(seconds=settings.code_ttl_seconds),
            created_at=now,
        )
    )
    db.flush()

    try:
        send_verification_code(settings, email=email, code=code)
    except Exception:
        db.rollback()
        log.exception("verification email failed email=%s purpose=%s", email, purpose)
        return IssueCodeResult(ok=False, error_code="EMAIL_UNAVAILABLE")

    db.commit()
    return IssueCodeResult(ok=True)


def find_valid_code(
    db: Session,
    *,
    email: str,
    code: str,
    purpose: str,
    now: datetime | None = None,
) -> EmailCode | None:
    if purpose not in ALLOWED_PURPOSES:
        raise ValueError(f"unsupported verification purpose: {purpose}")
    current = now or datetime.now(timezone.utc)
    row = (
        db.query(EmailCode)
        .filter(
            EmailCode.email == email,
            EmailCode.purpose == purpose,
            EmailCode.code == code,
            EmailCode.used_at.is_(None),
        )
        .order_by(EmailCode.created_at.desc())
        .first()
    )
    if row is None:
        return None
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= current:
        return None
    return row
