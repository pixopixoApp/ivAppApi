from __future__ import annotations

import copy

import pytest

from app.models import CreatorCreation, CreatorVersion, PublishedVideo
from app.protocol_video import (
    RUNTIME_SPEC_VERSION,
    SUPPORTED_RUNTIME_SPEC_VERSIONS,
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
        version=spec["version"],
    )[0].interactions[0].type == "mic_blow"


def test_unknown_gesture_fails_closed() -> None:
    with pytest.raises(RuntimeSpecError, match="unsupported gesture"):
        compile_runtime_spec(
            item_id="demo",
            content_mode="single",
            source={"interactions": [{"gesture": "magic", "gate_at_ms": 1}]},
            video_url="/media/demo.mp4",
        )


def test_continuous_swipe_compiles_as_a_sustained_playback_rule() -> None:
    source = {
        "media": {"duration_ms": 10_000},
        "interactions": [
            {
                "gesture": "continuous_swipe",
                "gate_at_ms": 1000,
                "hint": "持续往复滑动以播放",
            },
            {"gesture": "tap", "gate_at_ms": 6000},
        ],
    }
    spec = compile_runtime_spec(
        item_id="continuous-demo",
        content_mode="single",
        source=source,
        video_url="/media/continuous-demo.mp4",
    )
    interaction = spec["video"][0]["interactions"][0]
    assert interaction == {
        "id": "action_001",
        "type": "continuous_swipe",
        "description": "Swipe back and forth to play",
        "offset_time_ms": 1000,
        "pause_video": True,
        "detection": {
            "confidence_threshold": 0.85,
            "response_window_ms": 0,
            "place": "middle_middle",
            "min_travel_dp": 32,
            "idle_timeout_ms": 500,
        },
        "feedback": {
            "animation": "none",
            "animation_duration_ms": 0,
            "vibrate": False,
            "sound_effect": "",
        },
        "on_success": {"action": "continue"},
        "on_miss": {"action": "continue"},
    }
    assert read_runtime_spec(
        spec,
        item_id="continuous-demo",
        version=spec["version"],
    )[0].interactions[0].type == "continuous_swipe"
    legacy_spec = copy.deepcopy(spec)
    legacy_spec["video"][0]["interactions"][0]["detection"]["idle_timeout_ms"] = 180
    assert read_runtime_spec(
        legacy_spec,
        item_id="continuous-demo",
        version=legacy_spec["version"],
    )[0].interactions[0].type == "continuous_swipe"


