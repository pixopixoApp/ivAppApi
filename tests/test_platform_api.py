from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import get_settings
from app.main import app
from app.models import (
    CreatorAccessGrant,
    CreatorCreation,
    CreatorInvite,
    CreatorUpload,
    CreatorVersion,
    PublishedVideo,
    User,
    UserToken,
)
from app.routers.platform import _invite_hash


def _login(db, user_id: str = "creator") -> str:
    token = f"token-{user_id}"
    now = datetime.now(timezone.utc)
    db.add(User(user_id=user_id, provider="email", subject=f"{user_id}@example.com"))
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


def test_app_update_policy_is_database_managed(db) -> None:
    with TestClient(app) as client:
        written = client.put(
            "/internal/v1/app-versions/ios",
            headers={"X-Publish-Key": "test-publish-key"},
            json={
                "latest_version": "2.0",
                "latest_build": 20,
                "minimum_version": "1.5",
                "minimum_build": 15,
                "store_url": "https://example.com/app",
                "package_name": "com.pixopixo.pixoandroid",
                "size_bytes": 13002342,
                "release_notes": "New interactions",
                "enabled": True,
            },
        )
        checked = client.post(
            "/api/v1/app-updates/check",
            json={"platform": "ios", "version": "1.0", "build": 10},
        )
    assert written.status_code == 200
    assert checked.json()["update_available"] is True
    assert checked.json()["force_update"] is True
    assert checked.json()["package_name"] == "com.pixopixo.pixoandroid"
    assert checked.json()["size_bytes"] == 13002342


def test_single_use_invite_permanently_grants_creator_access(db) -> None:
    token = _login(db)
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        created = client.post(
            "/internal/v1/creator/invites",
            headers={"X-Publish-Key": "test-publish-key"},
            json={"count": 1},
        ).json()
        redeemed = client.post(
            "/api/v1/creator/invites/redeem",
            headers=headers,
            json={"code": created["codes"][0]},
        )
        access = client.get("/api/v1/creator/access", headers=headers)
    assert redeemed.status_code == 200
    assert access.json()["granted"] is True
    assert access.json()["source"] == "invite"


def test_creator_upload_uses_resumable_shared_local_cache(db, monkeypatch, tmp_path) -> None:
    token = _login(db, user_id="local-upload-creator")
    now = datetime.now(timezone.utc)
    db.add(CreatorAccessGrant(user_id="local-upload-creator", source="test", granted_at=now))
    db.commit()
    payload = b"local-first-video"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setenv("MEDIA_CACHE_ENABLED", "true")
    monkeypatch.setenv("MEDIA_CACHE_ROOT", str(tmp_path / "media-cache"))
    monkeypatch.setenv("CREATOR_LOCAL_UPLOAD_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.routers.media_storage.probe_video",
        lambda _path: SimpleNamespace(duration_ms=2_000),
    )
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        initialized = client.post(
            "/api/v1/creator/uploads/init",
            headers=headers,
            json={
                "filename": "clip.mp4",
                "content_type": "video/mp4",
                "size_bytes": len(payload),
                "sha256": digest,
                "supported_transports": ["local-resumable-v1"],
            },
        )
        assert initialized.status_code == 201
        policy = initialized.json()["upload"]
        assert initialized.json()["transport"] == "local-resumable-v1"
        first = client.patch(
            policy["url"],
            headers={**headers, "Upload-Offset": "0"},
            content=payload[:5],
        )
        assert first.status_code == 204
        resumed = client.head(policy["url"], headers=headers)
        assert resumed.headers["Upload-Offset"] == "5"
        second = client.patch(
            policy["url"],
            headers={**headers, "Upload-Offset": "5"},
            content=payload[5:],
        )
        assert second.status_code == 204
        finalized = client.post(
            f"/api/v1/creator/uploads/{initialized.json()['session_id']}/finalize",
            headers=headers,
            json={},
        )
        repeated = client.post(
            "/api/v1/creator/uploads/init",
            headers=headers,
            json={
                "filename": "clip-again.mp4",
                "content_type": "video/mp4",
                "size_bytes": len(payload),
                "sha256": digest,
                "supported_transports": ["local-resumable-v1"],
            },
        )
        assert repeated.status_code == 201
        repeated_policy = repeated.json()["upload"]
        repeated_upload = client.patch(
            repeated_policy["url"],
            headers={**headers, "Upload-Offset": "0"},
            content=payload,
        )
        assert repeated_upload.status_code == 204
        repeated_finalized = client.post(
            f"/api/v1/creator/uploads/{repeated.json()['session_id']}/finalize",
            headers=headers,
            json={},
        )

    assert finalized.status_code == 201
    assert repeated_finalized.status_code == 201
    assert finalized.json()["upload_id"] != repeated_finalized.json()["upload_id"]
    assert finalized.json()["upload_transport"] == "local-resumable-v1"
    assert finalized.json()["normalization_status"] == "pending"
    stored = tmp_path / "media-cache" / "objects" / digest[:2] / f"{digest}.cache"
    assert stored.read_bytes() == payload
    db.expire_all()
    rows = db.query(CreatorUpload).filter(CreatorUpload.source_sha256 == digest).all()
    assert len(rows) == 2
    assert len({row.storage_key for row in rows}) == 1
    get_settings.cache_clear()


