from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.models import EmailCode, User, UserToken
from app.review_login import get_review_login_settings
from app.verification_codes import (
    PURPOSE_DEACTIVATE,
    PURPOSE_LOGIN,
    find_valid_code,
    issue_email_code,
)

EMAIL = "store-review@example.com"
CODE = "482731"


@pytest.fixture(autouse=True)
def reset_review_config(monkeypatch):
    for suffix in ("ENABLED", "EMAIL", "CODE_HASH"):
        monkeypatch.delenv(f"APP_REVIEW_LOGIN_{suffix}", raising=False)
    get_review_login_settings.cache_clear()
    yield
    get_review_login_settings.cache_clear()


def configure(monkeypatch, *, enabled=True, encoded=None):
    salt = bytes(range(16))
    digest = hashlib.pbkdf2_hmac("sha256", CODE.encode(), salt, 200_000).hex()
    monkeypatch.setenv("APP_REVIEW_LOGIN_ENABLED", str(enabled).lower())
    monkeypatch.setenv("APP_REVIEW_LOGIN_EMAIL", EMAIL)
    monkeypatch.setenv(
        "APP_REVIEW_LOGIN_CODE_HASH",
        encoded if encoded is not None else f"pbkdf2_sha256$200000${salt.hex()}${digest}",
    )
    get_review_login_settings.cache_clear()


def account(db, *, enabled=True):
    user = User(
        user_id="store-review",
        provider="email",
        subject=EMAIL,
        nickname="Pixo Review",
        birthday="1990-01-01",
        enabled=enabled,
    )
    db.add(user)
    db.commit()
    return user


def request(act, *, email=EMAIL, code=CODE):
    body = {"email": email}
    if act == "verify":
        body["code"] = code
    return {"head": {"act": act, "ver": "1.2"}, "body": body}


def test_review_login_is_reusable_without_mail_or_consumed_otp(db, monkeypatch):
    configure(monkeypatch)
    account(db)

    def no_mail(*args, **kwargs):
        raise AssertionError("Review login must not require inbox access.")

    monkeypatch.setattr("app.verification_codes.send_verification_code", no_mail)
    with TestClient(app) as client:
        for _ in range(2):
            sent = client.post("/send_code", json=request("send_code", email=EMAIL.upper())).json()
            assert sent["head"]["status"] == 0
        sessions = [client.post("/verify", json=request("verify")).json() for _ in range(2)]
        assert all(s["head"]["status"] == 0 for s in sessions)
        assert all(s["body"]["user_id"] == "store-review" for s in sessions)
        assert all(s["body"]["needs_birthday"] is False for s in sessions)
        assert sessions[0]["body"]["token"] != sessions[1]["body"]["token"]
        rejected = client.post("/verify", json=request("verify", code="000000")).json()
        assert rejected["head"]["status"] == 101
    assert db.query(EmailCode).count() == 0
    assert db.query(UserToken).count() == 2


@pytest.mark.parametrize("state", ["unconfigured", "disabled", "missing", "blocked"])
def test_review_login_requires_explicit_config_and_an_existing_active_account(db, monkeypatch, state):
    if state != "unconfigured":
        configure(monkeypatch, enabled=state != "disabled")
    if state != "missing":
        account(db, enabled=state != "blocked")
    assert find_valid_code(db, email=EMAIL, code=CODE, purpose=PURPOSE_LOGIN) is None
    assert db.query(UserToken).count() == 0


@pytest.mark.parametrize("encoded", ["", "482731", "pbkdf2_sha256$1$00$00", "pbkdf2_sha256$200000$zz$zz"])
def test_invalid_review_hash_fails_closed(db, monkeypatch, encoded):
    configure(monkeypatch, encoded=encoded)
    account(db)
    assert find_valid_code(db, email=EMAIL, code=CODE, purpose=PURPOSE_LOGIN) is None


def test_fixed_code_never_authenticates_another_email_or_deletes_the_account(db, monkeypatch):
    configure(monkeypatch)
    account(db)
    assert find_valid_code(db, email="other@example.com", code=CODE, purpose=PURPOSE_LOGIN) is None
    assert find_valid_code(db, email=EMAIL, code=CODE, purpose=PURPOSE_DEACTIVATE) is None
    delivered = []
    monkeypatch.setattr(
        "app.verification_codes.send_verification_code",
        lambda _settings, *, email, code: delivered.append((email, code)),
    )
    result = issue_email_code(db, get_settings(), email=EMAIL, purpose=PURPOSE_DEACTIVATE)
    assert result.ok
    assert delivered and len(delivered[0][1]) == 6
    deletion_code = find_valid_code(db, email=EMAIL, code=delivered[0][1], purpose=PURPOSE_DEACTIVATE)
    assert isinstance(deletion_code, EmailCode)


def test_ordinary_codes_keep_expiry_consumption_and_mail_cooldown(db, monkeypatch):
    configure(monkeypatch)
    account(db)
    email = "ordinary@example.com"
    delivered = []
    monkeypatch.setattr(
        "app.verification_codes.send_verification_code",
        lambda _settings, *, email, code: delivered.append((email, code)),
    )
    settings = get_settings()
    assert issue_email_code(db, settings, email=email, purpose=PURPOSE_LOGIN).ok
    assert issue_email_code(db, settings, email=email, purpose=PURPOSE_LOGIN).error_code == "CODE_RATE_LIMITED"
    row = find_valid_code(db, email=email, code=delivered[0][1], purpose=PURPOSE_LOGIN)
    assert isinstance(row, EmailCode)
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert find_valid_code(db, email=email, code=row.code, purpose=PURPOSE_LOGIN) is None
    row.expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
    row.used_at = datetime.now(timezone.utc)
    db.commit()
    assert find_valid_code(db, email=email, code=row.code, purpose=PURPOSE_LOGIN) is None
