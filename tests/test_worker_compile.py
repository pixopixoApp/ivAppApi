from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.config import get_settings
from app.models import CreatorCreation, CreatorUpload, CreatorVersion
from app.worker import (
    _apply_remote_job,
    process_creator_version,
    process_next_upload_normalization,
)


def test_pending_normalization_is_not_starved_by_ready_backup_sync(
    db,
    monkeypatch,
) -> None:
    now = datetime.now(timezone.utc)
    ready_without_backup = CreatorUpload(
        id="up_ready_without_backup",
        user_id="creator-old",
        storage_key="creator_uploads/creator-old/ready.mp4",
        original_filename="ready.mp4",
        size_bytes=10,
        duration_ms=1_000,
        source_sha256="a" * 64,
        normalization_job_id="mnj-ready",
        normalization_status="ready",
        playable_local_uri="local-cache://sha256/" + "b" * 64,
        playable_sha256="b" * 64,
        playable_size_bytes=10,
        created_at=now - timedelta(minutes=5),
    )
    pending = CreatorUpload(
        id="up_pending",
        user_id="creator-new",
        storage_key="creator_uploads/creator-new/pending.mp4",
        original_filename="pending.mp4",
        size_bytes=10,
        duration_ms=1_000,
        source_sha256="c" * 64,
        normalization_status="pending",
        created_at=now,
    )
    db.add_all([ready_without_backup, pending])
    db.commit()
    processed: list[str] = []

    def fake_process(_db, _settings, upload) -> None:
        processed.append(upload.id)

    monkeypatch.setattr("app.worker.process_upload_normalization", fake_process)

    assert process_next_upload_normalization() is True
    assert processed == [pending.id]