def test_creator_upload_finalize_recovers_after_database_failure(
    db,
    monkeypatch,
    tmp_path,
) -> None:
    token = _login(db, user_id="recovering-upload-creator")
    db.add(
        CreatorAccessGrant(
            user_id="recovering-upload-creator",
            source="test",
            granted_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    payload = b"recoverable-local-video"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setenv("MEDIA_CACHE_ENABLED", "true")
    monkeypatch.setenv("MEDIA_CACHE_ROOT", str(tmp_path / "media-cache"))
    monkeypatch.setenv("CREATOR_LOCAL_UPLOAD_ENABLED", "true")
    get_settings.cache_clear()

    probed_paths = []

    def probe(path):
        probed_paths.append(path)
        return SimpleNamespace(duration_ms=2_000)

    monkeypatch.setattr("app.routers.media_storage.probe_video", probe)
    headers = {"Authorization": f"Bearer {token}"}

    original_commit = Session.commit
    failed_once = False

    def fail_first_upload_commit(session):
        nonlocal failed_once
        if not failed_once and any(isinstance(row, CreatorUpload) for row in session.new):
            failed_once = True
            raise RuntimeError("simulated database failure")
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", fail_first_upload_commit)

    with TestClient(app, raise_server_exceptions=False) as client:
        initialized = client.post(
            "/api/v1/creator/uploads/init",
            headers=headers,
            json={
                "filename": "recover.mp4",
                "content_type": "video/mp4",
                "size_bytes": len(payload),
                "sha256": digest,
                "supported_transports": ["local-resumable-v1"],
            },
        )
        policy = initialized.json()["upload"]
        uploaded = client.patch(
            policy["url"],
            headers={**headers, "Upload-Offset": "0"},
            content=payload,
        )
        first_finalize = client.post(
            f"/api/v1/creator/uploads/{initialized.json()['session_id']}/finalize",
            headers=headers,
            json={},
        )
        recovered = client.post(
            f"/api/v1/creator/uploads/{initialized.json()['session_id']}/finalize",
            headers=headers,
            json={},
        )

    assert initialized.status_code == 201
    assert uploaded.status_code == 204
    assert first_finalize.status_code == 500
    assert recovered.status_code == 201
    assert failed_once is True
    assert len(probed_paths) == 2
    assert probed_paths[0].suffix == ".part"
    assert probed_paths[1].suffix == ".cache"
    get_settings.cache_clear()


def test_single_character_invite_code_can_grant_creator_access(db) -> None:
    token = _login(db, user_id="single-character-invite-user")
    db.add(
        CreatorInvite(
            code_hash=_invite_hash("Q"),
            code_hint="Q",
            enabled=True,
        )
    )
    db.commit()
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        redeemed = client.post(
            "/api/v1/creator/invites/redeem",
            headers=headers,
            json={"code": "Q"},
        )

    assert redeemed.status_code == 200
    assert redeemed.json()["granted"] is True
    assert redeemed.json()["source"] == "invite"


def test_operations_can_list_revoke_invites_and_revoke_redeemed_access(db) -> None:
    token = _login(db, user_id="invite-ops-user")
    auth = {"Authorization": f"Bearer {token}"}
    internal = {"X-Publish-Key": "test-publish-key"}
    with TestClient(app) as client:
        created = client.post(
            "/internal/v1/creator/invites",
            headers=internal,
            json={"count": 2},
        ).json()
        before = client.get("/internal/v1/creator/invites", headers=internal)
        first_id = before.json()["items"][0]["id"]
        second_id = before.json()["items"][1]["id"]
        redeemed = client.post(
            "/api/v1/creator/invites/redeem",
            headers=auth,
            json={"code": created["codes"][0]},
        )
        after_redeem = client.get(
            "/internal/v1/creator/invites?status=redeemed",
            headers=internal,
        )
        redeemed_id = after_redeem.json()["items"][0]["id"]
        unused_id = second_id if redeemed_id == first_id else first_id
        revoked = client.post(
            "/internal/v1/creator/invites/revoke",
            headers=internal,
            json={"invite_ids": [unused_id, redeemed_id, 999999]},
        )
        access_revoked = client.post(
            "/internal/v1/creator/access/invite-ops-user/revoke",
            headers=internal,
            json={},
        )
        access = client.get("/api/v1/creator/access", headers=auth)

    assert before.json()["total"] == 2
    assert redeemed.status_code == 200
    assert after_redeem.json()["total"] == 1
    assert revoked.json()["revoked_ids"] == [unused_id]
    assert revoked.json()["skipped_redeemed_ids"] == [redeemed_id]
    assert revoked.json()["missing_ids"] == [999999]
    assert access_revoked.json()["granted"] is False
    assert access.json()["granted"] is False


def test_creator_session_persists_version_fifo_and_restores_active_state(db) -> None:
    token = _login(db)
    now = datetime.now(timezone.utc)
    db.add(CreatorAccessGrant(user_id="creator", source="test", granted_at=now))
    db.add(
        CreatorUpload(
            id="up_fifo",
            user_id="creator",
            storage_key="creator_uploads/creator/up_fifo.mp4",
            original_filename="video.mp4",
            size_bytes=12,
            duration_ms=5000,
            created_at=now,
        )
    )
    db.commit()
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/creator/creations",
            headers=headers,
            json={"upload_id": "up_fifo", "brief": "Add a tap", "request_id": "request-1"},
        )
        creation_id = created.json()["creation_id"]
        second = client.post(
            f"/api/v1/creator/creations/{creation_id}/versions",
            headers=headers,
            json={"brief": "Move it earlier", "request_id": "request-2"},
        )
        third = client.post(
            f"/api/v1/creator/creations/{creation_id}/versions",
            headers=headers,
            json={"brief": "Make the hint louder", "request_id": "request-3"},
        )
        restored = client.get("/api/v1/creator/creations/active", headers=headers)

    assert created.status_code == 202
    assert second.status_code == 202
    assert third.status_code == 202
    assert [item["number"] for item in restored.json()["versions"]] == [1, 2, 3]
    assert [item["request"] for item in restored.json()["versions"]] == [
        "Add a tap",
        "Move it earlier",
        "Make the hint louder",
    ]
    assert restored.json()["active_version_id"] == third.json()["active_version_id"]
    rows = (
        db.query(CreatorVersion)
        .filter(CreatorVersion.creation_id == creation_id)
        .order_by(CreatorVersion.number.asc())
        .all()
    )
    assert [row.request_id for row in rows] == ["request-1", "request-2", "request-3"]


def test_ready_creation_requires_confirmation_then_persists_final_runtime(db, monkeypatch) -> None:
    token = _login(db)
    now = datetime.now(timezone.utc)
    db.add(CreatorAccessGrant(user_id="creator", source="test", granted_at=now))
    media_root = Path("/tmp/ivapp-pytest-media")
    private = media_root / "private" / "creator_uploads" / "creator"
    private.mkdir(parents=True, exist_ok=True)
    source = private / "up_test.mp4"
    source.write_bytes(b"source-video")
    playable_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    cache_root = media_root / "media-cache"
    playable = cache_root / "objects" / playable_sha[:2] / f"{playable_sha}.cache"
    playable.parent.mkdir(parents=True, exist_ok=True)
    playable.write_bytes(source.read_bytes())
    monkeypatch.setenv("MEDIA_CACHE_ROOT", str(cache_root))
    monkeypatch.setenv("PUBLIC_GAME_BASE_URL", "https://demo.pixopixo.cn/game/")
    get_settings.cache_clear()
    db.add(
        CreatorUpload(
            id="up_test",
            user_id="creator",
            storage_key="creator_uploads/creator/up_test.mp4",
            original_filename="video.mp4",
            size_bytes=12,
            duration_ms=5000,
            normalization_status="ready",
            playable_sha256=playable_sha,
            playable_size_bytes=source.stat().st_size,
            created_at=now,
        )
    )
    timeline = {"interactions": [{"gesture": "tap", "gate_at_ms": 1000}]}
    db.add(
        CreatorCreation(
            id="cr_test",
            user_id="creator",
            upload_id="up_test",
            status="ready",
            progress_stage="compile_preview",
            progress_percent=100,
            source_timeline=timeline,
            runtime_spec={"preview": True},
            runtime_spec_version="1.1",
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        rejected = client.post(
            "/api/v1/creator/creations/cr_test/publish",
            headers=headers,
            json={"confirm": False, "title": "Test creation", "description": ""},
        )
        published = client.post(
            "/api/v1/creator/creations/cr_test/publish",
            headers=headers,
            json={"confirm": True, "title": "Test creation", "description": "Playable"},
        )
    assert rejected.status_code == 400
    assert published.status_code == 200
    assert published.json()["share_url"] == (
        "https://demo.pixopixo.cn/game/?experience=cr_test"
    )
    db.expire_all()
    row = db.get(PublishedVideo, "cr_test")
    assert row.title == "Test creation"
    assert row.description == "Playable"
    assert row.runtime_spec_version == "1.1"
    interaction = row.runtime_spec["video"][0]["interactions"][0]
    assert interaction["pause_video"] is True
    assert interaction["detection"]["response_window_ms"] == 0

    with TestClient(app) as client:
        deleted = client.delete(
            "/api/v1/creator/published/cr_test",
            headers=headers,
        )
        active_after_delete = client.get(
            "/api/v1/creator/creations/active",
            headers=headers,
        )
        missing_share = client.get("/api/v1/share/cr_test")
        restored = client.post(
            "/api/v1/creator/published/cr_test/restore",
            headers=headers,
        )
        share = client.get("/api/v1/share/cr_test", follow_redirects=False)
    assert deleted.json() == {"video_id": "cr_test", "deleted": True}
    assert active_after_delete.json() is None
    assert missing_share.status_code == 404
    assert restored.json() == {"video_id": "cr_test", "deleted": False}
    assert share.status_code == 302
    assert share.headers["location"] == (
        "https://demo.pixopixo.cn/game/?experience=cr_test"
    )
