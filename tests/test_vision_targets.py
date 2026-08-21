import pytest

from app.vision_targets import VisionTargetError, normalize_vision_config


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


@pytest.mark.parametrize("target", ["tongue_out", "hand_wave", "face_raw_52"])
def test_unknown_or_unverified_target_is_rejected(target: str) -> None:
    with pytest.raises(VisionTargetError, match="unsupported vision target"):
        normalize_vision_config({"target": target})
