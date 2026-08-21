from __future__ import annotations

import copy

import pytest

from app.models import PublishedVideo
from app.protocol_video import (
    RUNTIME_SPEC_VERSION,
    RuntimeSpecError,
    compile_runtime_spec,
    read_runtime_spec,
)
from app.runtime_backfill import compile_all_runtime_specs


def test_compile_preserves_response_window_and_pause_semantics() -> None:
    source = {
        "interactions": [
            {
                "gesture": "mic_blow",
                "gate_at_ms": 1000,
                "gate_end_ms": 1200,
                "hint": "Blow",
            }
        ]
    }
    spec = compile_runtime_spec(
        item_id="demo",
        content_mode="single",
        source=source,
        video_url="/media/demo.mp4",
    )
    interaction = spec["video"][0]["interactions"][0]
    assert interaction["pause_video"] is True
    assert interaction["detection"]["response_window_ms"] == 200
    assert interaction["detection"]["min_volume_score"] == 55
    assert interaction["detection"]["min_duration_ms"] == 300
    assert read_runtime_spec(
        spec,
        item_id="demo",
        version=RUNTIME_SPEC_VERSION,
    )[0].interactions[0].type == "mic_blow"


def test_unknown_gesture_fails_closed() -> None:
    with pytest.raises(RuntimeSpecError, match="unsupported gesture"):
        compile_runtime_spec(
            item_id="demo",
            content_mode="single",
            source={"interactions": [{"gesture": "magic", "gate_at_ms": 1}]},
            video_url="/media/demo.mp4",
        )


def test_story_on_end_is_compiled_and_validated() -> None:
    story = {
        "entry_clip_id": "a",
        "clips": {
            "a": {
                "timeline": {"interactions": []},
                "on_end": {"action": "goto", "clip_id": "b"},
            },
            "b": {"timeline": {"interactions": []}},
        },
    }
    spec = compile_runtime_spec(
        item_id="story",
        content_mode="story",
        source=story,
        video_url="/media/story/a.mp4",
    )
    assert spec["video"][0]["on_end"] == {
        "action": "jump_video",
        "target_video_id": "b",
        "timing": "immediate",
    }


def test_story_uses_exact_published_oss_url_for_every_clip() -> None:
    story = {
        "entry_clip_id": "a",
        "clips": {
            "a": {"timeline": {"interactions": []}},
            "b": {"timeline": {"interactions": []}},
        },
    }
    urls = {
        "a": "https://cdn.test/runtime/story/a.mp4",
        "b": "https://cdn.test/runtime/story/b.mp4",
    }
    spec = compile_runtime_spec(
        item_id="story",
        content_mode="story",
        source=story,
        video_url=urls["a"],
        video_urls=urls,
    )
    assert {clip["video_id"]: clip["video"] for clip in spec["video"]} == urls

    with pytest.raises(RuntimeSpecError, match="media URLs missing"):
        compile_runtime_spec(
            item_id="story",
            content_mode="story",
            source=story,
            video_url=urls["a"],
            video_urls={"a": urls["a"]},
        )


def test_persisted_runtime_data_accepts_explicit_response_window() -> None:
    spec = compile_runtime_spec(
        item_id="demo",
        content_mode="single",
        source={"interactions": [{"gesture": "tap", "gate_at_ms": 10}]},
        video_url="/media/demo.mp4",
    )
    broken = copy.deepcopy(spec)
    broken["video"][0]["interactions"][0]["detection"]["response_window_ms"] = 500
    assert read_runtime_spec(broken, item_id="demo", version=RUNTIME_SPEC_VERSION)


def test_camera_motion_requires_a_whitelisted_semantic_target() -> None:
    source = {
        "interactions": [{
            "gesture": "camera_motion",
            "gate_at_ms": 1000,
            "gate_end_ms": 6000,
            "pause_video": False,
            "vision": {
                "target": "hand_victory",
                "camera_facing": "front",
                "show_preview": False,
            },
        }]
    }
    spec = compile_runtime_spec(
        item_id="vision-demo",
        content_mode="single",
        source=source,
        video_url="/media/demo.mp4",
    )
    interaction = spec["video"][0]["interactions"][0]
    assert interaction["pause_video"] is False
    assert interaction["detection"]["response_window_ms"] == 5000
    assert interaction["detection"]["vision"]["target"] == "hand_victory"

    with pytest.raises(RuntimeSpecError, match="requires a vision object"):
        compile_runtime_spec(
            item_id="vision-demo",
            content_mode="single",
            source={"interactions": [{"gesture": "camera_motion", "gate_at_ms": 1}]},
            video_url="/media/demo.mp4",
        )


def test_backfill_is_all_or_nothing(db) -> None:
    legacy_spec = {"schema": "legacy"}
    good = PublishedVideo(
        id="good",
        video_url="/media/good.mp4",
        timeline={"interactions": [{"gesture": "tap", "gate_at_ms": 10}]},
        runtime_spec=legacy_spec,
        runtime_spec_version="legacy",
        version="1",
        content_mode="single",
    )
    bad = PublishedVideo(
        id="bad",
        video_url="/media/bad.mp4",
        timeline={"interactions": [{"gesture": "unknown", "gate_at_ms": 10}]},
        runtime_spec=legacy_spec,
        runtime_spec_version="legacy",
        version="1",
        content_mode="single",
    )
    db.add_all([good, bad])
    db.commit()
    report = compile_all_runtime_specs(db, apply=True)
    assert report.updated == 0
    assert [failure.video_id for failure in report.failures] == ["bad"]
    db.refresh(good)
    assert good.runtime_spec == legacy_spec
    assert good.runtime_spec_version == "legacy"
