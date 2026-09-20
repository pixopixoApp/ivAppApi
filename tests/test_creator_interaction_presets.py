import pytest

from app.camera_continuous_targets import supported_camera_continuous_targets
from app.creator_interaction_presets import (
    apply_preset_fields,
    creator_interaction_presets,
    preset_id_for_interaction,
    resolve_interaction_preset,
)
from app.protocol_video import creator_supported_gestures
from app.vision_targets import supported_vision_targets


def test_catalog_is_complete_unique_and_story_safe():
    presets = creator_interaction_presets()
    assert len(presets) == 55
    assert len({preset.id for preset in presets}) == 55
    assert {preset.type for preset in presets} == creator_supported_gestures()
    assert "continuous_hold" not in creator_supported_gestures()
    assert "multi_tap" not in creator_supported_gestures()
    assert sum(preset.story_enabled for preset in presets) == 49
    assert {preset.id for preset in presets if preset.lifecycle == "sustained"} == {
        "continuous_tap",
        "continuous_swipe",
        "camera_continuous.hand_finger_snap",
        "camera_continuous.hand_finger_gun_recoil",
        "mic_level_continuous",
        "mic_blow_continuous",
    }
    assert all(
        not preset.story_enabled
        for preset in presets
        if preset.lifecycle == "sustained"
    )
    assert {preset.vision_target for preset in presets if preset.type == "camera_motion"} == (
        supported_vision_targets()
    )
    assert {preset.vision_target for preset in presets if preset.type == "camera_continuous"} == (
        supported_camera_continuous_targets()
    )
    by_type = {}
    for preset in presets:
        by_type.setdefault(preset.type, []).append(preset)
    assert len(by_type) == 37
    assert len(by_type["camera_motion"]) == 16
    assert sum(preset.variant_group == "hand" for preset in by_type["camera_motion"]) == 7
    assert sum(preset.variant_group == "face" for preset in by_type["camera_motion"]) == 9
    assert len(by_type["camera_continuous"]) == 2
    assert {preset.id for preset in by_type["pinch"]} == {
        "pinch_in", "pinch_out",
    }
    assert {preset.id for preset in by_type["rotate"]} == {
        "rotate_clockwise", "rotate_counterclockwise",
    }
    assert all(
        len(items) == 1
        for interaction_type, items in by_type.items()
        if interaction_type not in {"pinch", "rotate", "camera_motion", "camera_continuous"}
    )


@pytest.mark.parametrize("interaction_type", ["continuous_hold", "multi_tap"])
def test_operator_only_types_are_rejected_by_creator_presets(interaction_type: str):
    with pytest.raises(ValueError, match="unsupported interaction type"):
        resolve_interaction_preset({"type": interaction_type})


def test_legacy_and_preset_inputs_resolve_to_the_same_semantics():
    cases = (
        ({"type": "pinch", "pinch_direction": "outward"}, "pinch_out"),
        ({"type": "rotate", "rotation_direction": "clockwise"}, "rotate_clockwise"),
        ({"type": "camera_motion", "vision_target": "face_smile"}, "camera_motion.face_smile"),
        ({"type": "camera_continuous", "vision_target": "hand_finger_snap"},
         "camera_continuous.hand_finger_snap"),
    )
    for legacy, preset_id in cases:
        assert resolve_interaction_preset(legacy).id == preset_id
        assert resolve_interaction_preset({**legacy, "preset_id": preset_id}).id == preset_id


def test_preset_fields_round_trip_through_source_and_runtime_shapes():
    for preset in creator_interaction_presets():
        source = {"gesture": preset.type}
        apply_preset_fields(source, preset)
        assert preset_id_for_interaction(source) == preset.id
        runtime = {
            "type": preset.type,
            "detection": {
                "pinch_direction": preset.pinch_direction,
                "rotation_direction": preset.rotation_direction,
                "vision": {"target": preset.vision_target} if preset.vision_target else None,
            },
        }
        assert preset_id_for_interaction(runtime) == preset.id
