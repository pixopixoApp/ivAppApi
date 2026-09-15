import pytest

from app.vision_targets import (
    VisionTargetError,
    creator_vision_config,
    normalize_vision_config,
)


def test_normalize_vision_target_applies_product_defaults() -> None:
    value = normalize_vision_config({"target": "face_smile"})
    assert value == {
        "registry_version": "v1",
        "target": "face_smile",
        "camera_facing": "front",
        "show_preview": False,
        "min_confidence": 0.72,
        "stable_for_ms": 400,
    }


def test_creator_thumb_up_is_visible_and_uses_calibrated_thresholds() -> None:
    value = creator_vision_config("hand_thumb_up")
    assert value == {
        "registry_version": "v1",
        "target": "hand_thumb_up",
        "camera_facing": "front",
        "show_preview": True,
        "min_confidence": 0.60,
        "stable_for_ms": 250,
    }


@pytest.mark.parametrize("target", ["tongue_out", "hand_wave", "face_raw_52"])
def test_unknown_or_unverified_target_is_rejected(target: str) -> None:
    with pytest.raises(VisionTargetError, match="unsupported vision target"):
        normalize_vision_config({"target": target})
