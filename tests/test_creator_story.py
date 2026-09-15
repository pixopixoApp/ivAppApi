from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.creator_manual_edits import compile_edits
from app.creator_story import register_cut, story_out, sync_story
from app.credits import balance, grant_welcome_credit, settle_reference
from app.main import app
from app.models import (
    CreatorAccessGrant,
    CreatorCreation,
    CreatorSourceGeneration,
    CreatorUpload,
    CreatorVersion,
    CreditLedgerEntry,
    CreditReservation,
    MediaObject,
    PublishedVideo,
    User,
    UserToken,
)
from app.worker import _apply_source_job, process_next_upload_normalization


def _enable(monkeypatch) -> None:
    monkeypatch.setenv("CREATOR_TEXT_TO_VIDEO_ENABLED", "true")
    monkeypatch.setenv("CREATOR_BRANCH_STORY_ENABLED", "true")
    monkeypatch.setenv("CREATOR_INTERNAL_KEY", "creator-story-key")
    monkeypatch.setenv("IVADMIN_BASE_URL", "http://ivadmin.test:8000")
    monkeypatch.setenv("MEDIA_CACHE_ROOT", "/tmp/ivapp-pytest-media/media-cache")
    get_settings.cache_clear()


def _cache_video(content: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(content).hexdigest()
    cache = Path(get_settings().media_cache_root)
    path = cache / "objects" / digest[:2] / f"{digest}.cache"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return digest, f"local-cache://sha256/{digest}"


def _seed(db, monkeypatch, *, extra_credits: int = 0, suffix: str = "main"):
    _enable(monkeypatch)
    now = datetime.now(timezone.utc)
    user_id = f"story-user-{suffix}"
    token = f"token-{user_id}"
    digest, uri = _cache_video(f"source-{suffix}".encode())
    upload_id = f"up_story_{suffix}"
    creation_id = f"cr_story_{suffix}"
    db.add_all([
        User(user_id=user_id, provider="email", subject=f"{user_id}@example.com"),
        UserToken(token=token, user_id=user_id, created_at=now,
                  expires_at=now + timedelta(days=1)),
        CreatorAccessGrant(user_id=user_id, source="test", granted_at=now),
        CreatorUpload(
            id=upload_id, user_id=user_id, storage_key=uri,
            source_local_uri=uri, source_sha256=digest,
            upload_transport="test", origin="user_upload",
            normalization_status="ready", normalization_profile="mobile-v1",
            playable_local_uri=uri, playable_sha256=digest,
            playable_size_bytes=len(f"source-{suffix}".encode()),
            original_filename="source.mp4",
            size_bytes=len(f"source-{suffix}".encode()), duration_ms=5000,
        ),
        CreatorCreation(
            id=creation_id, request_id=f"prepare-{suffix}", user_id=user_id,
            upload_id=upload_id, source_mode="upload", experience_mode="none",
            status="source_ready", progress_stage="choose_mode", progress_percent=100,
            created_at=now, updated_at=now,
        ),
    ])
    grant_welcome_credit(db, user_id)
    if extra_credits:
        db.add(CreditLedgerEntry(
            id=f"test-credit-{suffix}", user_id=user_id, kind="purchase",
            amount=extra_credits, reference_id="test", note="Test Credits",
            created_at=now,
        ))
    db.commit()
    return creation_id, user_id, {"Authorization": f"Bearer {token}"}


def _save_plan(client: TestClient, creation_id: str, headers: dict[str, str],
               *, owner: str | None = None, split_ms: int = 0):
    response = client.put(
        f"/api/v1/creator/creations/{creation_id}/story-plan",
        headers=headers,
        json={
            "revision": 0,
            "interaction_type": "tap",
            "success_prompt": "A bright doorway opens",
            "miss_prompt": "The doorway fades into mist",
            "source_tail_owner": owner,
            "split_ms": split_ms,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _ready_ending(db, creation_id: str, role: str, content: bytes) -> CreatorUpload:
    creation = db.get(CreatorCreation, creation_id)
    generation_id = creation.story_plan["generation_ids"][role]
    generation = db.get(CreatorSourceGeneration, generation_id)
    digest, uri = _cache_video(content)
    upload = CreatorUpload(
        id=f"up_{generation.id}", user_id=generation.user_id, storage_key=uri,
        source_local_uri=uri, source_sha256=digest, upload_transport="ai-generated",
        origin="ai_generated", source_generation_id=generation.id,
        normalization_status="ready", normalization_profile="mobile-v1",
        playable_local_uri=uri, playable_sha256=digest,
        playable_size_bytes=len(content), original_filename=f"{role}.mp4",
        size_bytes=len(content), duration_ms=3000,
    )
    db.add(upload)
    db.flush()
    generation.upload_id = upload.id
    generation.status = "ready"
    generation.progress_stage = "review_source"
    generation.progress_percent = 100
    generation.quota_state = "charged"
    settle_reference(db, user_id=generation.user_id,
                     reference_id=f"source:{generation.id}")
    sync_story(db, creation)
    db.commit()
    return upload


@pytest.mark.parametrize("replacement,direction", [("shake", None), ("pinch", "inward"), ("pinch", "outward")])
def test_story_two_endings_retry_preview_manual_save_and_publish(db, monkeypatch, replacement, direction):
    creation_id, user_id, headers = _seed(db, monkeypatch, extra_credits=5)
    with TestClient(app) as client:
        planned = _save_plan(client, creation_id, headers)
        assert planned["story"]["revision"] == 1
        assert planned["versions"] == []

        generated = client.post(
            f"/api/v1/creator/creations/{creation_id}/story-generations",
            headers=headers,
            json={"revision": 1, "request_id": "story-bc-1",
                  "targets": ["B", "C"], "operation": "generate"},
        )
        assert generated.status_code == 202, generated.text
        repeated = client.post(
            f"/api/v1/creator/creations/{creation_id}/story-generations",
            headers=headers,
            json={"revision": 1, "request_id": "story-bc-1",
                  "targets": ["B", "C"], "operation": "generate"},
        )
        assert repeated.status_code == 202

    db.expire_all()
    creation = db.get(CreatorCreation, creation_id)
    assert db.query(CreatorSourceGeneration).filter_by(creation_id=creation_id).count() == 2
    assert balance(db, user_id) == 4
    b_generation = db.get(CreatorSourceGeneration, creation.story_plan["generation_ids"]["B"])

    _apply_source_job(db, get_settings(), creation, b_generation, {
        "creation_id": creation_id, "generation_id": b_generation.id,
        "job_id": "job-story-b", "status": "failed", "progress_percent": 40,
        "progress_stage": "failed", "provider_task_accepted": True,
        "error_code": "PROVIDER_FAILED", "error_message": "Video generation failed.",
    })
    _ready_ending(db, creation_id, "C", b"ending-c")
    db.expire_all()
    assert balance(db, user_id) == 7
    assert db.get(CreatorCreation, creation_id).story_plan["status"] == "partial_failure"

    with TestClient(app) as client:
        retried = client.post(
            f"/api/v1/creator/creations/{creation_id}/story-generations",
            headers=headers,
            json={"revision": 1, "request_id": "story-b-retry",
                  "targets": ["B"], "operation": "retry"},
        )
        assert retried.status_code == 202, retried.text
    db.expire_all()
    assert balance(db, user_id) == 4
    assert db.query(CreatorSourceGeneration).filter_by(creation_id=creation_id).count() == 3
    _ready_ending(db, creation_id, "B", b"ending-b-retry")

    db.expire_all()
    creation = db.get(CreatorCreation, creation_id)
    version = db.get(CreatorVersion, creation.active_version_id)
    assert creation.status == "ready"
    assert creation.story_plan["status"] == "ready"
    assert [clip["video_id"] for clip in version.runtime_spec["video"]] == ["A", "B", "C"]
    assert version.runtime_spec["video"][0]["interactions"][0]["detection"]["response_window_ms"] == 4000
    assert version.runtime_spec["video"][1]["on_end"]["action"] == "end_experience"
    assert version.runtime_spec["video"][2]["on_end"]["action"] == "end_experience"

    with TestClient(app) as client:
        blocked = client.post(
            f"/api/v1/creator/creations/{creation_id}/publish", headers=headers,
            json={"confirm": True, "version_id": version.id, "title": "Story"},
        )
        assert blocked.status_code == 409
        for path in ("B", "C"):
            confirmed = client.post(
                f"/api/v1/creator/creations/{creation_id}/preview-confirmations",
                headers=headers, json={"version_id": version.id, "path": path},
            )
            assert confirmed.status_code == 200

        # The interaction switch is deterministic: no generation row or Credit changes.
        db.expire_all()
        version = db.get(CreatorVersion, version.id)
        edits = [{"clip_id": "A", "interaction_id": "action_001", "type": replacement}]
        if direction is not None:
            edits[0]["pinch_direction"] = direction
        _, preview = compile_edits(
            version.source_timeline,
            edits,
            item_id=creation_id, video_url=version.runtime_spec["video"][0]["video"],
            video_urls={clip["video_id"]: clip["video"] for clip in version.runtime_spec["video"]},
        )
        saved = client.post(
            f"/api/v1/creator/creations/{creation_id}/manual-edits", headers=headers,
            json={
                "base_version_id": version.id, "request_id": "story-manual-shake",
                "edits": edits,
                "preview_interactions": [item for clip in preview["video"]
                                         for item in clip["interactions"]],
                "previewed_paths": ["B", "C"],
            },
        )
        assert saved.status_code == 200, saved.text
        saved_version = saved.json()["active_version_id"]
        db.expire_all()
        assert db.get(CreatorVersion, saved_version).runtime_spec["item_id"] == creation_id
        assert db.get(CreatorCreation, creation_id).story_plan["interaction_type"] == replacement
        if direction is not None:
            assert db.get(CreatorCreation, creation_id).story_plan["pinch_direction"] == direction
            assert saved.json()["story"]["pinch_direction"] == direction
        assert db.query(CreatorSourceGeneration).filter_by(creation_id=creation_id).count() == 3
        assert balance(db, user_id) == 4
        published = client.post(
            f"/api/v1/creator/creations/{creation_id}/publish", headers=headers,
            json={"confirm": True, "version_id": saved_version,
                  "title": "Story", "description": "Three clips"},
        )
        assert published.status_code == 200, published.text

    db.expire_all()
    published = db.get(PublishedVideo, creation_id)
    assert published.content_mode == "story"
    assert published.video_url == f"/media/{creation_id}-A.mp4"
    assert {clip["video"] for clip in published.runtime_spec["video"]} == {
        f"/media/{creation_id}-A.mp4",
        f"/media/{creation_id}-B.mp4",
        f"/media/{creation_id}-C.mp4",
    }


def test_story_reusing_one_source_tail_only_reserves_one_ending(db, monkeypatch):
    creation_id, user_id, headers = _seed(db, monkeypatch, suffix="reuse")
    with TestClient(app) as client:
        planned = _save_plan(client, creation_id, headers, owner="B", split_ms=2500)
        generated = client.post(
            f"/api/v1/creator/creations/{creation_id}/story-generations",
            headers=headers,
            json={"revision": planned["story"]["revision"], "request_id": "reuse-c",
                  "targets": ["B", "C"], "operation": "generate"},
        )
    assert generated.status_code == 202, generated.text
    assert db.query(CreatorSourceGeneration).filter_by(creation_id=creation_id).count() == 1
    generation = db.query(CreatorSourceGeneration).filter_by(creation_id=creation_id).one()
    assert generation.generation_kind == "C"
    assert balance(db, user_id) == 2
    assert generated.json()["story"]["clips"]["B"]["source_kind"] == "source_clip"


def test_story_two_ai_endings_are_atomic_when_balance_is_five(db, monkeypatch):
    creation_id, user_id, headers = _seed(db, monkeypatch, suffix="low")
    with TestClient(app) as client:
        _save_plan(client, creation_id, headers)
        result = client.post(
            f"/api/v1/creator/creations/{creation_id}/story-generations",
            headers=headers,
            json={"revision": 1, "request_id": "too-expensive",
                  "targets": ["B", "C"], "operation": "generate"},
        )
    assert result.status_code == 402
    db.expire_all()
    assert balance(db, user_id) == 5
    assert db.query(CreatorSourceGeneration).filter_by(creation_id=creation_id).count() == 0
    assert db.query(CreditReservation).filter_by(user_id=user_id).count() == 0


def test_story_rejects_auto_only_interaction_and_invalid_cut(db, monkeypatch):
    creation_id, _user_id, headers = _seed(db, monkeypatch, suffix="invalid")
    with TestClient(app) as client:
        continuous = client.put(
            f"/api/v1/creator/creations/{creation_id}/story-plan", headers=headers,
            json={"revision": 0, "interaction_type": "continuous_tap",
                  "success_prompt": "yes", "miss_prompt": "no",
                  "source_tail_owner": None, "split_ms": 0},
        )
        short = client.put(
            f"/api/v1/creator/creations/{creation_id}/story-plan", headers=headers,
            json={"revision": 0, "interaction_type": "tap",
                  "success_prompt": "yes", "miss_prompt": "no",
                  "source_tail_owner": "C", "split_ms": 4500},
        )
    assert continuous.status_code == 422
    assert short.status_code == 422


@pytest.mark.parametrize(
    "payload,preset_id,field,value",
    [
        ({"interaction_type": "pinch", "interaction_preset_id": "pinch_out"},
         "pinch_out", "pinch_direction", "outward"),
        ({"interaction_type": "rotate", "interaction_preset_id": "rotate_clockwise"},
         "rotate_clockwise", "rotation_direction", "clockwise"),
        ({"interaction_type": "camera_motion",
          "interaction_preset_id": "camera_motion.face_smile"},
         "camera_motion.face_smile", "vision_target", "face_smile"),
    ],
)
def test_story_plan_persists_semantic_interaction_presets(
    db, monkeypatch, payload, preset_id, field, value,
):
    creation_id, _user_id, headers = _seed(db, monkeypatch, suffix=field)
    with TestClient(app) as client:
        response = client.put(
            f"/api/v1/creator/creations/{creation_id}/story-plan",
            headers=headers,
            json={
                "revision": 0,
                **payload,
                "success_prompt": "yes",
                "miss_prompt": "no",
                "source_tail_owner": None,
                "split_ms": 0,
            },
        )
    assert response.status_code == 200, response.text
    story = response.json()["story"]
    assert story["interaction_preset_id"] == preset_id
    assert story[field] == value


def test_abandon_story_cancels_unstarted_endings_and_releases_holds(db, monkeypatch):
    creation_id, user_id, headers = _seed(db, monkeypatch, extra_credits=5, suffix="abandon")
    with TestClient(app) as client:
        _save_plan(client, creation_id, headers)
        generated = client.post(
            f"/api/v1/creator/creations/{creation_id}/story-generations",
            headers=headers,
            json={"revision": 1, "request_id": "abandon-bc",
                  "targets": ["B", "C"], "operation": "generate"},
        )
        assert generated.status_code == 202
        assert balance(db, user_id) == 4
        abandoned = client.delete(
            f"/api/v1/creator/creations/{creation_id}", headers=headers,
        )
        assert abandoned.status_code == 200, abandoned.text

    db.expire_all()
    assert db.get(CreatorCreation, creation_id).status == "abandoned"
    assert balance(db, user_id) == 10
    generations = db.query(CreatorSourceGeneration).filter_by(creation_id=creation_id).all()
    assert {item.status for item in generations} == {"cancelled"}
    assert {item.quota_state for item in generations} == {"released"}


def test_story_cut_is_healed_previewable_prefetched_and_not_normalized_again(
    db, monkeypatch
):
    creation_id, _user_id, headers = _seed(db, monkeypatch, suffix="cut-heal")
    with TestClient(app) as client:
        _save_plan(client, creation_id, headers, owner="B", split_ms=2000)

    content = b"durable-story-cut"
    digest = hashlib.sha256(content).hexdigest()
    media = MediaObject(
        id="mo_story_cut_heal",
        purpose="creator_source",
        origin="user_upload",
        visibility="private",
        state="ready",
        staging_key="",
        object_key="ivapp-media/v1/private/creator-sources/heal/source.mp4",
        original_filename="source.mp4",
        content_type="video/mp4",
        size_bytes=len(content),
        sha256=digest,
        etag="story-cut-etag",
    )
    db.add(media)
    db.flush()
    metadata = {
        "source_media_object_id": media.id,
        "source_storage_key": media.object_key,
        "source_sha256": digest,
        "source_size_bytes": len(content),
        "source_duration_ms": 2000,
    }
    warmed: list[str] = []
    monkeypatch.setattr(
        "app.creator_story.enqueue_private_media_prefetch",
        lambda _db, _settings, keys: warmed.extend(keys) or [],
    )
    creation = db.get(CreatorCreation, creation_id)
    upload_id = register_cut(db, creation, metadata, "A")
    upload = db.get(CreatorUpload, upload_id)
    upload.normalization_status = "failed"
    upload.normalization_error = "legacy worker retried a derived clip"
    upload.playable_media_object_id = None
    upload.playable_local_uri = ""
    db.flush()

    assert register_cut(db, creation, metadata, "A") == upload_id
    plan = dict(creation.story_plan)
    plan["assets"] = {"A": upload_id}
    creation.story_plan = plan
    source = db.get(CreatorUpload, creation.upload_id)
    source.normalization_status = "failed"
    db.commit()

    db.refresh(upload)
    assert upload.normalization_status == "ready"
    assert upload.normalization_error == ""
    assert upload.playable_media_object_id == media.id
    assert story_out(db, creation)["clips"]["A"]["preview_url"].endswith(upload_id)
    assert warmed == [media.object_key, media.object_key]

    monkeypatch.setenv("MEDIA_STORAGE_MODE", "oss")
    get_settings.cache_clear()
    assert process_next_upload_normalization(get_settings()) is False