def test_continuous_tap_alone_upgrades_to_v12_with_fixed_lease() -> None:
    source = {
        "media": {"duration_ms": 10_000},
        "interactions": [
            {"gesture": "continuous_tap", "gate_at_ms": 1000},
            {"gesture": "tap", "gate_at_ms": 6000},
        ],
    }
    spec = compile_runtime_spec(
        item_id="continuous-tap-demo",
        content_mode="single",
        source=source,
        video_url="/media/continuous-tap-demo.mp4",
    )
    interaction = spec["video"][0]["interactions"][0]

    assert spec["version"] == RUNTIME_SPEC_VERSION == "1.2"
    assert interaction["type"] == "continuous_tap"
    assert interaction["description"] == "Keep tapping to play"
    assert interaction["pause_video"] is True
    assert interaction["detection"] == {
        "confidence_threshold": 0.85,
        "response_window_ms": 0,
        "place": "middle_middle",
        "idle_timeout_ms": 500,
    }
    assert interaction["feedback"]["vibrate"] is False
    assert read_runtime_spec(
        spec,
        item_id="continuous-tap-demo",
        version="1.2",
    )[0].interactions[0].type == "continuous_tap"

    downgraded = copy.deepcopy(spec)
    downgraded["version"] = "1.1"
    with pytest.raises(RuntimeSpecError, match="requires runtime spec version 1.2"):
        read_runtime_spec(
            downgraded,
            item_id="continuous-tap-demo",
            version="1.1",
        )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            {"interactions": [{"gesture": "continuous_swipe", "gate_at_ms": 1000}]},
            "media.duration_ms is required",
        ),
        (
            {
                "media": {"duration_ms": 10_000},
                "interactions": [{
                    "gesture": "continuous_swipe",
                    "gate_at_ms": 1000,
                    "pause_video": False,
                }],
            },
            "requires pause_video=true",
        ),
        (
            {
                "media": {"duration_ms": 10_000},
                "interactions": [{
                    "gesture": "continuous_swipe",
                    "gate_at_ms": 1000,
                    "gate_end_ms": 2000,
                }],
            },
            "does not allow gate_end_ms",
        ),
        (
            {
                "media": {"duration_ms": 10_000},
                "interactions": [
                    {"gesture": "continuous_swipe", "gate_at_ms": 1000},
                    {"gesture": "tap", "gate_at_ms": 1000},
                ],
            },
            "must end at a later interaction",
        ),
    ],
)
def test_continuous_swipe_rejects_conflicting_source(
    source: dict,
    message: str,
) -> None:
    with pytest.raises(RuntimeSpecError, match=message):
        compile_runtime_spec(
            item_id="continuous-demo",
            content_mode="single",
            source=source,
            video_url="/media/continuous-demo.mp4",
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
    assert spec["version"] == "1.1"


def test_story_result_end_and_retry_reuse_existing_actions() -> None:
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
    spec = compile_runtime_spec(
        item_id="branch-story",
        content_mode="story",
        source=story,
        video_url="/media/branch-story/a.mp4",
    )
    clips = {clip["video_id"]: clip for clip in spec["video"]}
    assert clips["b"]["on_end"] == {
        "action": "end_experience",
        "timing": "immediate",
    }
    assert clips["c"]["on_end"] == {"action": "retry_previous_point"}
    assert read_runtime_spec(
        spec,
        item_id="branch-story",
        version=spec["version"],
    )


def test_v10_remains_readable_but_cannot_claim_video_on_end() -> None:
    assert SUPPORTED_RUNTIME_SPEC_VERSIONS == frozenset({"1.0", "1.1", "1.2"})
    spec = compile_runtime_spec(
        item_id="legacy",
        content_mode="single",
        source={"interactions": []},
        video_url="/media/legacy.mp4",
    )
    spec["version"] = "1.0"
    assert read_runtime_spec(spec, item_id="legacy", version="1.0")

    spec["video"][0]["on_end"] = {
        "action": "end_experience",
        "timing": "immediate",
    }
    with pytest.raises(RuntimeSpecError, match="requires runtime spec version 1.1"):
        read_runtime_spec(spec, item_id="legacy", version="1.0")


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
    assert read_runtime_spec(broken, item_id="demo", version=broken["version"])


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


def test_backfill_updates_good_rows_and_preserves_bad_rows(db) -> None:
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
    assert report.updated == 1
    assert [failure.video_id for failure in report.failures] == ["bad"]
    db.refresh(good)
    db.refresh(bad)
    assert good.runtime_spec["video"][0]["interactions"][0]["description"] == "Tap"
    assert good.runtime_spec_version != "legacy"
    assert bad.runtime_spec == legacy_spec
    assert bad.runtime_spec_version == "legacy"


def test_backfill_recompiles_creator_versions_and_active_snapshot(db) -> None:
    creation = CreatorCreation(
        id="creator-backfill",
        user_id="creator-user",
        upload_id="upload-backfill",
        status="ready",
        active_version_id="creator-version",
    )
    version = CreatorVersion(
        id="creator-version",
        creation_id=creation.id,
        user_id=creation.user_id,
        number=1,
        request_id="creator-request",
        status="ready",
        source_timeline={
            "media": {"duration_ms": 10_000},
            "interactions": [
                {
                    "gesture": "continuous_swipe",
                    "gate_at_ms": 100,
                    "hint": "持续往复滑动以播放",
                }
            ]
        },
    )
    db.add_all([creation, version])
    db.commit()

    report = compile_all_runtime_specs(db, apply=True)

    assert report.failures == []
    assert report.total == report.compilable == report.updated == 2
    db.refresh(version)
    db.refresh(creation)
    assert version.runtime_spec["video"][0]["interactions"][0]["description"] == (
        "Swipe back and forth to play"
    )
    assert creation.runtime_spec == version.runtime_spec
    assert creation.runtime_spec_version == version.runtime_spec_version
