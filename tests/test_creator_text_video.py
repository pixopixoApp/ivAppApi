from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from app.config import get_settings
from app.credits import settle_reference
from app.main import app
from app.models import (
    CreatorAccessGrant,
    CreatorCreation,
    CreatorSourceGeneration,
    CreatorUpload,
    CreatorVersion,
    CreditLedgerEntry,
    MediaObject,
    User,
    UserToken,
)
from app.worker import process_next_expired_source, process_source_generation


def _creator(db, user_id: str = "text-video-user") -> tuple[str, dict[str, str]]:
    now = datetime.now(timezone.utc)
    token = f"token-{user_id}"
    db.add_all(
        [
            User(user_id=user_id, provider="email", subject=f"{user_id}@example.com"),
            UserToken(
                token=token,
                user_id=user_id,
                created_at=now,
                expires_at=now + timedelta(days=1),
            ),
            CreatorAccessGrant(user_id=user_id, source="test", granted_at=now),
        ]
    )
    db.commit()
    return user_id, {"Authorization": f"Bearer {token}"}


def _enable(monkeypatch) -> None:
    monkeypatch.setenv("CREATOR_TEXT_TO_VIDEO_ENABLED", "true")
    monkeypatch.setenv("CREATOR_VIDEO_DAILY_QUOTA", "3")
    monkeypatch.setenv("CREATOR_INTERNAL_KEY", "creator-text-video-key")
    monkeypatch.setenv("IVADMIN_BASE_URL", "http://ivadmin.test:8000")
    get_settings.cache_clear()


def test_prompt_creation_is_disabled_by_default(db, monkeypatch) -> None:
    monkeypatch.delenv("CREATOR_TEXT_TO_VIDEO_ENABLED", raising=False)
    get_settings.cache_clear()
    _user_id, headers = _creator(db, "soft-rollback-user")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/creator/creations",
            headers=headers,
            json={
                "source_mode": "prompt",
                "prompt": "A scene that should not start a provider job",
                "request_id": "soft-rollback-prompt",
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "text-to-video creation is not available"
    assert db.query(CreatorCreation).count() == 0


def test_prompt_creation_waits_for_source_confirmation(db, monkeypatch) -> None:
    _enable(monkeypatch)
    user_id, headers = _creator(db)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/creator/creations",
            headers=headers,
            json={
                "source_mode": "prompt",
                "prompt": "A paper moon unfolds when the viewer raises a hand",
                "request_id": "prompt-create-1",
            },
        )
        repeated = client.post(
            "/api/v1/creator/creations",
            headers=headers,
            json={
                "source_mode": "prompt",
                "prompt": "A paper moon unfolds when the viewer raises a hand",
                "request_id": "prompt-create-1",
            },
        )
        cancelled = client.post(
            f"/api/v1/creator/creations/{created.json()['creation_id']}/cancel",
            headers=headers,
        )
        credits = client.get("/api/v1/credits", headers=headers)

    assert created.status_code == 202
    assert created.json()["source_mode"] == "prompt"
    assert created.json()["upload_id"] is None
    assert created.json()["versions"] == []
    assert created.json()["generation_quota"]["reserved"] == 1
    assert repeated.json()["creation_id"] == created.json()["creation_id"]
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["generation_quota"]["reserved"] == 0
    assert credits.status_code == 200
    assert credits.json()["balance"] == 5
    generation = db.get(
        CreatorSourceGeneration,
        created.json()["source_generation_id"],
    )
    assert generation is not None
    assert generation.user_id == user_id
    assert generation.quota_state == "released"


