import hashlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.creator_interaction_presets import creator_interaction_presets
from app.creator_manual_edits import (
    compile_edits,
    manual_edit_options,
    replace_interactions,
)
from app.main import app
from app.models import (
    CreatorAccessGrant,
    CreatorCreation,
    CreatorUpload,
    CreatorVersion,
    CreditLedgerEntry,
    PublishedVideo,
    User,
    UserToken,
)
from app.protocol_video import (
    RuntimeSpecError,
    compile_runtime_spec,
)

TIMELINE = {
    "media": {"duration_ms": 5000},
    "interactions": [
        {"gesture": "tap", "gate_at_ms": 1000, "gate_end_ms": 1600,
         "region": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}},
        {"gesture": "rotate", "gate_at_ms": 3000, "rotation_direction": "clockwise"},
    ],
}


def runtime(source=TIMELINE):
    return compile_runtime_spec(item_id="cr_edit", content_mode="single", source=source, video_url="/preview.mp4")


@pytest.fixture
def seeded(db, monkeypatch):
    now = datetime.now(timezone.utc)
    for user_id in ("editor", "other"):
        db.add(User(user_id=user_id, provider="email", subject=f"{user_id}@example.com"))
        db.add(UserToken(token=f"token-{user_id}", user_id=user_id, created_at=now, expires_at=now + timedelta(days=1)))
        db.add(CreatorAccessGrant(user_id=user_id, source="test", granted_at=now))
    content = b"manual-edit-source-video"
    digest = hashlib.sha256(content).hexdigest()
    cache = Path("/tmp/ivapp-pytest-media/media-cache")
    path = cache / "objects" / digest[:2] / f"{digest}.cache"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    monkeypatch.setenv("MEDIA_CACHE_ROOT", str(cache))
    get_settings.cache_clear()
    db.add(CreatorUpload(id="up_edit", user_id="editor", storage_key="creator_uploads/editor/up_edit.mp4",
                         original_filename="test.mp4", size_bytes=len(content), duration_ms=5000,
                         normalization_status="ready", playable_sha256=digest, playable_size_bytes=len(content)))
    db.add(CreatorCreation(id="cr_edit", user_id="editor", upload_id="up_edit", status="ready",
                           progress_stage="ready", progress_percent=100, active_version_id="cv_base",
                           source_timeline=TIMELINE, runtime_spec=runtime(), runtime_spec_version="1.1"))
    db.add(CreatorVersion(id="cv_base", creation_id="cr_edit", user_id="editor", number=1,
                         request_id="base", brief="Original analysis", status="ready", progress_stage="ready",
                         progress_percent=100, source_timeline=TIMELINE, runtime_spec=runtime(), runtime_spec_version="1.1",
                         ivadmin_run_id="original-ai-run"))
    db.commit()
    # Fail loudly if manual editing ever starts an AI worker call or reserves credits.
    def forbidden(*args, **kwargs):
        raise AssertionError("Manual editing must not call AI or reserve credits")
    monkeypatch.setattr("app.worker._request", forbidden)
    monkeypatch.setattr("app.routers.platform.reserve_credits", forbidden)
    return {"Authorization": "Bearer token-editor"}


def payload(edits=None, request_id="manual-1"):
    edits = edits or [{"interaction_id": "action_001", "type": "swipe_up"}]
    _, result = compile_edits(TIMELINE, edits, item_id="cr_edit", video_url="/preview.mp4")
    return {"base_version_id": "cv_base", "request_id": request_id, "edits": edits,
            "preview_interactions": result["video"][0]["interactions"]}


def test_options_cover_every_supported_type_and_match_saved_nodes():
    source = deepcopy(TIMELINE)
    options = manual_edit_options(source, runtime())
    preset_ids = {preset.id for preset in creator_interaction_presets()}
    legacy_aliases = {"pinch", "rotate", "camera_motion", "camera_continuous"}
    assert set(options["action_001"]) == preset_ids | legacy_aliases
    for action_id, choices in options.items():
        for choice in choices.values():
            edit = {
                "interaction_id": action_id,
                "type": choice["interaction"]["type"],
                "preset_id": choice["preset_id"],
            }
            timeline, compiled = compile_edits(source, [edit],
                                               item_id="cr_edit", video_url="/preview.mp4")
            node = next(item for item in compiled["video"][0]["interactions"] if item["id"] == action_id)
            assert choice["interaction"] == node
            assert choice["runtime_spec_version"] == compiled["version"]
            assert [i["gate_at_ms"] for i in timeline["interactions"]] == [1000, 3000]
    assert source == TIMELINE


