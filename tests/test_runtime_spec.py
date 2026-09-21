from __future__ import annotations

import copy

import pytest

from app.models import CreatorCreation, CreatorVersion, PublishedVideo
from app.protocol_video import (
    RUNTIME_SPEC_VERSION,
    SUPPORTED_RUNTIME_SPEC_VERSIONS,
    RuntimeSpecError,
    compile_runtime_spec,
    mark_user_relative_tilt_semantics,
    read_runtime_spec,
    upgrade_tilt_semantics,
)
from app.runtime_backfill import (
    backfill_known_rotation_directions,
    compile_all_runtime_specs,
)


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


@pytest.mark.parametrize("gesture", ["rotate", "draw_circle"])
def test_rotation_interactions_compile_selected_direction_and_reject_invalid_value(
    gesture: str,
) -> None:
    source = {
        "interactions": [
            {
                "gesture": gesture,
                "gate_at_ms": 1000,
                "rotation_direction": "clockwise",
            }
        ]
    }
    spec = compile_runtime_spec(
        item_id=f"{gesture}-demo",
        content_mode="single",
        source=source,
        video_url=f"/media/{gesture}-demo.mp4",
    )
    assert spec["video"][0]["interactions"][0]["detection"]["rotation_direction"] == (
        "clockwise"
    )
    with pytest.raises(RuntimeSpecError, match="rotation_direction"):
        compile_runtime_spec(
            item_id=f"invalid-{gesture}",
            content_mode="single",
            source={
                "interactions": [
                    {
                        "gesture": gesture,
                        "gate_at_ms": 1000,
                        "rotation_direction": "sideways",
                    }
                ]
            },
            video_url=f"/media/invalid-{gesture}.mp4",
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

    assert spec["version"] == "1.2"
    assert RUNTIME_SPEC_VERSION == "1.10"
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
    ("configured_end", "expected_end"),
    [(3000, 3000), (8000, 5000)],
)
def test_explicit_sustained_end_is_compiled_and_clipped(
    configured_end: int,
    expected_end: int,
) -> None:
    spec = compile_runtime_spec(
        item_id="bounded-continuous",
        content_mode="single",
        source={
            "media": {"duration_ms": 10_000},
            "interactions": [
                {
                    "gesture": "continuous_tap",
                    "gate_at_ms": 0,
                    "gate_end_ms": configured_end,
                },
                {"gesture": "tap", "gate_at_ms": 5000},
            ],
        },
        video_url="/media/bounded-continuous.mp4",
    )

    assert spec["version"] == "1.8"
    interaction = spec["video"][0]["interactions"][0]
    assert interaction["active_until_ms"] == expected_end
    assert interaction["detection"]["response_window_ms"] == 0
    assert read_runtime_spec(
        spec,
        item_id="bounded-continuous",
        version="1.8",
    )[0].interactions[0].active_until_ms == expected_end


def test_sustained_end_must_be_later_than_start() -> None:
    with pytest.raises(RuntimeSpecError, match="greater than gate_at_ms"):
        compile_runtime_spec(
            item_id="invalid-bounded-continuous",
            content_mode="single",
            source={
                "media": {"duration_ms": 10_000},
                "interactions": [
                    {
                        "gesture": "continuous_swipe",
                        "gate_at_ms": 1000,
                        "gate_end_ms": 1000,
                    }
                ],
            },
            video_url="/media/invalid-bounded-continuous.mp4",
        )


def test_continuous_hold_and_multi_tap_compile_as_v18() -> None:
    spec = compile_runtime_spec(
        item_id="new-touch-controls",
        content_mode="single",
        source={
            "media": {"duration_ms": 10_000},
            "interactions": [
                {
                    "gesture": "continuous_hold",
                    "gate_at_ms": 0,
                    "gate_end_ms": 3000,
                },
                {
                    "gesture": "multi_tap",
                    "gate_at_ms": 5000,
                    "tap_count": 99,
                },
            ],
        },
        video_url="/media/new-touch-controls.mp4",
    )

    assert spec["version"] == "1.8"
    hold, multi_tap = spec["video"][0]["interactions"]
    assert hold["type"] == "continuous_hold"
    assert hold["active_until_ms"] == 3000
    assert hold["description"] == "Press and hold to play"
    assert multi_tap["type"] == "multi_tap"
    assert multi_tap["description"] == "Tap 99 times"
    assert multi_tap["detection"]["required_tap_count"] == 99