def test_source_worker_charges_only_after_ready_preview_and_accept_starts_analysis(
    db,
    monkeypatch,
) -> None:
    _enable(monkeypatch)
    user_id, headers = _creator(db, "source-worker-user")
    now = datetime.now(timezone.utc)
    creation = CreatorCreation(
        id="cr_source_worker",
        user_id=user_id,
        upload_id=None,
        source_mode="prompt",
        source_prompt="A glowing ribbon follows a hand wave",
        source_generation_id="csg_source_worker",
        brief="",
        status="queued",
        progress_stage="planning_prompt",
        progress_percent=0,
        created_at=now,
        updated_at=now,
    )
    generation = CreatorSourceGeneration(
        id="csg_source_worker",
        creation_id=creation.id,
        user_id=user_id,
        attempt=1,
        request_id="source-worker-request",
        original_prompt=creation.source_prompt,
        status="running",
        progress_stage="planning_prompt",
        progress_percent=1,
        quota_date=now.astimezone().date().isoformat(),
        quota_state="reserved",
        next_poll_at=now,
        expires_at=now + timedelta(days=30),
        created_at=now,
        updated_at=now,
    )
    db.add_all([creation, generation])
    db.commit()

    requests: list[dict] = []
    responses = [
        {
            "job_id": "cvgj-source-worker",
            "creation_id": creation.id,
            "generation_id": generation.id,
            "status": "running",
            "progress_stage": "generating_video",
            "progress_percent": 15,
            "provider_task_accepted": True,
            "prompt_summary": "Glowing ribbon hand-wave scene",
            "generation_prompt": "Vertical cinematic scene with a glowing ribbon.",
            "interaction_brief": "Trigger the reveal when the viewer waves.",
            "preset": {
                "model": "doubao-seedance-2-0-260128",
                "ratio": "9:16",
                "duration_seconds": 3,
                "resolution": "720p",
                "generate_audio": True,
                "watermark": False,
            },
        },
        {
            "job_id": "cvgj-source-worker",
            "creation_id": creation.id,
            "generation_id": generation.id,
            "status": "ready",
            "progress_stage": "ready",
            "progress_percent": 100,
            "provider_task_accepted": True,
            "prompt_summary": "Glowing ribbon hand-wave scene",
            "generation_prompt": "Vertical cinematic scene with a glowing ribbon.",
            "interaction_brief": "Trigger the reveal when the viewer waves.",
            "preset": {"model": "doubao-seedance-2-0-260128", "ratio": "9:16"},
            "source_local_uri": "local-cache://sha256/" + "a" * 64,
            "source_storage_key": "local-cache://sha256/" + "a" * 64,
            "source_sha256": "a" * 64,
            "source_size_bytes": 2048,
            "source_duration_ms": 3_000,
        },
    ]

    def fake_request(method, url, **kwargs):
        requests.append({"method": method, "url": url, **kwargs})
        return httpx.Response(
            200,
            json=responses.pop(0),
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("app.worker.httpx.request", fake_request)
    settings = get_settings()
    process_source_generation(db, settings, generation)
    db.refresh(generation)
    assert generation.quota_state == "reserved"
    assert generation.provider_task_accepted is True

    process_source_generation(db, settings, generation)
    db.refresh(generation)
    upload = db.get(CreatorUpload, generation.upload_id)
    assert upload is not None
    assert upload.origin == "ai_generated"
    assert upload.normalization_status == "pending"

    upload.normalization_status = "ready"
    upload.playable_sha256 = "b" * 64
    upload.playable_size_bytes = 1024
    playable = MediaObject(
        id="mo_source_worker_playable",
        purpose="creator_normalized",
        origin="ai_generated",
        visibility="private",
        state="ready",
        staging_key="",
        object_key=(
            "ivapp-media/v1/private/creator-sources/worker/playable.mp4"
        ),
        original_filename="playable.mp4",
        content_type="video/mp4",
        size_bytes=1024,
        sha256="b" * 64,
        etag="test-etag",
    )
    upload.playable_media_object_id = playable.id
    warmed_keys: list[str] = []
    monkeypatch.setattr(
        "app.worker.enqueue_private_media_prefetch",
        lambda _db, _settings, keys: warmed_keys.extend(keys) or [],
    )
    db.add_all([upload, playable])
    db.commit()
    process_source_generation(db, settings, generation)
    db.refresh(generation)
    db.refresh(creation)
    assert generation.status == "ready"
    assert creation.status == "source_ready"
    assert warmed_keys == [playable.object_key]

    with TestClient(app) as client:
        reviewed = client.get(
            f"/api/v1/creator/creations/{creation.id}",
            headers=headers,
        )
        accepted = client.post(
            f"/api/v1/creator/creations/{creation.id}/source/accept",
            headers=headers,
            json={
                "generation_id": generation.id,
                "request_id": "accept-source-worker",
            },
        )
        accepted_again = client.post(
            f"/api/v1/creator/creations/{creation.id}/source/accept",
            headers=headers,
            json={
                "generation_id": generation.id,
                "request_id": "accept-source-worker",
            },
        )

    assert reviewed.json()["source_preview_url"].endswith(
        f"/creator/previews/{upload.id}"
    )
    assert reviewed.json()["source_generation"]["prompt_summary"].startswith("Glowing")
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "queued"
    assert len(accepted.json()["versions"]) == 1
    assert accepted_again.status_code == 202
    assert db.query(CreatorVersion).filter_by(creation_id=creation.id).count() == 1
    assert requests[0]["json"] == {
        "request_id": "source-worker-request",
        "creation_id": creation.id,
        "generation_id": generation.id,
        "prompt": "A glowing ribbon follows a hand wave",
        "duration_seconds": 3,
    }


def test_credits_allow_fourth_provider_attempt_without_hidden_daily_cap(db, monkeypatch) -> None:
    _enable(monkeypatch)
    user_id, headers = _creator(db, "quota-user")
    # A funded account is no longer subject to the old daily-three limit.
    db.add(
        CreditLedgerEntry(
            id="test-quota-credit-grant",
            user_id=user_id,
            kind="test_grant",
            amount=20,
            reference_id="test",
            note="test-only credit grant",
        )
    )
    db.commit()

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/creator/creations",
            headers=headers,
            json={"source_mode": "prompt", "prompt": "Idea one", "request_id": "quota-1"},
        )
        creation_id = first.json()["creation_id"]
        for request_id, prompt in (("quota-2", "Idea two"), ("quota-3", "Idea three")):
            current = db.get(
                CreatorSourceGeneration,
                db.get(CreatorCreation, creation_id).source_generation_id,
            )
            current.status = "ready"
            current.quota_state = "charged"
            db.add(current)
            db.commit()
            regenerated = client.post(
                f"/api/v1/creator/creations/{creation_id}/source/regenerate",
                headers=headers,
                json={"prompt": prompt, "request_id": request_id},
            )
            assert regenerated.status_code == 202

        current = db.get(
            CreatorSourceGeneration,
            db.get(CreatorCreation, creation_id).source_generation_id,
        )
        current.status = "ready"
        current.quota_state = "charged"
        db.add(current)
        db.commit()
        blocked = client.post(
            f"/api/v1/creator/creations/{creation_id}/source/regenerate",
            headers=headers,
            json={"prompt": "Idea four", "request_id": "quota-4"},
        )

    assert blocked.status_code == 202
    assert blocked.json()["generation_quota"]["unlimited"] is True


