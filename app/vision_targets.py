"""Stable, product-facing targets for on-device visual interaction.

The runtime intentionally exposes a small semantic vocabulary instead of raw
MediaPipe categories or face landmarks.  This keeps authored content portable
between Android and a future trusted HTML bridge, and lets us disable an
underperforming target without changing the wire protocol.
"""

from __future__ import annotations

from typing import Any

VISION_REGISTRY_VERSION = "v1"

# Targets are deliberately product semantics, not raw model blendshape names.
# Left/right always mean the participant's own left/right, not screen position.
VISION_TARGETS: dict[str, dict[str, Any]] = {
    "hand_victory": {"family": "hand", "label": "比耶", "camera_facing": "front"},
    "hand_thumb_up": {"family": "hand", "label": "点赞", "camera_facing": "front"},
    "hand_thumb_down": {"family": "hand", "label": "踩", "camera_facing": "front"},
    "hand_open_palm": {"family": "hand", "label": "张开手掌", "camera_facing": "front"},
    "hand_closed_fist": {"family": "hand", "label": "握拳", "camera_facing": "front"},
    "hand_pointing_up": {"family": "hand", "label": "食指向上", "camera_facing": "front"},
    "hand_i_love_you": {"family": "hand", "label": "我爱你手势", "camera_facing": "front"},
    "face_smile": {"family": "face", "label": "微笑", "camera_facing": "front"},
    "face_wink_left": {"family": "face", "label": "左眼眨眼（以本人为准）", "camera_facing": "front"},
    "face_wink_right": {"family": "face", "label": "右眼眨眼（以本人为准）", "camera_facing": "front"},
    "face_blink": {"family": "face", "label": "双眼眨眼", "camera_facing": "front"},
    "face_mouth_open": {"family": "face", "label": "张嘴", "camera_facing": "front"},
    "face_mouth_pucker": {"family": "face", "label": "嘟嘴", "camera_facing": "front"},
    "face_brow_raise": {"family": "face", "label": "挑眉", "camera_facing": "front"},
    "face_brow_furrow": {"family": "face", "label": "皱眉", "camera_facing": "front"},
    "face_cheek_puff": {"family": "face", "label": "鼓腮", "camera_facing": "front"},
}

_DEFAULT_CONFIDENCE = {"hand": 0.82, "face": 0.72}


class VisionTargetError(ValueError):
    """Raised when authored visual interaction configuration is unsafe."""


def supported_vision_targets() -> frozenset[str]:
    return frozenset(VISION_TARGETS)


def normalize_vision_config(value: Any) -> dict[str, Any]:
    """Validate author input and emit the immutable Runtime `detection.vision` object."""
    if not isinstance(value, dict):
        raise VisionTargetError("camera_motion requires a vision object")
    registry_version = str(value.get("registry_version") or VISION_REGISTRY_VERSION).strip()
    if registry_version != VISION_REGISTRY_VERSION:
        raise VisionTargetError(f"unsupported vision registry version: {registry_version!r}")
    target = value.get("target")
    if not isinstance(target, str) or target.strip() not in VISION_TARGETS:
        raise VisionTargetError(f"unsupported vision target: {target!r}")
    target = target.strip()
    family = str(VISION_TARGETS[target]["family"])
    facing = str(value.get("camera_facing") or VISION_TARGETS[target]["camera_facing"]).strip().lower()
    if facing not in {"front", "back"}:
        raise VisionTargetError("vision.camera_facing must be front or back")
    show_preview = value.get("show_preview", False)
    if not isinstance(show_preview, bool):
        raise VisionTargetError("vision.show_preview must be boolean")
    confidence = value.get("min_confidence", _DEFAULT_CONFIDENCE[family])
    if isinstance(confidence, bool) or not isinstance(confidence, (float, int)):
        raise VisionTargetError("vision.min_confidence must be a number")
    confidence = float(confidence)
    if not 0.5 <= confidence <= 0.99:
        raise VisionTargetError("vision.min_confidence must be between 0.5 and 0.99")
    stable_for_ms = value.get("stable_for_ms", 400)
    if isinstance(stable_for_ms, bool) or not isinstance(stable_for_ms, int):
        raise VisionTargetError("vision.stable_for_ms must be an integer")
    if not 150 <= stable_for_ms <= 3000:
        raise VisionTargetError("vision.stable_for_ms must be between 150 and 3000")
    return {
        "registry_version": VISION_REGISTRY_VERSION,
        "target": target,
        "camera_facing": facing,
        "show_preview": show_preview,
        "min_confidence": confidence,
        "stable_for_ms": stable_for_ms,
    }
