from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.models import User

PUBLISH_HEADERS = {"X-Publish-Key": "test-publish-key"}


def _add_user(db, user_id: str, *, source: str = "admin", enabled: bool = True) -> None:
    db.add(
        User(
            user_id=user_id,
            provider="email",
            subject=f"{user_id}@example.com",
            nickname=user_id.title(),
            source=source,
            enabled=enabled,
        )
    )


def test_random_user_picks_only_enabled_admin_and_respects_excludes(db) -> None:
    _add_user(db, "admin-a", source="admin", enabled=True)
    _add_user(db, "admin-b", source="admin", enabled=True)
    _add_user(db, "disabled", source="admin", enabled=False)
    _add_user(db, "app-user", source="app", enabled=True)
    db.commit()

    with TestClient(app) as client:
        # No excludes: only admin-a / admin-b are eligible.
        resp = client.get("/internal/v1/users/random", headers=PUBLISH_HEADERS)
        assert resp.status_code == 200
        picked = resp.json()["user_id"]
        assert picked in {"admin-a", "admin-b"}

        # Exclude both eligible admin accounts -> no available account.
        resp_none = client.get(
            "/internal/v1/users/random",
            headers=PUBLISH_HEADERS,
            params={"exclude_user_ids": ["admin-a", "admin-b"]},
        )
        assert resp_none.status_code == 404

        # Excluding one leaves the other.
        resp_other = client.get(
            "/internal/v1/users/random",
            headers=PUBLISH_HEADERS,
            params={"exclude_user_ids": ["admin-a"]},
        )
        assert resp_other.status_code == 200
        assert resp_other.json()["user_id"] == "admin-b"


def test_random_user_requires_admin_source(db) -> None:
    _add_user(db, "admin-a", source="admin", enabled=True)
    db.commit()
    with TestClient(app) as client:
        resp = client.get(
            "/internal/v1/users/random",
            headers=PUBLISH_HEADERS,
            params={"source": "app"},
        )
        assert resp.status_code == 400