def test_parameterized_choices_keep_each_direction_and_camera_target_distinct():
    choices = manual_edit_options(deepcopy(TIMELINE), runtime())["action_001"]
    expected = {
        "pinch_in": ("pinch", "inward", None, None),
        "pinch_out": ("pinch", "outward", None, None),
        "rotate_clockwise": ("rotate", None, "clockwise", None),
        "rotate_counterclockwise": ("rotate", None, "counterclockwise", None),
        "camera_motion.face_smile": ("camera_motion", None, None, "face_smile"),
        "camera_motion.hand_thumb_up": ("camera_motion", None, None, "hand_thumb_up"),
        "camera_motion.hand_open_palm": ("camera_motion", None, None, "hand_open_palm"),
        "camera_continuous.hand_finger_snap": (
            "camera_continuous", None, None, "hand_finger_snap",
        ),
        "camera_continuous.hand_finger_gun_recoil": (
            "camera_continuous", None, None, "hand_finger_gun_recoil",
        ),
    }
    for preset_id, (interaction_type, pinch, rotation, vision) in expected.items():
        interaction = choices[preset_id]["interaction"]
        detection = interaction["detection"]
        assert interaction["type"] == interaction_type
        assert detection.get("pinch_direction") == pinch
        assert detection.get("rotation_direction") == rotation
        assert (detection.get("vision") or {}).get("target") == vision
        if preset_id == "camera_motion.hand_thumb_up":
            assert detection["vision"] == {
                "registry_version": "v1",
                "target": "hand_thumb_up",
                "camera_facing": "front",
                "show_preview": True,
                "min_confidence": 0.60,
                "stable_for_ms": 250,
            }


def test_replacement_resets_only_incompatible_fields_and_keeps_other_moments():
    changed, compiled = compile_edits(TIMELINE, [{"interaction_id": "action_001", "type": "continuous_tap"}],
                                      item_id="cr_edit", video_url="/preview.mp4")
    assert changed["interactions"][1] == TIMELINE["interactions"][1]
    assert "gate_end_ms" not in changed["interactions"][0]
    assert "region" not in changed["interactions"][0]
    assert compiled["video"][0]["interactions"][0]["feedback"]["animation"] == "none"
    assert compiled["version"] == "1.2"
    assert replace_interactions(TIMELINE, [{"interaction_id": "action_002", "type": "rotate"}]) == TIMELINE


def test_save_is_ready_idempotent_private_and_does_not_change_original(db, seeded):
    with TestClient(app) as client:
        original = client.get("/api/v1/creator/creations/cr_edit", headers=seeded).json()
        assert original["versions"][0]["manual_edit_options"]
        denied = client.post("/api/v1/creator/creations/cr_edit/manual-edits",
                             headers={"Authorization": "Bearer token-other"}, json=payload())
        assert denied.status_code == 404
        saved = client.post("/api/v1/creator/creations/cr_edit/manual-edits", headers=seeded, json=payload())
        assert saved.status_code == 200, saved.text
        retried = client.post("/api/v1/creator/creations/cr_edit/manual-edits", headers=seeded, json=payload())
        assert retried.status_code == 200
        assert retried.json()["active_version_id"] == saved.json()["active_version_id"]
        assert len(retried.json()["versions"]) == 2
        latest = saved.json()["versions"][-1]
        assert (latest["status"], latest["progress_stage"], latest["progress_percent"]) == ("ready", "ready", 100)
        assert latest["runtime_spec"]["video"][0]["interactions"] == payload()["preview_interactions"]
        assert saved.json()["versions"][0]["manual_edit_options"] == {}
        assert latest["manual_edit_options"]
        assert client.post("/api/v1/creator/creations/cr_edit/manual-edits", headers=seeded,
                           json=payload(request_id="stale-base")).status_code == 409
        other_payload = payload([{"interaction_id": "action_001", "type": "shake"}])
        assert client.post("/api/v1/creator/creations/cr_edit/manual-edits", headers=seeded,
                           json=other_payload).status_code == 409
    db.expire_all()
    assert db.get(CreatorVersion, "cv_base").source_timeline == TIMELINE
    saved_row = db.get(CreatorVersion, latest["version_id"])
    assert saved_row.ivadmin_job_id == saved_row.ivadmin_run_id == ""
    assert db.query(CreditLedgerEntry).count() == 0


def test_saved_version_is_used_by_publish_and_restore(db, seeded):
    with TestClient(app) as client:
        saved = client.post("/api/v1/creator/creations/cr_edit/manual-edits", headers=seeded, json=payload()).json()
        restored = client.get("/api/v1/creator/creations/active", headers=seeded).json()
        assert restored["active_version_id"] == saved["active_version_id"]
        published = client.post("/api/v1/creator/creations/cr_edit/publish", headers=seeded,
                                json={"confirm": True, "version_id": saved["active_version_id"], "title": "Edited"})
        assert published.status_code == 200, published.text
        assert client.post("/api/v1/creator/creations/cr_edit/manual-edits", headers=seeded,
                           json=payload(request_id="after-publish")).status_code == 409
    db.expire_all()
    assert db.get(PublishedVideo, "cr_edit").runtime_spec["video"][0]["interactions"] == payload()["preview_interactions"]


