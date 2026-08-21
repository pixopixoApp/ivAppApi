from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import User, UserToken
from app.pagination import CursorError, decode_cursor, encode_cursor


def _login(db, *, user_id: str = "u1", token: str = "token-1") -> None:
    now = datetime.now(timezone.utc)
    db.add(
        User(
            user_id=user_id,
            provider="email",
            subject=f"{user_id}@example.com",
            enabled=True,
        )
    )
    db.add(
        UserToken(
            token=token,
            user_id=user_id,
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
    )
    db.commit()


def test_cursor_is_opaque_signed_and_list_scoped() -> None:
    cursor = encode_cursor(kind="following:u1", values={"id": 7}, secret="secret")
    assert decode_cursor(cursor=cursor, kind="following:u1", secret="secret") == {"id": 7}
    with pytest.raises(CursorError):
        decode_cursor(cursor=cursor + "x", kind="following:u1", secret="secret")
    with pytest.raises(CursorError):
        decode_cursor(cursor=cursor, kind="followers:u1", secret="secret")


def test_existing_endpoint_accepts_bearer_without_head_token(db) -> None:
    _login(db)
    with TestClient(app) as client:
        response = client.post(
            "/profile",
            headers={"Authorization": "Bearer token-1"},
            json={"head": {"act": "profile", "ver": "1.2"}, "body": {}},
        )
    assert response.status_code == 200
    assert response.json()["head"]["status"] == 0
    assert response.json()["body"]["user_id"] == "u1"


def test_conflicting_bearer_and_legacy_token_is_rejected(db) -> None:
    _login(db)
    with TestClient(app) as client:
        response = client.post(
            "/profile",
            headers={"Authorization": "Bearer token-1"},
            json={
                "head": {"act": "profile", "ver": "1.2", "token": "other"},
                "body": {},
            },
        )
    assert response.status_code == 200
    assert response.json()["head"]["status"] == 101
