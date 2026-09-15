"""An explicitly configured, reusable login code for one store-review identity.

The account must already exist. This does not provision identities, elevate
permissions, skip disabled-account checks, or authorize account deletion.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.orm import Session

from app.models import User


class ReviewLoginSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_REVIEW_LOGIN_",
        env_file=str(Path(__file__).resolve().parent.parent / ".env"),
        extra="ignore",
    )
    enabled: bool = False
    email: str = ""
    code_hash: str = ""


@lru_cache
def get_review_login_settings() -> ReviewLoginSettings:
    return ReviewLoginSettings()


def is_review_login_email(email: str) -> bool:
    settings = get_review_login_settings()
    configured = settings.email.strip().lower()
    return bool(settings.enabled and configured and email.strip().lower() == configured)


def review_login_user(db: Session, email: str) -> User | None:
    if not is_review_login_email(email):
        return None
    return (
        db.query(User)
        .filter(
            User.provider == "email",
            User.subject == email.strip().lower(),
            User.enabled.is_(True),
        )
        .one_or_none()
    )


def matches_review_login_code(code: str) -> bool:
    if not re.fullmatch(r"[0-9]{6}", code):
        return False
    encoded = get_review_login_settings().code_hash
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or int(iterations) != 200_000:
            return False
        salt_bytes = bytes.fromhex(salt)
        expected_bytes = bytes.fromhex(expected)
        if len(salt_bytes) != 16 or len(expected_bytes) != 32:
            return False
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", code.encode("ascii"), salt_bytes, 200_000)
    return hmac.compare_digest(actual, expected_bytes)


@dataclass
class ReviewLoginCode:
    """Request-local receipt; marking it used does not consume the fixed code."""
    used_at: datetime | None = None
