"""Closed creator-facing interaction presets.

Runtime interaction types stay stable. Presets expose the semantic choices an
author can make when one type requires a target or direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.camera_continuous_targets import CAMERA_CONTINUOUS_TARGETS
from app.protocol_video import creator_supported_gestures
from app.public_copy import interaction_instruction
from app.vision_targets import VISION_TARGETS, creator_vision_config

SUSTAINED_INTERACTION_TYPES = frozenset({
    "continuous_swipe",
    "continuous_tap",
    "camera_continuous",
    "mic_blow_continuous",
    "mic_level_continuous",
})

PARAMETERIZED_INTERACTION_TYPES = frozenset({
    "pinch",
    "rotate",
    "camera_motion",
    "camera_continuous",
})


@dataclass(frozen=True)
class CreatorInteractionPreset:
    id: str
    type: str
    label: str
    instruction: str
    category: str
    family: str
    variant_group: str | None = None
    lifecycle: str = "discrete"
    story_enabled: bool = True
    minimum_runtime_version: str = "1.1"
    recommended: bool = False
    pinch_direction: str | None = None
    rotation_direction: str | None = None
    vision_target: str | None = None

    @property
    def capability(self) -> str:
        return {
            "screen": "touch",
            "camera": "vision",
            "device_motion": "device_motion",
            "microphone": "microphone",
        }[self.category]

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "instruction": self.instruction,
            "category": self.category,
            "family": self.family,
            "variant_group": self.variant_group,
            "lifecycle": self.lifecycle,
            "capability": self.capability,
            "story_enabled": self.story_enabled,
            "minimum_runtime_version": self.minimum_runtime_version,
            "recommended": self.recommended,
        }


_SCREEN_FAMILIES = {
    "tap": "tap",
    "double_tap": "tap",
    "rapid_tap": "tap",
    "continuous_tap": "continuous_screen",
    "hold": "hold",
    "hold_charge": "hold",
    "swipe_left": "swipe",
    "swipe_right": "swipe",
    "swipe_up": "swipe",
    "swipe_down": "swipe",
    "continuous_swipe": "continuous_screen",
    "drag_left": "drag",
    "drag_right": "drag",
    "drag_up": "drag",
    "drag_down": "drag",
    "scrub_left": "scrub",
    "scrub_right": "scrub",
    "scrub_up": "scrub",
    "scrub_down": "scrub",
    "draw_circle": "draw",
    "erase": "draw",
}

_DEVICE_FAMILIES = {
    "hold_still": "stability",
    "tilt_left": "tilt",
    "tilt_right": "tilt",
    "tilt_forward": "tilt",
    "tilt_backward": "tilt",
    "shake": "shake",
}

_MICROPHONE_FAMILIES = {
    "mic_level": "sound",
    "mic_level_continuous": "continuous_microphone",
    "mic_blow": "breath",
    "mic_blow_continuous": "continuous_microphone",
    "mic_clap": "clap",
    "mic_quiet": "quiet",
}

_LABELS = {
    "tap": "Tap",
    "double_tap": "Double tap",
    "rapid_tap": "Rapid tap",
    "continuous_tap": "Keep tapping to play",
    "hold": "Press and hold",
    "hold_charge": "Hold to charge",
    "swipe_left": "Swipe left",
    "swipe_right": "Swipe right",
    "swipe_up": "Swipe up",
    "swipe_down": "Swipe down",
    "continuous_swipe": "Swipe back and forth to play",
    "drag_left": "Hold and drag left",
    "drag_right": "Hold and drag right",
    "drag_up": "Hold and drag up",
    "drag_down": "Hold and drag down",
    "scrub_left": "Scrub and finish left",
    "scrub_right": "Scrub and finish right",
    "scrub_up": "Scrub and finish up",
    "scrub_down": "Scrub and finish down",
    "draw_circle": "Draw a circle",
    "erase": "Rub to erase",
    "hold_still": "Keep phone still",
    "tilt_left": "Tilt phone left",
    "tilt_right": "Tilt phone right",
    "tilt_forward": "Tilt phone forward",
    "tilt_backward": "Tilt phone backward",
    "shake": "Shake phone",
    "mic_level": "Make a sound",
    "mic_level_continuous": "Keep vocalizing to play",
    "mic_blow": "Blow once",
    "mic_blow_continuous": "Keep blowing to play",
    "mic_clap": "Clap once",
    "mic_quiet": "Stay quiet",
}

_RECOMMENDED = frozenset({
    "tap",
    "swipe_up",
    "hold",
    "shake",
    "mic_clap",
    "camera_motion.face_smile",
})


def _minimum_runtime_version(interaction_type: str, preset_id: str) -> str:
    if interaction_type in {"tilt_forward", "tilt_backward"}:
        return "1.10"
    if preset_id == "pinch_out":
        return "1.7"
    if interaction_type == "mic_level_continuous":
        return "1.6"
    if interaction_type == "mic_blow_continuous":
        return "1.5"
    if preset_id == "camera_continuous.hand_finger_gun_recoil":
        return "1.4"
    if interaction_type == "camera_continuous":
        return "1.3"
    if interaction_type == "continuous_tap":
        return "1.2"
    return "1.1"


def _simple_preset(interaction_type: str, category: str, family: str) -> CreatorInteractionPreset:
    lifecycle = "sustained" if interaction_type in SUSTAINED_INTERACTION_TYPES else "discrete"
    return CreatorInteractionPreset(
        id=interaction_type,
        type=interaction_type,
        label=_LABELS[interaction_type],
        instruction=interaction_instruction(interaction_type),
        category=category,
        family=family,
        lifecycle=lifecycle,
        story_enabled=lifecycle == "discrete",
        minimum_runtime_version=_minimum_runtime_version(interaction_type, interaction_type),
        recommended=interaction_type in _RECOMMENDED,
    )


def _build_presets() -> tuple[CreatorInteractionPreset, ...]:
    presets: list[CreatorInteractionPreset] = []
    screen_order = (
        "tap", "double_tap", "rapid_tap", "continuous_tap",
        "hold", "hold_charge",
        "swipe_left", "swipe_right", "swipe_up", "swipe_down", "continuous_swipe",
        "drag_left", "drag_right", "drag_up", "drag_down",
        "scrub_left", "scrub_right", "scrub_up", "scrub_down",
        "draw_circle", "erase",
    )
    presets.extend(_simple_preset(value, "screen", _SCREEN_FAMILIES[value]) for value in screen_order)
    presets.extend((
        CreatorInteractionPreset(
            id="pinch_in",
            type="pinch",
            label="Pinch-in",
            instruction="Move two fingers toward each other",
            category="screen",
            family="pinch",
            recommended=False,
            pinch_direction="inward",
        ),
        CreatorInteractionPreset(
            id="pinch_out",
            type="pinch",
            label="Pinch-out",
            instruction="Spread two fingers apart",
            category="screen",
            family="pinch",
            minimum_runtime_version="1.7",
            pinch_direction="outward",
        ),
    ))

    for target, metadata in VISION_TARGETS.items():
        preset_id = f"camera_motion.{target}"
        presets.append(CreatorInteractionPreset(
            id=preset_id,
            type="camera_motion",
            label=str(metadata["label"]),
            instruction=str(metadata["instruction"]),
            category="camera",
            family="camera_motion",
            variant_group=str(metadata["family"]),
            recommended=preset_id in _RECOMMENDED,
            vision_target=target,
        ))
    for target, metadata in CAMERA_CONTINUOUS_TARGETS.items():
        presets.append(CreatorInteractionPreset(
            id=f"camera_continuous.{target}",
            type="camera_continuous",
            label=(
                "Keep snapping fingers to play"
                if target == "hand_finger_snap"
                else "Keep recoiling a finger gun to play"
            ),
            instruction=str(metadata["instruction"]),
            category="camera",
            family="continuous_camera",
            lifecycle="sustained",
            story_enabled=False,
            minimum_runtime_version=_minimum_runtime_version(
                "camera_continuous", f"camera_continuous.{target}",
            ),
            vision_target=target,
        ))

    for interaction_type in (
        "hold_still", "tilt_left", "tilt_right", "tilt_forward", "tilt_backward", "shake",
    ):
        presets.append(_simple_preset(
            interaction_type, "device_motion", _DEVICE_FAMILIES[interaction_type],
        ))
    presets.extend((
        CreatorInteractionPreset(
            id="rotate_clockwise",
            type="rotate",
            label="Rotate phone clockwise",
            instruction="Rotate your phone clockwise",
            category="device_motion",
            family="rotate",
            rotation_direction="clockwise",
        ),
        CreatorInteractionPreset(
            id="rotate_counterclockwise",
            type="rotate",
            label="Rotate phone counterclockwise",
            instruction="Rotate your phone counterclockwise",
            category="device_motion",
            family="rotate",
            rotation_direction="counterclockwise",
        ),
    ))

    microphone_order = (
        "mic_level", "mic_level_continuous", "mic_blow", "mic_blow_continuous",
        "mic_clap", "mic_quiet",
    )
    presets.extend(
        _simple_preset(value, "microphone", _MICROPHONE_FAMILIES[value])
        for value in microphone_order
    )
    return tuple(presets)


CREATOR_INTERACTION_PRESETS = _build_presets()
CREATOR_INTERACTION_PRESETS_BY_ID = {
    preset.id: preset for preset in CREATOR_INTERACTION_PRESETS
}

_LEGACY_DEFAULT_PRESET_BY_TYPE = {
    "pinch": "pinch_in",
    "rotate": "rotate_counterclockwise",
    "camera_motion": "camera_motion.hand_open_palm",
    "camera_continuous": "camera_continuous.hand_finger_snap",
}


def creator_interaction_presets() -> tuple[CreatorInteractionPreset, ...]:
    return CREATOR_INTERACTION_PRESETS


def preset_by_id(preset_id: str) -> CreatorInteractionPreset:
    try:
        return CREATOR_INTERACTION_PRESETS_BY_ID[preset_id]
    except KeyError as exc:
        raise ValueError(f"unsupported creator interaction preset: {preset_id!r}") from exc


def resolve_interaction_preset(value: dict[str, Any]) -> CreatorInteractionPreset:
    interaction_type = value.get("type") or value.get("interaction_type")
    preset_id = value.get("preset_id") or value.get("interaction_preset_id")
    if preset_id is not None:
        if not isinstance(preset_id, str):
            raise ValueError("interaction preset id must be a string")
        preset = preset_by_id(preset_id)
        if interaction_type is not None and interaction_type != preset.type:
            raise ValueError("interaction preset does not match interaction type")
        for field, expected in (
            ("pinch_direction", preset.pinch_direction),
            ("rotation_direction", preset.rotation_direction),
            ("vision_target", preset.vision_target),
        ):
            supplied = value.get(field)
            if supplied is not None and supplied != expected:
                raise ValueError(f"{field} does not match interaction preset")
        return preset
    if (
        not isinstance(interaction_type, str)
        or interaction_type not in creator_supported_gestures()
    ):
        raise ValueError("unsupported interaction type")
    if interaction_type == "pinch":
        if value.get("rotation_direction") is not None or value.get("vision_target") is not None:
            raise ValueError("pinch only accepts pinch_direction")
        direction = value.get("pinch_direction") or "inward"
        if direction not in {"inward", "outward"}:
            raise ValueError("pinch_direction must be inward or outward")
        return preset_by_id("pinch_out" if direction == "outward" else "pinch_in")
    if interaction_type == "rotate":
        if value.get("pinch_direction") is not None or value.get("vision_target") is not None:
            raise ValueError("rotate only accepts rotation_direction")
        direction = value.get("rotation_direction") or "counterclockwise"
        if direction not in {"clockwise", "counterclockwise"}:
            raise ValueError("rotation_direction must be clockwise or counterclockwise")
        return preset_by_id(f"rotate_{direction}")
    if interaction_type in {"camera_motion", "camera_continuous"}:
        if value.get("pinch_direction") is not None or value.get("rotation_direction") is not None:
            raise ValueError("camera interaction only accepts vision_target")
        target = value.get("vision_target")
        if target is None:
            return preset_by_id(_LEGACY_DEFAULT_PRESET_BY_TYPE[interaction_type])
        if not isinstance(target, str):
            raise ValueError("vision_target must be a string")
        return preset_by_id(f"{interaction_type}.{target}")
    if any(value.get(field) is not None for field in (
        "pinch_direction", "rotation_direction", "vision_target",
    )):
        raise ValueError("interaction parameters do not match interaction type")
    return preset_by_id(interaction_type)


def preset_id_for_interaction(value: dict[str, Any]) -> str:
    interaction_type = value.get("gesture") or value.get("type")
    detection = value.get("detection") if isinstance(value.get("detection"), dict) else {}
    if interaction_type == "pinch":
        direction = value.get("pinch_direction") or detection.get("pinch_direction") or "inward"
        return "pinch_out" if direction == "outward" else "pinch_in"
    if interaction_type == "rotate":
        direction = value.get("rotation_direction") or detection.get("rotation_direction") or "counterclockwise"
        return f"rotate_{direction}"
    if interaction_type in {"camera_motion", "camera_continuous"}:
        vision = value.get("vision") if isinstance(value.get("vision"), dict) else detection.get("vision")
        target = vision.get("target") if isinstance(vision, dict) else None
        if not isinstance(target, str):
            return _LEGACY_DEFAULT_PRESET_BY_TYPE[interaction_type]
        preset_id = f"{interaction_type}.{target}"
        return preset_id if preset_id in CREATOR_INTERACTION_PRESETS_BY_ID else _LEGACY_DEFAULT_PRESET_BY_TYPE[interaction_type]
    return str(interaction_type)


def apply_preset_fields(item: dict[str, Any], preset: CreatorInteractionPreset) -> None:
    item.pop("vision", None)
    item.pop("rotation_direction", None)
    item.pop("pinch_direction", None)
    if preset.pinch_direction is not None:
        item["pinch_direction"] = preset.pinch_direction
    if preset.rotation_direction is not None:
        item["rotation_direction"] = preset.rotation_direction
    if preset.vision_target is not None:
        item["vision"] = (
            creator_vision_config(preset.vision_target)
            if preset.type == "camera_motion"
            else {"target": preset.vision_target}
        )


assert len(CREATOR_INTERACTION_PRESETS) == 55
assert len(CREATOR_INTERACTION_PRESETS_BY_ID) == len(CREATOR_INTERACTION_PRESETS)
assert {preset.type for preset in CREATOR_INTERACTION_PRESETS} == creator_supported_gestures()
