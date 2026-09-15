"""Deterministic creator edits. No model, task queue, or credit operations."""

from copy import deepcopy
from typing import Any

from app.creator_interaction_presets import (
    SUSTAINED_INTERACTION_TYPES,
    apply_preset_fields,
    creator_interaction_presets,
    preset_id_for_interaction,
    resolve_interaction_preset,
)
from app.protocol_video import (
    RuntimeSpecError,
    compile_runtime_spec,
)

SUSTAINED = SUSTAINED_INTERACTION_TYPES


def replace_interactions(source: dict, edits: list[dict[str, str]]) -> dict:
    timeline = deepcopy(source)
    if isinstance(timeline.get("clips"), dict):
        groups: dict[str, list] = {}
        for edit in edits:
            clip_id = edit.get("clip_id") or timeline.get("entry_clip_id")
            try:
                preset = resolve_interaction_preset(edit)
            except ValueError as exc:
                raise RuntimeSpecError(str(exc)) from exc
            if clip_id not in timeline["clips"] or preset.type in SUSTAINED:
                raise RuntimeSpecError("Invalid Story clip or Auto-only interaction")
            groups.setdefault(clip_id, []).append(edit)
        for clip_id, changes in groups.items():
            timeline["clips"][clip_id]["timeline"] = replace_interactions(
                timeline["clips"][clip_id]["timeline"], changes,
            )
        return timeline
    interactions = timeline.get("interactions")
    if not isinstance(interactions, list):
        raise RuntimeSpecError("This version has no editable interaction timeline")
    by_id = {f"action_{index + 1:03d}": item for index, item in enumerate(interactions)}
    seen: set[str] = set()
    for edit in edits:
        action_id = edit["interaction_id"]
        if action_id in seen or action_id not in by_id:
            raise RuntimeSpecError("Unknown or duplicate interaction id")
        seen.add(action_id)
        item = by_id[action_id]
        if (
            edit.get("preset_id") is None
            and item.get("gesture") == edit.get("type")
            and edit.get("type") != "pinch"
            and edit.get("rotation_direction") is None
            and edit.get("vision_target") is None
        ):
            continue
        try:
            preset = resolve_interaction_preset(edit)
        except ValueError as exc:
            raise RuntimeSpecError(str(exc)) from exc
        if preset_id_for_interaction(item) == preset.id:
            continue
        item["gesture"] = preset.type
        # Type-specific fields must not leak from the replaced mechanic.
        apply_preset_fields(item, preset)
        if preset.type in SUSTAINED:
            item["pause_video"] = True
            for field in ("gate_end_ms", "outcomes", "region"):
                item.pop(field, None)
    return timeline


def compile_edits(source: dict, edits: list[dict[str, str]], *, item_id: str, video_url: str,
                  video_urls: dict[str, str] | None = None) -> tuple[dict, dict]:
    timeline = replace_interactions(source, edits)
    runtime = compile_runtime_spec(
        item_id=item_id, content_mode="story" if timeline.get("clips") else "single",
        source=timeline, video_url=video_url, video_urls=video_urls,
    )
    return timeline, runtime


def manual_edit_options(source: dict | None, runtime: dict | None) -> dict[str, Any]:
    """Ship validated replacements with the ready version for offline switching.

    The same compiler is used here and at save, so the client does not maintain
    another set of gesture defaults. Impossible sustained intervals are omitted.
    """
    if not isinstance(source, dict) or not isinstance(runtime, dict):
        return {}
    clips = runtime.get("video")
    if not isinstance(clips, list) or not clips:
        return {}
    options: dict[str, Any] = {}
    story = bool(source.get("clips"))
    urls = {clip["video_id"]: clip["video"] for clip in clips}
    for clip_index, clip in enumerate(clips):
        for index, interaction in enumerate(clip.get("interactions", [])):
            action_id = interaction["id"]
            choices = {}
            for preset in creator_interaction_presets():
                if story and not preset.story_enabled:
                    continue
                try:
                    edit = {
                        "interaction_id": action_id,
                        "clip_id": clip["video_id"],
                        "type": preset.type,
                        "preset_id": preset.id,
                    }
                    _, candidate = compile_edits(
                        source, [edit],
                        item_id=clip["video_id"], video_url=clip["video"], video_urls=urls,
                    )
                except RuntimeSpecError:
                    continue
                choices[preset.id] = {
                    "preset_id": preset.id,
                    "interaction": candidate["video"][clip_index]["interactions"][index],
                    "runtime_spec_version": candidate["version"],
                }
            for legacy_key, preset_id in {
                "pinch": "pinch_in",
                "rotate": "rotate_counterclockwise",
                "camera_motion": "camera_motion.hand_open_palm",
                "camera_continuous": "camera_continuous.hand_finger_snap",
            }.items():
                if preset_id in choices:
                    choices[legacy_key] = choices[preset_id]
            options[f"{clip['video_id']}:{action_id}" if story else action_id] = choices
    return options
