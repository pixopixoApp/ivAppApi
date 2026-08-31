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
    "hand_victory": {"family": "hand", "label": "Victory sign", "instruction": "Show a victory sign to the camera", "camera_facing": "front"},
    "hand_thumb_up": {"family": "hand", "label": "Thumbs up", "instruction": "Give the camera a thumbs up", "camera_facing": "front"},
    "hand_thumb_down": {"family": "hand", "label": "Thumbs down", "instruction": "Give the camera a thumbs down", "camera_facing": "front"},
    "hand_open_palm": {"family": "hand", "label": "Open palm", "instruction": "Show an open palm to the camera", "camera_facing": "front"},
    "hand_closed_fist": {"family": "hand", "label": "Closed fist", "instruction": "Show a closed fist to the camera", "camera_facing": "front"},
    "hand_pointing_up": {"family": "hand", "label": "Pointing up", "instruction": "Point one finger up at the camera", "camera_facing": "front"},
    "hand_i_love_you": {"family": "hand", "label": "I love you sign", "instruction": "Show the I love you sign to the camera", "camera_facing": "front"},
    "face_smile": {"family": "face", "label": "Smile", "instruction": "Smile at the camera", "camera_facing": "front"},
    "face_wink_left": {"family": "face", "label": "Wink left eye", "instruction": "Wink your left eye at the camera", "camera_facing": "front"},
    "face_wink_right": {"family": "face", "label": "Wink right eye", "instruction": "Wink your right eye at the camera", "camera_facing": "front"},
    "face_blink": {"family": "face", "label": "Blink", "instruction": "Blink both eyes at the camera", "camera_facing": "front"},
    "face_mouth_open": {"family": "face", "label": "Open mouth", "instruction": "Open your mouth at the camera", "camera_facing": "front"},
    "face_mouth_pucker": {"family": "face", "label": "Pucker lips", "instruction": "Pucker your lips at the camera", "camera_facing": "front"},
    "face_brow_raise": {"family": "face", "label": "Raise eyebrows", "instruction": "Raise your eyebrows at the camera", "camera_facing": "front"},
    "face_brow_furrow": {"family": "face", "label": "Furrow eyebrows", "instruction": "Furrow your eyebrows at the camera", "camera_facing": "front"},
    "face_cheek_puff": {"family": "face", "label": "Puff cheeks", "instruction": "Puff your cheeks at the camera", "camera_facing": "front"},
}


def canonical_vision_instruction(target: str) -> str:
    try:
        return str(VISION_TARGETS[target]["instruction"])
    except KeyError as exc:
        raise VisionTargetError(f"unsupported vision target: {target!r}") from exc

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
