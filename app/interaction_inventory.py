"""Compact interaction inventory for operations-facing video filters.

The admin UI filters with the same authoring values used by the expert editor.
Parameterized interactions therefore expose both their parent type and their
specific ``type::variant`` value.  A video is counted at most once per value.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

PRESET_SEPARATOR = "::"


def _interaction_rows(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        interactions = value.get("interactions")
        if isinstance(interactions, list):
            for item in interactions:
                if isinstance(item, dict):
                    yield item
        for key, child in value.items():
            if key != "interactions":
                yield from _interaction_rows(child)
    elif isinstance(value, list):
        for child in value:
            yield from _interaction_rows(child)


def _variant_key(gesture: str, interaction: dict[str, Any]) -> str | None:
    if gesture in {"camera_motion", "camera_continuous"}:
        vision = interaction.get("vision")
        target = vision.get("target") if isinstance(vision, dict) else None
        if not isinstance(target, str) or not target.strip():
            target = (
                "hand_finger_snap"
                if gesture == "camera_continuous"
                else "hand_victory"
            )
        return f"{gesture}{PRESET_SEPARATOR}{target.strip()}"
    if gesture == "pinch":
        direction = str(interaction.get("pinch_direction") or "inward")
        if direction not in {"inward", "outward"}:
            direction = "inward"
        return f"{gesture}{PRESET_SEPARATOR}{direction}"
    if gesture in {"rotate", "draw_circle"}:
        direction = str(interaction.get("rotation_direction") or "counterclockwise")
        if direction not in {"clockwise", "counterclockwise"}:
            direction = "counterclockwise"
        return f"{gesture}{PRESET_SEPARATOR}{direction}"
    if gesture == "mic_blow_continuous":
        return f"mic_continuous{PRESET_SEPARATOR}blow_volume"
    if gesture == "mic_level_continuous":
        return f"mic_continuous{PRESET_SEPARATOR}voice_pitch"
    return None


def interaction_filter_keys(timeline: Any) -> list[str]:
    """Return stable expert-editor filter keys found in one video timeline."""
    keys: set[str] = set()
    for interaction in _interaction_rows(timeline):
        gesture = interaction.get("gesture")
        if not isinstance(gesture, str) or not gesture.strip():
            continue
        gesture = gesture.strip()
        if gesture in {"mic_blow_continuous", "mic_level_continuous"}:
            keys.add("mic_continuous")
        else:
            keys.add(gesture)
        variant = _variant_key(gesture, interaction)
        if variant:
            keys.add(variant)
    return sorted(keys)
