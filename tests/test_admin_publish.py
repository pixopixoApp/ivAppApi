from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.models import PublishedVideo, User
from app.protocol_video import RUNTIME_SPEC_VERSION


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
    assert created.json()["runtime_spec_version"] == RUNTIME_SPEC_VERSION
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