@pytest.mark.parametrize("invalid", ["unknown_id", "duplicate_id", "unknown_type", "preview_mismatch", "arbitrary_field"])
def test_rejects_invalid_edits_without_creating_a_version(db, seeded, invalid):
    body = payload()
    if invalid == "unknown_id": body["edits"][0]["interaction_id"] = "missing"
    if invalid == "duplicate_id": body["edits"].append(body["edits"][0])
    if invalid == "unknown_type": body["edits"][0]["type"] = "invented"
    if invalid == "preview_mismatch": body["preview_interactions"][0]["offset_time_ms"] = 999
    if invalid == "arbitrary_field": body["runtime_spec"] = {"video": "https://attacker.invalid"}
    with TestClient(app) as client:
        result = client.post("/api/v1/creator/creations/cr_edit/manual-edits", headers=seeded, json=body)
    assert result.status_code in {409, 422}
    assert db.query(CreatorVersion).count() == 1


def test_cannot_edit_during_analysis(db, seeded):
    db.get(CreatorVersion, "cv_base").status = "running"
    db.commit()
    with TestClient(app) as client:
        assert client.post("/api/v1/creator/creations/cr_edit/manual-edits", headers=seeded,
                           json=payload()).status_code == 409


def test_explicit_ai_reanalysis_remains_available_after_manual_save(db, seeded):
    with TestClient(app) as client:
        saved = client.post("/api/v1/creator/creations/cr_edit/manual-edits", headers=seeded, json=payload()).json()
        response = client.post("/api/v1/creator/creations/cr_edit/versions", headers=seeded,
                               json={"brief": "Re-analyze the original video", "request_id": "explicit-ai-request"})
        assert response.status_code == 202
        assert response.json()["versions"][-1]["status"] == "queued"
    db.expire_all()
    assert db.get(CreatorVersion, saved["active_version_id"]).status == "ready"
    assert db.get(CreatorVersion, "cv_base").ivadmin_run_id == "original-ai-run"


def test_invalid_continuous_interval_is_not_offered():
    timeline = deepcopy(TIMELINE)
    timeline["interactions"][1]["gate_at_ms"] = 1000
    choices = manual_edit_options(timeline, runtime(timeline))["action_001"]
    assert "tap" in choices and "continuous_tap" not in choices
    with pytest.raises(RuntimeSpecError):
        compile_edits(timeline, [{"interaction_id": "action_001", "type": "continuous_tap"}],
                      item_id="cr_edit", video_url="/preview.mp4")


@pytest.mark.parametrize("direction,version", [("inward", "1.1"), ("outward", "1.7")])
def test_pinch_direction_save_restore_publish_without_ai_or_credits(db, seeded, direction, version):
    edits = [{"interaction_id": "action_001", "type": "pinch", "pinch_direction": direction}]
    body = payload(edits, f"pinch-{direction}")
    with TestClient(app) as client:
        saved = client.post("/api/v1/creator/creations/cr_edit/manual-edits", headers=seeded, json=body)
        assert saved.status_code == 200, saved.text
        data = saved.json()
        latest = data["versions"][-1]
        assert latest["runtime_spec"]["version"] == version
        assert latest["runtime_spec"]["video"][0]["interactions"] == body["preview_interactions"]
        restored = client.get("/api/v1/creator/creations/active", headers=seeded).json()
        assert restored["active_version_id"] == data["active_version_id"]
        assert client.post("/api/v1/creator/creations/cr_edit/publish", headers=seeded, json={"confirm": True, "version_id": data["active_version_id"], "title": "Pinch regression"}).status_code == 200
    db.expire_all()
    assert db.get(CreatorCreation, "cr_edit").source_timeline["interactions"][0]["pinch_direction"] == direction
    assert db.get(PublishedVideo, "cr_edit").runtime_spec["video"][0]["interactions"][0]["detection"]["pinch_direction"] == direction
    assert db.query(CreditLedgerEntry).count() == 0


def test_direction_only_change_preserves_branch_timing_and_removes_stale_direction():
    original = {"interactions": [{"gesture": "pinch", "pinch_direction": "inward", "gate_at_ms": 3000, "gate_end_ms": 5000, "outcomes": [{"result": "success", "target": "B"}]}]}
    changed = replace_interactions(original, [{"interaction_id": "action_001", "type": "pinch", "pinch_direction": "outward"}])
    assert changed["interactions"][0] == {**original["interactions"][0], "pinch_direction": "outward"}
    assert "pinch_direction" not in replace_interactions(changed, [{"interaction_id": "action_001", "type": "tap"}])["interactions"][0]
