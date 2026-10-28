from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.models import (
    ContentReport,
    CreatorAccessGrant,
    EmailCode,
    Follow,
    PublishedVideo,
    User,
    UserBlock,
    UserToken,
)
from app.protocol_video import RUNTIME_SPEC_VERSION, compile_runtime_spec


def _login(db, user_id: str, *, provider: str = "email") -> str:
    token = f"token-{user_id}"
    now = datetime.now(timezone.utc)
    db.add(
        User(
            user_id=user_id,
            provider=provider,
            subject=(f"{user_id}@example.com" if provider == "email" else f"google-{user_id}"),
            nickname=user_id.title(),
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
    return token


def _published(db, video_id: str, author_id: str) -> None:
    timeline = {"interactions": [{"gesture": "tap", "gate_at_ms": 1000}]}
    runtime = compile_runtime_spec(
        item_id=video_id,
        content_mode="single",
        source=timeline,
        video_url=f"/media/{video_id}.mp4",
    )
    now = datetime.now(timezone.utc)
    db.add(
        PublishedVideo(
            id=video_id,
            video_url=f"/media/{video_id}.mp4",
            timeline=timeline,
            runtime_spec=runtime,
            runtime_spec_version=RUNTIME_SPEC_VERSION,
            version="1",
            title="Safety test",
            user_id=author_id,
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()


def test_report_block_feed_filter_and_moderation_decision(db) -> None:
    reporter_token = _login(db, "reporter")
    _login(db, "author")
    _published(db, "unsafe-video", "author")
    db.add(Follow(follower_user_id="reporter", followee_user_id="author"))
    db.commit()
    headers = {"Authorization": f"Bearer {reporter_token}"}

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/safety/reports",
            headers=headers,
            json={
                "target_type": "video",
                "target_id": "unsafe-video",
                "reason": "dangerous_acts",
                "details": "",
            },
        )
        repeated = client.post(
            "/api/v1/safety/reports",
            headers=headers,
            json={
                "target_type": "video",
                "target_id": "unsafe-video",
                "reason": "spam",
                "details": "duplicate updates the pending report",
            },
        )
        blocked = client.post("/api/v1/safety/blocks/author", headers=headers, json={})
        feed = client.post(
            "/video",
            headers=headers,
            json={"head": {"act": "video", "ver": "1.2"}, "body": {"limit": 10}},
        )
        listed = client.get(
            "/internal/v1/moderation/reports?status=pending",
            headers={"X-Publish-Key": "test-publish-key"},
        )
        report_id = created.json()["report_id"]
        invalid_decision = client.post(
            f"/internal/v1/moderation/reports/{report_id}/decision",
            headers={"X-Publish-Key": "test-publish-key"},
            json={
                "status": "dismissed",
                "action": "remove_content",
                "resolution": "Contradictory decision",
                "reviewed_by": "admin",
            },
        )
        decided = client.post(
            f"/internal/v1/moderation/reports/{report_id}/decision",
            headers={"X-Publish-Key": "test-publish-key"},
            json={
                "status": "actioned",
                "action": "remove_content",
                "resolution": "Removed after review",
                "reviewed_by": "admin",
            },
        )

    assert created.status_code == 200
    assert repeated.json()["report_id"] == created.json()["report_id"]
    assert blocked.json() == {"user_id": "author", "blocked": True}
    assert feed.json()["head"]["status"] == 100
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["reason"] == "spam"
    assert invalid_decision.status_code == 400
    assert decided.json()["status"] == "actioned"
    assert db.query(Follow).count() == 0
    assert db.query(UserBlock).count() == 1
    db.expire_all()
    assert db.get(PublishedVideo, "unsafe-video").deleted_at is not None


def test_google_account_deletion_is_immediate_and_removes_relations(db, monkeypatch) -> None:
    token = _login(db, "delete-me", provider="google")
    _login(db, "peer")
    _published(db, "owned-video", "delete-me")
    db.add(Follow(follower_user_id="delete-me", followee_user_id="peer"))
    db.add(UserBlock(blocker_user_id="delete-me", blocked_user_id="peer"))
    db.add(CreatorAccessGrant(user_id="delete-me", source="invite"))
    db.add(
        ContentReport(
            id="rpt-delete-me",
            reporter_user_id="delete-me",
            target_type="user",
            target_id="peer",
            target_user_id="peer",
            reason="spam",
        )
    )
    db.commit()
    monkeypatch.setattr("app.account_deletion._purge_remote_creations", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.account_deletion.get_impression_store",
        lambda: type("MemoryStore", (), {"clear_user": lambda self, user_id: None})(),
    )

    with TestClient(app) as client:
        response = client.request(
            "DELETE",
            "/api/v1/account",
            headers={"Authorization": f"Bearer {token}"},
            json={"confirm": True},
        )

    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert db.get(User, "delete-me") is None
    assert db.get(PublishedVideo, "owned-video") is None
    assert db.query(Follow).count() == 0
    assert db.query(UserBlock).count() == 0
    assert db.query(ContentReport).count() == 0
    assert db.get(CreatorAccessGrant, "delete-me") is None
    assert db.get(UserToken, token) is None


def test_email_account_deletion_requires_a_valid_deactivation_code(db, monkeypatch) -> None:
    token = _login(db, "email-delete")
    now = datetime.now(timezone.utc)
    db.add(
        EmailCode(
            email="email-delete@example.com",
            purpose="deactivate",
            code="123456",
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        )
    )
    db.commit()
    monkeypatch.setattr("app.account_deletion._purge_remote_creations", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.account_deletion.get_impression_store",
        lambda: type("MemoryStore", (), {"clear_user": lambda self, user_id: None})(),
    )
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        rejected = client.request(
            "DELETE",
            "/api/v1/account",
            headers=headers,
            json={"confirm": True, "verification_code": "000000"},
        )
        accepted = client.request(
            "DELETE",
            "/api/v1/account",
            headers=headers,
            json={"confirm": True, "verification_code": "123456"},
        )
    assert rejected.status_code == 400
    assert accepted.status_code == 200
    assert db.get(User, "email-delete") is None


def test_ai_reports_are_private_and_reach_moderation(db, monkeypatch) -> None:
    from types import SimpleNamespace

    from app.models import CreatorCreation, CreatorSourceGeneration, CreatorVersion
    monkeypatch.setattr("app.routers.safety.get_settings", lambda: SimpleNamespace(
        public_share_base_url="https://api.example.com",
    ))

    token = _login(db, "creator")
    other_token = _login(db, "other")
    _published(db, "own-video", "creator")
    now = datetime.now(timezone.utc)
    db.add(CreatorCreation(id="creation-1", user_id="creator", status="ready"))
    db.add(CreatorSourceGeneration(
        id="generation-1", creation_id="creation-1", user_id="creator", attempt=1,
        request_id="request-1", original_prompt="A prompt", quota_date="2026-10-28",
        status="ready", expires_at=now + timedelta(days=1), ivadmin_job_id="job-1", upload_id="upload-1",
    ))
    db.add(CreatorVersion(
        id="version-1", creation_id="creation-1", user_id="creator", number=1,
        request_id="version-request-1", status="ready", brief="A version",
        runtime_spec={"video": []},
    ))
    db.commit()
    headers = {"Authorization": f"Bearer {token}"}
    other = {"Authorization": f"Bearer {other_token}"}
    moderation = {"X-Publish-Key": "test-publish-key"}
    with TestClient(app) as client:
        def report(target_type, target_id, auth=headers):
            return client.post("/api/v1/safety/reports", headers=auth, json={
                "target_type": target_type, "target_id": target_id,
                "reason": "dangerous_acts", "details": "unsafe result",
            })

        own_video = report("video", "own-video")
        source = report("ai_source", "generation-1")
        version = report("ai_version", "version-1")
        assert all(response.status_code == 200 for response in (own_video, source, version))
        assert report("ai_source", "generation-1", other).status_code == 404
        assert report("ai_version", "version-1", other).status_code == 404
        assert report("ai_source", "missing").status_code == 404
        assert report("user", "creator").status_code == 400
        listed = client.get("/internal/v1/moderation/reports?target_type=ai_source", headers=moderation)
        assert listed.status_code == 200
        item = listed.json()["items"][0]
        assert item["target_context"]["job_id"] == "job-1"
        assert item["target_context"]["prompt"] == "A prompt"
        assert item["target_context"]["preview_url"] == "https://api.example.com/api/v1/creator/previews/upload-1"
        assert item["details"] == "unsafe result"
        decided = client.post(f"/internal/v1/moderation/reports/{item['id']}/decision",
                              headers=moderation, json={"status": "actioned", "action": "none"})
        assert decided.status_code == 200
        assert decided.json()["status"] == "actioned"
