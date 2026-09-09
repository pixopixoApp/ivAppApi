"""Closed registry for sustained on-device camera interaction targets."""

from __future__ import annotations

from typing import Any


class CameraContinuousTargetError(ValueError):
    """Authored sustained-camera configuration is invalid."""


CAMERA_CONTINUOUS_TARGETS: dict[str, dict[str, Any]] = {
    "hand_finger_snap": {
        "instruction": "Keep snapping your thumb and middle finger to play",
        "camera_facing": "front",
        "show_preview": True,
        "idle_timeout_ms": 1100,
        "signal_kind": "pulse",
        "detector_profile": "finger_snap_v1",
    },
    "hand_finger_gun_recoil": {
        "instruction": "Hold a finger-gun pose and keep recoiling to play",
        "camera_facing": "front",
        "show_preview": True,
        "idle_timeout_ms": 1100,
        "signal_kind": "pulse",
        "detector_profile": "finger_gun_recoil_v1",
    },
}
CAMERA_CONTINUOUS_REGISTRY_VERSION = "v1"


def supported_camera_continuous_targets() -> frozenset[str]:
    return frozenset(CAMERA_CONTINUOUS_TARGETS)


def normalize_camera_continuous_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CameraContinuousTargetError(
            "camera_continuous requires a vision object"
        )
    target = value.get("target")
    if not isinstance(target, str) or target not in CAMERA_CONTINUOUS_TARGETS:
        raise CameraContinuousTargetError(
            "camera_continuous vision.target is unsupported"
        )
    registry_version = value.get(
        "registry_version",
        CAMERA_CONTINUOUS_REGISTRY_VERSION,
    )
    if registry_version != CAMERA_CONTINUOUS_REGISTRY_VERSION:
        raise CameraContinuousTargetError(
            "camera_continuous vision.registry_version must be v1"
        )
    source = CAMERA_CONTINUOUS_TARGETS[target]
    facing = value.get("camera_facing", source["camera_facing"])
    if facing not in {"front", "back"}:
        raise CameraContinuousTargetError(
            "camera_continuous vision.camera_facing must be front or back"
        )
    show_preview = value.get("show_preview", source["show_preview"])
    if not isinstance(show_preview, bool):
        raise CameraContinuousTargetError(
            "camera_continuous vision.show_preview must be boolean"
        )
    signal_kind = value.get("signal_kind", source["signal_kind"])
    if signal_kind != source["signal_kind"]:
        raise CameraContinuousTargetError(
            "camera_continuous vision.signal_kind does not match target"
        )
    detector_profile = value.get("detector_profile", source["detector_profile"])
    if detector_profile != source["detector_profile"]:
        raise CameraContinuousTargetError(
            "camera_continuous vision.detector_profile does not match target"
        )
    return {
        "registry_version": CAMERA_CONTINUOUS_REGISTRY_VERSION,
        "target": target,
        "camera_facing": facing,
        "show_preview": show_preview,
        "signal_kind": signal_kind,
        "detector_profile": detector_profile,
    }


def canonical_camera_continuous_instruction(target: str) -> str:
    try:
        return str(CAMERA_CONTINUOUS_TARGETS[target]["instruction"])
    except KeyError as exc:
        raise CameraContinuousTargetError(
            f"unsupported camera_continuous target: {target!r}"
        ) from exc
