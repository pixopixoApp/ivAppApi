from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.models import (
    CreatorAccessGrant,
    CreatorCreation,
    CreatorSourceGeneration,
    CreatorUpload,
    CreatorVersion,
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

    assert created.status_code == 202
    assert created.json()["source_mode"] == "prompt"
    assert created.json()["upload_id"] is None
    assert created.json()["versions"] == []
    assert created.json()["generation_quota"]["reserved"] == 1
    assert repeated.json()["creation_id"] == created.json()["creation_id"]
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["generation_quota"]["reserved"] == 0
    generation = db.get(
        CreatorSourceGeneration,
        created.json()["source_generation_id"],
    )
    assert generation is not None
    assert generation.user_id == user_id
    assert generation.quota_state == "released"


def test_source_worker_charges_on_provider_acceptance_and_accept_starts_analysis(
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
                "duration_seconds": 10,
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
            "source_duration_ms": 10_000,
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
    assert generation.quota_state == "charged"
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
    db.add(upload)
    db.commit()
    process_source_generation(db, settings, generation)
    db.refresh(generation)
    db.refresh(creation)
    assert generation.status == "ready"
    assert creation.status == "source_ready"

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

    assert reviewed.json()["source_preview_url"].endswith(f"/{upload.id}/media")
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
    }


def test_daily_generation_quota_blocks_fourth_provider_attempt(db, monkeypatch) -> None:
    _enable(monkeypatch)
    _user_id, headers = _creator(db, "quota-user")

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

    assert blocked.status_code == 429
    assert blocked.json()["detail"]["limit"] == 3


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
