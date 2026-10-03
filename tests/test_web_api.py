from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import get_settings
from app.credits import balance
from app.main import app
from app.models import (
    AppHandoffCode,
    CreatorAccessGrant,
    EmailCode,
    PublishedVideo,
    ReferralBinding,
    User,
    UserToken,
)
from app.protocol_video import creator_supported_gestures
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


def test_web_invite_binds_only_a_new_web_account_pending_android_activation(db) -> None:
    now = datetime.now(timezone.utc)
    _, inviter_token = _identity(db, "inviter")
    with TestClient(app) as client:
        invite = client.get(
            "/api/v1/referrals/me",
            headers={"Authorization": f"Bearer {inviter_token}"},
        )
        landing = client.get(invite.json()["url"])

    db.add(
        EmailCode(
            email="new-invite@example.com",
            purpose="login",
            code="654321",
            created_at=now,
            expires_at=now + timedelta(minutes=10),
        )
    )
    db.commit()

    with _web_client() as client:
        verified = client.post(
            "/api/v1/web/auth/email/verify",
            headers=_csrf(client),
            json={
                "email": "new-invite@example.com",
                "code": "654321",
                "invite_code": invite.json()["code"],
            },
        )
        handoff = client.post("/api/v1/web/auth/app-handoff", headers=_csrf(client), json={})

    with TestClient(app) as client:
        exchanged = client.post(
            "/api/v1/app-handoff/exchange",
            json={"code": handoff.json()["code"]},
        )
        replayed = client.post(
            "/api/v1/app-handoff/exchange",
            json={"code": handoff.json()["code"]},
        )

    assert invite.status_code == 200
    assert landing.status_code == 200
    assert landing.headers["cache-control"] == "no-store"
    assert "Get 5 Credits" in landing.text
    assert 'pixo://invite/' in landing.text
    assert verified.status_code == 200
    assert verified.json()["referral"]["invitee_registration_reward_credits"] == 5
    assert handoff.status_code == 200
    assert exchanged.status_code == 200
    assert replayed.status_code == 400
    invitee = db.query(User).filter_by(subject="new-invite@example.com").one()
    binding = db.get(ReferralBinding, invitee.user_id)
    assert binding is not None
    assert binding.inviter_user_id == "inviter"
    assert binding.status == "activated"
    assert balance(db, invitee.user_id) == 5
    assert balance(db, "inviter") == 5


def test_expired_app_handoff_cannot_be_exchanged(db) -> None:
    _, token = _identity(db, "expired-handoff")
    with _web_client(token) as client:
        handoff = client.post(
            "/api/v1/web/auth/app-handoff",
            headers=_csrf(client),
            json={},
        )

    row = db.query(AppHandoffCode).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    with TestClient(app) as client:
        exchanged = client.post(
            "/api/v1/app-handoff/exchange",
            json={"code": handoff.json()["code"]},
        )

    assert handoff.status_code == 200
    assert exchanged.status_code == 400
    assert exchanged.json()["detail"] == "handoff code is invalid or expired"


def test_web_google_invite_registration_gets_locked_reward(db, monkeypatch) -> None:
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "web-client-id")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.routers.web.verify_google_id_token",
        lambda **_kwargs: SimpleNamespace(subject="google-invitee", email="invitee@gmail.com"),
    )
    _, inviter_token = _identity(db, "google-inviter")
    with TestClient(app) as client:
        invite = client.get(
            "/api/v1/referrals/me",
            headers={"Authorization": f"Bearer {inviter_token}"},
        ).json()
    with _web_client() as client:
        registered = client.post(
            "/api/v1/web/auth/google",
            headers=_csrf(client),
            json={"credential": "verified-token", "invite_code": invite["code"]},
        )

    assert registered.status_code == 200
    assert registered.json()["referral"]["invitee_registration_reward_credits"] == 5
    invitee = db.query(User).filter_by(provider="google", subject="google-invitee").one()
    binding = db.get(ReferralBinding, invitee.user_id)
    assert binding is not None
    assert binding.config_version == 1
    assert balance(db, invitee.user_id) == 5


def test_creator_access_is_open_and_persists_across_clients(db) -> None:
    _, token = _identity(db, "policy-user")
    with _web_client(token) as client:
        web_access = client.get("/api/v1/creator/access")
    assert web_access.json()["granted"] is True
    assert web_access.json()["source"] == "open_access"

    with TestClient(app) as client:
        android_access = client.get(
            "/api/v1/creator/access",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert android_access.json()["granted"] is True
    assert android_access.json()["source"] == "open_access"
    assert db.query(CreatorAccessGrant).filter_by(user_id="policy-user").count() == 1
    assert db.get(CreatorAccessGrant, "policy-user") is not None


def test_web_session_can_read_shared_credits_referral_and_creator_capabilities(db) -> None:
    _, token = _identity(db, "web-account-data")
    db.add(CreatorAccessGrant(user_id="web-account-data", source="test"))
    db.commit()

    with _web_client(token) as client:
        credits = client.get("/api/v1/credits")
        referral = client.get("/api/v1/referrals/me")
        capabilities = client.get("/api/v1/creator/capabilities")

    assert credits.status_code == 200
    assert referral.status_code == 200
    assert referral.json()["code"]
    assert capabilities.status_code == 200
    body = capabilities.json()
    assert body["creator_contract_version"] == "2"
    assert body["credit_per_generated_second"] == 1
    assert body["referral_reward_credits"] == 5
    assert len(body["supported_interactions"]) == 37
    assert {item["type"] for item in body["supported_interactions"]} == set(
        creator_supported_gestures()
    )
    assert len(body["interaction_presets"]) == 55
    assert len({item["id"] for item in body["interaction_presets"]}) == 55
    assert sum(item["story_enabled"] for item in body["interaction_presets"]) == 49
    assert {
        "pinch_in", "pinch_out", "rotate_clockwise", "rotate_counterclockwise",
        "camera_motion.face_smile", "camera_motion.hand_open_palm",
        "camera_continuous.hand_finger_snap",
        "camera_continuous.hand_finger_gun_recoil",
        "tilt_forward", "tilt_backward",
    } <= {item["id"] for item in body["interaction_presets"]}
    assert next(item for item in body["supported_interactions"] if item["type"] == "camera_continuous") == {
        "type": "camera_continuous",
        "lifecycle": "sustained",
        "capability": "vision",
        "story_enabled": False,
    }


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
