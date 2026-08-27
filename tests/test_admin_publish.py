from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.models import MediaObject, PublishedVideo, User


def test_admin_publish_compiles_before_persisting_and_preserves_old_media_on_error(db) -> None:
    db.add(User(user_id="author", provider="email", subject="author@example.com"))
    db.commit()
    headers = {"X-Publish-Key": "test-publish-key"}
    form = {
        "video_id": "single-demo",
        "version": "1",
        "user_id": "author",
        "content_mode": "single",
        "timeline": json.dumps(
            {"interactions": [{"gesture": "mic_level", "gate_at_ms": 500}]}
        ),
    }
    with TestClient(app) as client:
        created = client.post(
            "/internal/v1/publish",
            headers=headers,
            data=form,
            files={"video": ("video.mp4", b"old-video", "video/mp4")},
        )
        invalid = client.post(
            "/internal/v1/publish",
            headers=headers,
            data={
                **form,
                "version": "2",
                "timeline": json.dumps(
                    {"interactions": [{"gesture": "unknown", "gate_at_ms": 500}]}
                ),
            },
            files={"video": ("video.mp4", b"new-video", "video/mp4")},
        )
    assert created.status_code == 200
    assert created.json()["runtime_spec_version"] == "1.1"
    assert invalid.status_code == 400
    db.expire_all()
    row = db.get(PublishedVideo, "single-demo")
    assert row.version == "1"
    interaction = row.runtime_spec["video"][0]["interactions"][0]
    assert interaction["detection"]["response_window_ms"] == 0
    assert Path("/tmp/ivapp-pytest-media/single-demo.mp4").read_bytes() == b"old-video"


def test_story_publish_keeps_clip_on_end_in_persisted_runtime(db) -> None:
    db.add(User(user_id="author", provider="email", subject="author@example.com"))
    db.commit()
    story = {
        "entry_clip_id": "intro",
        "clips": {
            "intro": {
                "timeline": {"interactions": []},
                "on_end": {"action": "goto", "clip_id": "ending"},
            },
            "ending": {"timeline": {"interactions": []}},
        },
    }
    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/publish",
            headers={"X-Publish-Key": "test-publish-key"},
            data={
                "video_id": "story-demo",
                "version": "1",
                "user_id": "author",
                "content_mode": "story",
                "story": json.dumps(story),
            },
            files=[
                ("clips", ("intro.mp4", b"intro", "video/mp4")),
                ("clips", ("ending.mp4", b"ending", "video/mp4")),
            ],
        )
    assert response.status_code == 200
    db.expire_all()
    row = db.get(PublishedVideo, "story-demo")
    assert row.timeline["clips"]["intro"]["on_end"]["clip_id"] == "ending"
    assert row.runtime_spec["video"][0]["on_end"]["target_video_id"] == "ending"


def test_preview_runtime_does_not_materialize_unset_optional_fields(
    db,
    monkeypatch,
) -> None:
    media = MediaObject(
        id="preview-media",
        purpose="runtime_source",
        origin="ivadmin",
        visibility="public",
        state="ready",
        staging_key="staging/preview-media.mp4",
        object_key="runtime/preview-media.mp4",
        original_filename="preview-media.mp4",
        content_type="video/mp4",
        size_bytes=1024,
        sha256="a" * 64,
    )
    db.add(media)
    db.commit()
    monkeypatch.setattr("app.routers.admin.media_mode_is_oss", lambda _settings: True)
    monkeypatch.setattr(
        "app.routers.admin.public_url",
        lambda _settings, _key: "https://cdn.test/runtime/preview-media.mp4",
    )

    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/preview-runtime",
            headers={"X-Publish-Key": "test-publish-key"},
            json={
                "preview_id": "continuous-preview",
                "content_mode": "single",
                "timeline": {
                    "media": {"duration_ms": 10_000},
                    "interactions": [
                        {
                            "gesture": "continuous_swipe",
                            "gate_at_ms": 1000,
                            "hint": "持续往复滑动以播放",
                        }
                    ],
                },
                "assets": [
                    {
                        "role": "single",
                        "media_object_id": "preview-media",
                    }
                ],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "pixo.mobile-preview.v1"
    assert body["runtime_spec"]["video"][0]["interactions"][0]["type"] == (
        "continuous_swipe"
    )


def test_story_preview_compiles_v11_terminal_and_retry_routes(db, monkeypatch) -> None:
    for index, clip_id in enumerate(("preview-a", "preview-b", "preview-c")):
        db.add(
            MediaObject(
                id=clip_id,
                purpose="runtime_source",
                origin="ivadmin",
                visibility="public",
                state="ready",
                staging_key=f"staging/{clip_id}.mp4",
                object_key=f"runtime/{clip_id}.mp4",
                original_filename=f"{clip_id}.mp4",
                content_type="video/mp4",
                size_bytes=1024,
                sha256=str(index + 1) * 64,
            )
        )
    db.commit()
    monkeypatch.setattr("app.routers.admin.media_mode_is_oss", lambda _settings: True)
    monkeypatch.setattr(
        "app.routers.admin.public_url",
        lambda _settings, key: f"https://cdn.test/{key}",
    )
    story = {
        "entry_clip_id": "a",
        "clips": {
            "a": {
                "timeline": {
                    "interactions": [{
                        "gesture": "tap",
                        "gate_at_ms": 1000,
                        "gate_end_ms": 4000,
                        "outcomes": {
                            "success": {"action": "goto", "clip_id": "b"},
                            "fail": {"action": "goto", "clip_id": "c"},
                        },
                    }],
                },
            },
            "b": {
                "timeline": {"interactions": []},
                "on_end": {"action": "end"},
            },
            "c": {
                "timeline": {"interactions": []},
                "on_end": {"action": "retry_previous_point"},
            },
        },
    }

    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/preview-runtime",
            headers={"X-Publish-Key": "test-publish-key"},
            json={
                "preview_id": "branch-preview",
                "content_mode": "story",
                "story": story,
                "assets": [
                    {"role": "clip", "clip_id": "a", "media_object_id": "preview-a"},
                    {"role": "clip", "clip_id": "b", "media_object_id": "preview-b"},
                    {"role": "clip", "clip_id": "c", "media_object_id": "preview-c"},
                ],
            },
        )

    assert response.status_code == 200
    spec = response.json()["runtime_spec"]
    assert spec["version"] == "1.1"
    clips = {clip["video_id"]: clip for clip in spec["video"]}
    assert clips["b"]["on_end"] == {
        "action": "end_experience",
        "timing": "immediate",
    }
    assert clips["c"]["on_end"] == {"action": "retry_previous_point"}