def test_local_mode_does_not_poll_ready_uploads_for_oss_backup(db, monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    db.add(
        CreatorUpload(
            id="up_local_ready",
            user_id="creator",
            storage_key="creator_uploads/creator/local.mp4",
            original_filename="local.mp4",
            size_bytes=10,
            duration_ms=1_000,
            source_sha256="d" * 64,
            normalization_job_id="mnj-local",
            normalization_status="ready",
            playable_local_uri="local-cache://sha256/" + "e" * 64,
            playable_sha256="e" * 64,
            playable_size_bytes=10,
            created_at=now,
        )
    )
    db.commit()
    monkeypatch.setenv("MEDIA_STORAGE_MODE", "local")
    get_settings.cache_clear()
    try:
        assert process_next_upload_normalization() is False
    finally:
        get_settings.cache_clear()


def test_ivadmin_ready_timeline_is_persisted_as_runtime_version(db) -> None:
    now = datetime.now(timezone.utc)
    upload = CreatorUpload(
        id="up_worker",
        user_id="creator",
        storage_key="creator_uploads/creator/up_worker.mp4",
        original_filename="video.mp4",
        size_bytes=10,
        duration_ms=5000,
        created_at=now,
    )
    creation = CreatorCreation(
        id="cr_worker",
        user_id="creator",
        upload_id=upload.id,
        status="running",
        progress_stage="find_playable_moments",
        progress_percent=55,
        created_at=now,
        updated_at=now,
    )
    version = CreatorVersion(
        id="cv_worker",
        creation_id=creation.id,
        user_id="creator",
        number=1,
        request_id="request-worker",
        brief="Add a tap",
        status="running",
        progress_stage="find_playable_moments",
        progress_percent=55,
        created_at=now,
        updated_at=now,
    )
    db.add_all([upload, creation, version])
    db.commit()

    _apply_remote_job(
        db,
        creation,
        version,
        {
            "status": "ready",
            "job_id": "job-1",
            "run_id": "run-1",
            "version": "0.0.1",
            "progress_stage": "ready",
            "progress_percent": 100,
            "timeline": {
                "interactions": [
                    {"gesture": "tap", "gate_at_ms": 1000, "hint": "Tap now"}
                ]
            },
        },
    )

    db.refresh(version)
    db.refresh(creation)
    assert version.status == "ready"
    assert version.runtime_spec_version == "1.1"
    assert creation.active_version_id == version.id
    assert creation.status == "ready"
    assert version.runtime_spec["video"][0]["interactions"][0]["type"] == "tap"


def test_creator_coordinator_uses_private_ivadmin_contract_for_initial_and_fifo_version(
    db,
    monkeypatch,
) -> None:
    """Contract E2E for ivapp -> ivadmin without putting model credentials in ivapp."""
    monkeypatch.setenv("CREATOR_INTERNAL_KEY", "creator-contract-key")
    monkeypatch.setenv("IVADMIN_BASE_URL", "http://ivadmin.test:8000")
    get_settings.cache_clear()
    settings = get_settings()

    source = Path(settings.media_root) / "private" / "creator_uploads" / "creator" / "up_contract.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"contract-video")
    now = datetime.now(timezone.utc)
    upload = CreatorUpload(
        id="up_contract",
        user_id="creator",
        storage_key="creator_uploads/creator/up_contract.mp4",
        original_filename="video.mp4",
        size_bytes=source.stat().st_size,
        duration_ms=5_000,
        source_sha256="a" * 64,
        normalization_job_id="mnj-contract",
        normalization_status="ready",
        playable_local_uri="local-cache://sha256/" + "b" * 64,
        playable_sha256="b" * 64,
        playable_size_bytes=source.stat().st_size,
        created_at=now,
    )
    creation = CreatorCreation(
        id="cr_contract",
        user_id="creator",
        upload_id=upload.id,
        status="running",
        progress_stage="queued",
        progress_percent=1,
        created_at=now,
        updated_at=now,
    )
    first = CreatorVersion(
        id="cv_contract_1",
        creation_id=creation.id,
        user_id="creator",
        number=1,
        request_id="contract-request-1",
        brief="Add a tap",
        status="running",
        progress_stage="queued",
        progress_percent=1,
        created_at=now,
        updated_at=now,
    )
    db.add_all([upload, creation, first])
    db.commit()

    requests: list[dict] = []

    def fake_request(method, url, **kwargs):
        requests.append({"method": method, "url": url, **kwargs})
        number = len(requests)
        payload = {
            "status": "ready",
            "job_id": f"job-{number}",
            "run_id": "run-contract",
            "version": f"0.0.{number}",
            "progress_stage": "ready",
            "progress_percent": 100,
            "timeline": {
                "interactions": [
                    {"gesture": "tap", "gate_at_ms": 1_000 + number},
                ],
            },
        }
        return httpx.Response(
            200,
            json=payload,
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("app.worker.httpx.request", fake_request)
    process_creator_version(db, settings, first)

    second = CreatorVersion(
        id="cv_contract_2",
        creation_id=creation.id,
        user_id="creator",
        number=2,
        request_id="contract-request-2",
        brief="Move it earlier",
        status="running",
        progress_stage="queued",
        progress_percent=1,
        created_at=now,
        updated_at=now,
    )
    db.add(second)
    db.commit()
    process_creator_version(db, settings, second)

    assert requests[0]["method"] == "POST"
    assert requests[0]["url"].endswith("/internal/v1/mobile-creator/jobs/from-normalization")
    assert requests[0]["headers"]["X-Creator-Internal-Key"] == "creator-contract-key"
    assert requests[0]["json"] == {
        "request_id": "contract-request-1",
        "creation_id": "cr_contract",
        "brief": "Add a tap",
        "normalization_job_id": "mnj-contract",
    }
    assert requests[1]["url"].endswith(
        "/internal/v1/mobile-creator/runs/run-contract/versions"
    )
    assert requests[1]["json"] == {
        "request_id": "contract-request-2",
        "creation_id": "cr_contract",
        "brief": "Move it earlier",
    }
    db.refresh(first)
    db.refresh(second)
    assert first.status == second.status == "ready"
    assert second.runtime_spec["video"][0]["interactions"][0]["type"] == "tap"