@pytest.mark.parametrize("gesture", ["tilt_forward", "tilt_backward"])
def test_depth_tilt_interactions_preserve_v19_and_compile_user_relative_v110(
    gesture: str,
) -> None:
    spec = compile_runtime_spec(
        item_id=f"{gesture}-demo",
        content_mode="single",
        source={
            "media": {"duration_ms": 5_000},
            "interactions": [{"gesture": gesture, "gate_at_ms": 1_000}],
        },
        video_url=f"/media/{gesture}-demo.mp4",
    )

    assert spec["version"] == "1.9"
    assert spec["video"][0]["interactions"][0]["type"] == gesture
    assert read_runtime_spec(
        spec,
        item_id=f"{gesture}-demo",
        version="1.9",
    )[0].interactions[0].type == gesture

    corrected = compile_runtime_spec(
        item_id=f"{gesture}-corrected-demo",
        content_mode="single",
        source={
            "tilt_semantics": "user_relative_v2",
            "media": {"duration_ms": 5_000},
            "interactions": [{"gesture": gesture, "gate_at_ms": 1_000}],
        },
        video_url=f"/media/{gesture}-corrected-demo.mp4",
    )
    assert corrected["version"] == "1.10"
    assert read_runtime_spec(
        corrected,
        item_id=f"{gesture}-corrected-demo",
        version="1.10",
    )[0].interactions[0].type == gesture

    downgraded = copy.deepcopy(spec)
    downgraded["version"] = "1.8"
    with pytest.raises(RuntimeSpecError, match="requires runtime spec version 1.9 or later"):
        read_runtime_spec(
            downgraded,
            item_id=f"{gesture}-demo",
            version="1.8",
        )


def test_pitch_source_upgrade_swaps_legacy_names_once_for_single_and_story() -> None:
    legacy = {
        "interactions": [
            {"gesture": "tilt_backward", "gate_at_ms": 1_000},
            {"gesture": "tilt_forward", "gate_at_ms": 2_000},
        ]
    }
    upgraded = upgrade_tilt_semantics(legacy)
    assert [item["gesture"] for item in upgraded["interactions"]] == [
        "tilt_forward",
        "tilt_backward",
    ]
    assert upgraded["tilt_semantics"] == "user_relative_v2"
    assert upgrade_tilt_semantics(upgraded) == upgraded
    assert legacy["interactions"][0]["gesture"] == "tilt_backward"

    story = upgrade_tilt_semantics({
        "entry_clip_id": "A",
        "clips": {"A": {"timeline": legacy}},
    })
    assert story["clips"]["A"]["timeline"]["tilt_semantics"] == "user_relative_v2"
    assert story["clips"]["A"]["timeline"]["interactions"][0]["gesture"] == "tilt_forward"

    newly_authored = mark_user_relative_tilt_semantics(legacy)
    assert newly_authored["tilt_semantics"] == "user_relative_v2"
    assert newly_authored["interactions"][0]["gesture"] == "tilt_backward"


def test_legacy_pitch_version_keeps_all_v18_features_readable() -> None:
    spec = compile_runtime_spec(
        item_id="legacy-pitch-with-range",
        content_mode="single",
        source={
            "media": {"duration_ms": 2_000},
            "interactions": [
                {"gesture": "continuous_hold", "gate_at_ms": 0, "gate_end_ms": 500},
                {"gesture": "tilt_forward", "gate_at_ms": 1_000},
            ],
        },
        video_url="/media/legacy-pitch-with-range.mp4",
    )
    assert spec["version"] == "1.9"
    assert [item.type for item in read_runtime_spec(
        spec,
        item_id="legacy-pitch-with-range",
        version="1.9",
    )[0].interactions] == ["continuous_hold", "tilt_forward"]