def test_paid_source_regeneration_keeps_previous_ready_preview(db, monkeypatch) -> None:
    _enable(monkeypatch)
    user_id, headers = _creator(db, "regenerate-preview-user")
    now = datetime.now(timezone.utc)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/creator/creations",
            headers=headers,
            json={"source_mode": "prompt", "prompt": "First source", "request_id": "source-first"},
        )
        assert created.status_code == 202
        creation = db.get(CreatorCreation, created.json()["creation_id"])
        generation = db.get(CreatorSourceGeneration, creation.source_generation_id)
        old_upload = CreatorUpload(
            id="up_previous_ready_source", user_id=user_id,
            storage_key="local-cache://sha256/" + "a" * 64,
            source_local_uri="local-cache://sha256/" + "a" * 64,
            source_sha256="a" * 64, upload_transport="ai-generated",
            origin="ai_generated", source_generation_id=generation.id,
            normalization_status="ready", normalization_profile="mobile-v1",
            playable_local_uri="local-cache://sha256/" + "a" * 64,
            playable_sha256="a" * 64, playable_size_bytes=2048,
            original_filename="old.mp4", size_bytes=2048, duration_ms=5000,
            created_at=now,
        )
        db.add(old_upload)
        generation.upload_id = old_upload.id
        generation.status = "ready"
        generation.quota_state = "charged"
        generation.accepted_at = now
        creation.upload_id = old_upload.id
        creation.status = "source_ready"
        db.add(CreditLedgerEntry(
            id="regenerate-preview-credit", user_id=user_id, kind="test_grant",
            amount=5, reference_id="test", note="test-only credit grant", created_at=now,
        ))
        settle_reference(db, user_id=user_id, reference_id=f"source:{generation.id}")
        db.commit()

        regenerated = client.post(
            f"/api/v1/creator/creations/{creation.id}/source/regenerate",
            headers=headers,
            json={"prompt": "Replacement source", "request_id": "source-replacement"},
        )

    assert regenerated.status_code == 202, regenerated.text
    assert regenerated.json()["upload_id"] == old_upload.id
    assert regenerated.json()["source_preview_url"].endswith(
        f"/creator/previews/{old_upload.id}"
    )
    db.expire_all()
    assert db.get(CreatorCreation, creation.id).upload_id == old_upload.id


def test_unaccepted_source_expires_and_releases_reserved_quota(db, monkeypatch) -> None:
    _enable(monkeypatch)
    user_id, _headers = _creator(db, "expired-source-user")
    now = datetime.now(timezone.utc)
    creation = CreatorCreation(
        id="cr_expired_source",
        user_id=user_id,
        upload_id=None,
        source_mode="prompt",
        source_prompt="An old unaccepted draft",
        source_generation_id="csg_expired_source",
        brief="",
        status="failed",
        progress_stage="failed",
        progress_percent=30,
        created_at=now - timedelta(days=31),
        updated_at=now,
    )
    generation = CreatorSourceGeneration(
        id="csg_expired_source",
        creation_id=creation.id,
        user_id=user_id,
        attempt=1,
        request_id="expired-source-request",
        original_prompt=creation.source_prompt,
        status="failed",
        progress_stage="failed",
        progress_percent=30,
        ivadmin_job_id="cvgj_expired_source",
        quota_date=now.date().isoformat(),
        quota_state="reserved",
        next_poll_at=now,
        expires_at=now - timedelta(seconds=1),
        created_at=now - timedelta(days=31),
        updated_at=now,
    )
    db.add_all([creation, generation])
    db.commit()

    requests = []

    def fake_request(_settings, method, path, **_kwargs):
        requests.append((method, path))
        return httpx.Response(200, json={"deleted": True})

    monkeypatch.setattr("app.worker._request", fake_request)
    assert process_next_expired_source(get_settings()) is True
    db.expire_all()
    db.refresh(generation)
    db.refresh(creation)
    assert generation.status == "expired"
    assert generation.quota_state == "released"
    assert creation.status == "abandoned"
    assert requests == [
        (
            "DELETE",
            "/internal/v1/mobile-creator/video-generations/cvgj_expired_source",
        )
    ]
