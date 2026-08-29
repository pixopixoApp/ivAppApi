from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.models import CreatorAccessGrant, EmailCode, PublishedVideo, User, UserToken
from app.web_session import WEB_CSRF_COOKIE, WEB_SESSION_COOKIE


def _identity(db, user_id: str = "web-user") -> tuple[User, str]:
    now = datetime.now(timezone.utc)
    user = User(
        user_id=user_id,
        provider="email",
        subject=f"{user_id}@example.com",
        nickname="Web creator",
    )
    token = f"token-{user_id}"
    db.add(user)
    db.add(
        UserToken(
            token=token,
            user_id=user_id,
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
    )
    db.commit()
    return user, token


def _web_client(token: str | None = None) -> TestClient:
    client = TestClient(app)
    if token:
        client.cookies.set(WEB_SESSION_COOKIE, token)
    client.get("/api/v1/web/config")
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-Pixo-CSRF": client.cookies.get(WEB_CSRF_COOKIE) or ""}


def test_web_session_profile_and_csrf_are_same_origin(db) -> None:
    _, token = _identity(db)
    with _web_client(token) as client:
        session = client.get("/api/v1/web/auth/session")
        rejected = client.patch("/api/v1/web/me", json={"nickname": "No CSRF"})
        updated = client.patch(
            "/api/v1/web/me",
            headers=_csrf(client),
            json={"nickname": "Playable person", "bio": "Makes moments move"},
        )

    assert session.status_code == 200
    assert session.json()["authenticated"] is True
    assert rejected.status_code == 403
    assert updated.status_code == 200
    assert updated.json()["nickname"] == "Playable person"
    assert updated.json()["bio"] == "Makes moments move"


def test_web_email_send_code_route_uses_existing_mail_delivery(db, monkeypatch) -> None:
    delivered: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.verification_codes.send_verification_code",
        lambda _settings, *, email, code: delivered.append((email, code)),
    )

    with _web_client() as client:
        response = client.post(
            "/api/v1/web/auth/email/send-code",
            headers=_csrf(client),
            json={"email": "creator@example.com"},
        )

    assert response.status_code == 200
    assert response.json()["sent"] is True
    assert delivered and delivered[0][0] == "creator@example.com"
    assert delivered[0][1].isdigit() and len(delivered[0][1]) == 6


def test_web_email_login_keeps_existing_android_session(db) -> None:
    now = datetime.now(timezone.utc)
    _, android_token = _identity(db, "same-account")
    db.add(
        EmailCode(
            email="same-account@example.com",
            purpose="login",
            code="123456",
            created_at=now,
            expires_at=now + timedelta(minutes=10),
        )
    )
    db.commit()

    with _web_client() as client:
        response = client.post(
            "/api/v1/web/auth/email/verify",
            headers=_csrf(client),
            json={"email": "same-account@example.com", "code": "123456"},
        )
        web_token = client.cookies.get(WEB_SESSION_COOKIE)

    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert web_token and web_token != android_token
    db.expire_all()
    tokens = db.query(UserToken).filter(UserToken.user_id == "same-account").all()
    assert {row.token for row in tokens} == {android_token, web_token}


def test_creator_access_modes_create_permanent_cross_client_grants(db, monkeypatch) -> None:
    _, token = _identity(db, "policy-user")
    monkeypatch.setenv("CREATOR_ACCESS_MODE", "web_open")
    get_settings.cache_clear()
    with _web_client(token) as client:
        web_access = client.get("/api/v1/creator/access")
    assert web_access.json()["granted"] is True
    assert web_access.json()["source"] == "web_open"

    monkeypatch.setenv("CREATOR_ACCESS_MODE", "invite")
    get_settings.cache_clear()
    with TestClient(app) as client:
        android_access = client.get(
            "/api/v1/creator/access",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert android_access.json()["granted"] is True
    assert db.get(CreatorAccessGrant, "policy-user") is not None


def test_browser_resumable_upload_computes_checksum_on_finalize(db, monkeypatch, tmp_path) -> None:
    _, token = _identity(db, "upload-user")
    db.add(CreatorAccessGrant(user_id="upload-user", source="test"))
    db.commit()
    payload = b"browser-video-without-client-hash"
    monkeypatch.setenv("MEDIA_CACHE_ENABLED", "true")
    monkeypatch.setenv("MEDIA_CACHE_ROOT", str(tmp_path / "media-cache"))
    monkeypatch.setenv("CREATOR_LOCAL_UPLOAD_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.routers.media_storage.probe_video",
        lambda _path: SimpleNamespace(duration_ms=2_500),
    )

    with _web_client(token) as client:
        headers = _csrf(client)
        initialized = client.post(
            "/api/v1/creator/uploads/init",
            headers=headers,
            json={
                "filename": "clip.mp4",
                "content_type": "video/mp4",
                "size_bytes": len(payload),
                "supported_transports": ["local-resumable-v1"],
            },
        )
        session_id = initialized.json()["session_id"]
        uploaded = client.patch(
            f"/api/v1/creator/uploads/{session_id}/source",
            headers={**headers, "Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"},
            content=payload,
        )
        finalized = client.post(
            f"/api/v1/creator/uploads/{session_id}/finalize",
            headers=headers,
            json={"manifest_hash": ""},
        )

    assert initialized.status_code == 201
    assert uploaded.status_code == 204
    assert finalized.status_code == 201
    assert finalized.json()["upload_transport"] == "local-resumable-v1"
    assert finalized.json()["duration_ms"] == 2500


def test_web_publications_include_review_cdn_and_deleted_states(db) -> None:
    _, token = _identity(db, "library-user")
    now = datetime.now(timezone.utc)
    db.add_all(
        [
            PublishedVideo(
                id="pending-work",
                title="Waiting room",
                description="Pending moderation",
                video_url="/media/pending.mp4",
                timeline={},
                runtime_spec={},
                runtime_spec_version="1.1",
                version="1",
                user_id="library-user",
                content_type="runtime",
                content_mode="single",
                content_source="ugc",
                review_status="pending",
                cdn_ready=False,
                created_at=now,
                updated_at=now,
            ),
            PublishedVideo(
                id="deleted-work",
                title="Archived idea",
                description="",
                video_url="/media/deleted.mp4",
                timeline={},
                runtime_spec={},
                runtime_spec_version="1.1",
                version="1",
                user_id="library-user",
                content_type="runtime",
                content_mode="single",
                content_source="ugc",
                review_status="approved",
                cdn_ready=True,
                is_deleted=1,
                deleted_at=now,
                created_at=now - timedelta(minutes=1),
                updated_at=now,
            ),
        ]
    )
    db.commit()

    with _web_client(token) as client:
        page = client.get("/api/v1/web/me/publications")

    assert page.status_code == 200
    assert [item["status"] for item in page.json()["items"]] == [
        "pending_review",
        "deleted",
    ]