@pytest.mark.parametrize("tap_count", [0, 100, 1.5, True, None])
def test_multi_tap_rejects_invalid_counts(tap_count: object) -> None:
    with pytest.raises(RuntimeSpecError, match="tap_count"):
        compile_runtime_spec(
            item_id="invalid-multi-tap",
            content_mode="single",
            source={
                "interactions": [
                    {
                        "gesture": "multi_tap",
                        "gate_at_ms": 1000,
                        "tap_count": tap_count,
                    }
                ]
            },
            video_url="/media/invalid-multi-tap.mp4",
        )


def test_continuous_blow_compiles_as_v15_with_fixed_audio_lease() -> None:
    source = {
        "media": {"duration_ms": 10_000},
        "interactions": [
            {"gesture": "mic_blow_continuous", "gate_at_ms": 1000},
            {"gesture": "tap", "gate_at_ms": 6000},
        ],
    }
    spec = compile_runtime_spec(
        item_id="continuous-blow-demo",
        content_mode="single",
        source=source,
        video_url="/media/continuous-blow-demo.mp4",
    )
    interaction = spec["video"][0]["interactions"][0]

    assert spec["version"] == "1.5"
    assert interaction == {
        "id": "action_001",
        "type": "mic_blow_continuous",
        "description": "Keep blowing at the target volume to play",
        "offset_time_ms": 1000,
        "pause_video": True,
        "detection": {
            "confidence_threshold": 0.85,
            "response_window_ms": 0,
            "place": "middle_bottom",
            "min_duration_ms": 160,
            "min_volume_score": 55,
            "idle_timeout_ms": 450,
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
        item_id="continuous-blow-demo",
        version="1.5",
    )[0].interactions[0].type == "mic_blow_continuous"

    downgraded = copy.deepcopy(spec)
    downgraded["version"] = "1.4"
    with pytest.raises(RuntimeSpecError, match="requires runtime spec version 1.5"):
        read_runtime_spec(
            downgraded,
            item_id="continuous-blow-demo",
            version="1.4",
        )


def test_continuous_voice_compiles_as_v16_with_fixed_audio_lease() -> None:
    source = {
        "media": {"duration_ms": 10_000},
        "interactions": [
            {"gesture": "mic_level_continuous", "gate_at_ms": 1000},
            {"gesture": "tap", "gate_at_ms": 6000},
        ],
    }
    spec = compile_runtime_spec(
        item_id="continuous-voice-demo",
        content_mode="single",
        source=source,
        video_url="/media/continuous-voice-demo.mp4",
    )
    interaction = spec["video"][0]["interactions"][0]

    assert spec["version"] == "1.6"
    assert interaction == {
        "id": "action_001",
        "type": "mic_level_continuous",
        "description": "Keep your voice in the target pitch range",
        "offset_time_ms": 1000,
        "pause_video": True,
        "detection": {
            "confidence_threshold": 0.85,
            "response_window_ms": 0,
            "place": "middle_bottom",
            "min_duration_ms": 160,
            "min_volume_score": 45,
            "idle_timeout_ms": 450,
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
        item_id="continuous-voice-demo",
        version="1.6",
    )[0].interactions[0].type == "mic_level_continuous"

    downgraded = copy.deepcopy(spec)
    downgraded["version"] = "1.5"
    with pytest.raises(RuntimeSpecError, match="requires runtime spec version 1.6"):
        read_runtime_spec(
            downgraded,
            item_id="continuous-voice-demo",
            version="1.5",
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
    assert SUPPORTED_RUNTIME_SPEC_VERSIONS == frozenset(
        {"1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "1.9", "1.10"}
    )
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


def test_camera_continuous_compiles_as_v13_finger_snap_lease() -> None:
    source = {
        "media": {"duration_ms": 10_000},
        "interactions": [
            {
                "gesture": "camera_continuous",
                "gate_at_ms": 1000,
                "vision": {
                    "target": "hand_finger_snap",
                    "camera_facing": "front",
                    "show_preview": True,
                },
            },
            {"gesture": "tap", "gate_at_ms": 6000},
        ],
    }
    spec = compile_runtime_spec(
        item_id="snap-demo",
        content_mode="single",
        source=source,
        video_url="/media/snap-demo.mp4",
    )
    interaction = spec["video"][0]["interactions"][0]

    assert spec["version"] == "1.3"
    assert interaction["type"] == "camera_continuous"
    assert interaction["pause_video"] is True
    assert interaction["detection"] == {
        "confidence_threshold": 0.85,
        "response_window_ms": 0,
        "place": "middle_bottom",
        "idle_timeout_ms": 1100,
        "vision": {
            "registry_version": "v1",
            "target": "hand_finger_snap",
            "camera_facing": "front",
            "show_preview": True,
            "signal_kind": "pulse",
            "detector_profile": "finger_snap_v1",
        },
    }
    assert read_runtime_spec(
        spec,
        item_id="snap-demo",
        version="1.3",
    )[0].interactions[0].type == "camera_continuous"

    downgraded = copy.deepcopy(spec)
    downgraded["version"] = "1.2"
    with pytest.raises(RuntimeSpecError, match="version 1.3 or later"):
        read_runtime_spec(downgraded, item_id="snap-demo", version="1.2")

    source["interactions"][0]["vision"]["target"] = "hand_open_palm"
    with pytest.raises(RuntimeSpecError, match="target is unsupported"):
        compile_runtime_spec(
            item_id="snap-demo",
            content_mode="single",
            source=source,
            video_url="/media/snap-demo.mp4",
        )


def test_finger_gun_recoil_compiles_as_v14_camera_continuous_target() -> None:
    source = {
        "media": {"duration_ms": 10_000},
        "interactions": [{
            "gesture": "camera_continuous",
            "gate_at_ms": 1000,
            "vision": {"target": "hand_finger_gun_recoil"},
        }],
    }

    spec = compile_runtime_spec(
        item_id="finger-gun-demo",
        content_mode="single",
        source=source,
        video_url="/media/finger-gun-demo.mp4",
    )
    vision = spec["video"][0]["interactions"][0]["detection"]["vision"]

    assert spec["version"] == "1.4"
    assert vision == {
        "registry_version": "v1",
        "target": "hand_finger_gun_recoil",
        "camera_facing": "front",
        "show_preview": True,
        "signal_kind": "pulse",
        "detector_profile": "finger_gun_recoil_v1",
    }
    assert read_runtime_spec(
        spec,
        item_id="finger-gun-demo",
        version="1.4",
    )[0].interactions[0].type == "camera_continuous"

    downgraded = copy.deepcopy(spec)
    downgraded["version"] = "1.3"
    with pytest.raises(RuntimeSpecError, match="requires runtime spec version 1.4"):
        read_runtime_spec(downgraded, item_id="finger-gun-demo", version="1.3")

    mismatched = copy.deepcopy(spec)
    mismatched["video"][0]["interactions"][0]["detection"]["vision"][
        "detector_profile"
    ] = "finger_snap_v1"
    with pytest.raises(RuntimeSpecError, match="detector_profile does not match"):
        read_runtime_spec(mismatched, item_id="finger-gun-demo", version="1.4")


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


def test_rotation_direction_backfill_updates_source_and_runtime_spec(db, monkeypatch) -> None:
    row = PublishedVideo(
        id="audited-rotate",
        video_url="/media/audited-rotate.mp4",
        timeline={"interactions": [{"gesture": "rotate", "gate_at_ms": 3620}]},
        runtime_spec={"schema": "legacy"},
        runtime_spec_version="legacy",
        version="1",
        content_mode="single",
    )
    db.add(row)
    db.commit()
    monkeypatch.setattr(
        "app.runtime_backfill.KNOWN_ROTATION_DIRECTIONS",
        {"audited-rotate": {3620: "counterclockwise"}},
    )

    report = backfill_known_rotation_directions(db, apply=True)

    assert report.failures == []
    assert report.updated == 1
    db.refresh(row)
    assert row.timeline["interactions"][0]["rotation_direction"] == "counterclockwise"
    detection = row.runtime_spec["video"][0]["interactions"][0]["detection"]
    assert detection["rotation_direction"] == "counterclockwise"


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
