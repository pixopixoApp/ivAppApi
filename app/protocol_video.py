"""Compile editable interaction source into an immutable App runtime spec.

Compilation happens when content is created or published.  Feed/detail are not
allowed to invent defaults or reinterpret source timelines at request time.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from app.public_copy import interaction_instruction
from app.schemas import ClipOut
from app.vision_targets import (
    VisionTargetError,
    canonical_vision_instruction,
    normalize_vision_config,
)

log = logging.getLogger(__name__)

_CONF = 0.85
# v1.1 adds ``video[].on_end`` and continuous_swipe. v1.2 adds
# continuous_tap. Compilation deliberately keeps content on v1.1 unless the
# new interaction is present so clients can negotiate capabilities safely.
BASE_RUNTIME_SPEC_VERSION = "1.1"
RUNTIME_SPEC_VERSION = "1.2"
SUPPORTED_RUNTIME_SPEC_VERSIONS = frozenset(
    {"1.0", BASE_RUNTIME_SPEC_VERSION, RUNTIME_SPEC_VERSION}
)
LEGACY_CLIENT_RUNTIME_SPEC_VERSIONS = frozenset({"1.0", "1.1"})
RUNTIME_SPEC_SCHEMA = "pixo.runtime.v1"

# Per-gesture detection defaults (App interaction-type catalog).
_TOUCH_MID = {"confidence_threshold": _CONF, "place": "middle_middle"}
_MOTION_BOT = {"confidence_threshold": _CONF, "place": "middle_bottom"}

_DETECTION_BY_GESTURE: dict[str, dict[str, Any]] = {
    "tap": {**_TOUCH_MID, "response_window_ms": 0},
    "double_tap": {**_TOUCH_MID, "response_window_ms": 0},
    "rapid_tap": {**_TOUCH_MID, "response_window_ms": 0},
    "hold": {**_TOUCH_MID, "response_window_ms": 0, "min_duration_ms": 1000},
    "hold_still": {
        **_MOTION_BOT,
        "response_window_ms": 0,
        "min_duration_ms": 1000,
        "max_motion_score": 20,
    },
    "hold_charge": {**_TOUCH_MID, "response_window_ms": 0, "min_duration_ms": 1500},
    "swipe_left": {**_TOUCH_MID, "response_window_ms": 0, "min_distance_dp": 64},
    "swipe_right": {**_TOUCH_MID, "response_window_ms": 0, "min_distance_dp": 64},
    "swipe_up": {**_TOUCH_MID, "response_window_ms": 0, "min_distance_dp": 64},
    "swipe_down": {**_TOUCH_MID, "response_window_ms": 0, "min_distance_dp": 64},
    "drag_left": {**_TOUCH_MID, "response_window_ms": 0, "min_distance_dp": 56},
    "drag_right": {**_TOUCH_MID, "response_window_ms": 0, "min_distance_dp": 56},
    "drag_up": {**_TOUCH_MID, "response_window_ms": 0, "min_distance_dp": 56},
    "drag_down": {**_TOUCH_MID, "response_window_ms": 0, "min_distance_dp": 56},
    "scrub_left": {**_TOUCH_MID, "response_window_ms": 0, "min_travel_dp": 96},
    "scrub_right": {**_TOUCH_MID, "response_window_ms": 0, "min_travel_dp": 96},
    "scrub_up": {**_TOUCH_MID, "response_window_ms": 0, "min_travel_dp": 96},
    "scrub_down": {**_TOUCH_MID, "response_window_ms": 0, "min_travel_dp": 96},
    "continuous_swipe": {
        **_TOUCH_MID,
        "response_window_ms": 0,
        "min_travel_dp": 32,
        "idle_timeout_ms": 500,
    },
    "continuous_tap": {
        **_TOUCH_MID,
        "response_window_ms": 0,
        "idle_timeout_ms": 500,
    },
    "pinch": {**_TOUCH_MID, "response_window_ms": 0, "min_scale_delta": 0.2},
    "draw_circle": {
        **_TOUCH_MID,
        "response_window_ms": 0,
        "min_radius_dp": 24,
        "max_closure_gap_dp": 28,
    },
    "erase": {**_TOUCH_MID, "response_window_ms": 0, "min_travel_dp": 100},
    "camera_motion": {
        **_MOTION_BOT,
        "response_window_ms": 0,
        "min_motion_score": 45,
    },
    "tilt_left": {**_MOTION_BOT, "response_window_ms": 0, "min_angle_deg": 15},
    "tilt_right": {**_MOTION_BOT, "response_window_ms": 0, "min_angle_deg": 15},
    "shake": {**_MOTION_BOT, "response_window_ms": 0, "min_shake_score": 60},
    "rotate": {**_MOTION_BOT, "response_window_ms": 0, "min_angle_deg": 75},
    "mic_level": {
        **_MOTION_BOT,
        "response_window_ms": 0,
        "min_duration_ms": 300,
        "min_volume_score": 55,
    },
    "mic_blow": {
        **_MOTION_BOT,
        "response_window_ms": 0,
        "min_duration_ms": 300,
        "min_volume_score": 55,
    },
    "mic_clap": {**_MOTION_BOT, "response_window_ms": 0, "min_volume_score": 55},
    "mic_quiet": {
        **_MOTION_BOT,
        "response_window_ms": 0,
        "min_duration_ms": 1000,
        "max_volume_score": 20,
    },
}

_FEEDBACK = {
    "animation": "character_wave",
    "animation_duration_ms": 2000,
    "vibrate": True,
    "sound_effect": "sfx_cheer",
}
_CONTINUOUS_FEEDBACK = {
    "animation": "none",
    "animation_duration_ms": 0,
    "vibrate": False,
    "sound_effect": "",
}

_DEFAULT_PLACE = "middle_bottom"
_ACTION_CONTINUE = {"action": "continue"}
_TIMING_IMMEDIATE = "immediate"


def region_to_place(region: dict | None) -> str:
    """Map normalized region box to a 3x3 place id."""
    if not isinstance(region, dict):
        return _DEFAULT_PLACE
    try:
        x = float(region["x"])
        y = float(region["y"])
        w = float(region["w"])
        h = float(region["h"])
    except (KeyError, TypeError, ValueError):
        return _DEFAULT_PLACE
    cx = x + w / 2.0
    cy = y + h / 2.0
    col = "left" if cx < 1 / 3 else "right" if cx >= 2 / 3 else "middle"
    row = "top" if cy < 1 / 3 else "bottom" if cy >= 2 / 3 else "middle"
    return f"{col}_{row}"


class RuntimeSpecError(ValueError):
    """Source cannot be compiled or persisted runtime data is invalid."""


def supported_gestures() -> frozenset[str]:
    return frozenset(_DETECTION_BY_GESTURE)


def normalize_client_runtime_spec_versions(
    declared: list[str] | None,
) -> frozenset[str]:
    """Intersect client capabilities with the server without rejecting future values."""
    if declared is None:
        return LEGACY_CLIENT_RUNTIME_SPEC_VERSIONS
    return frozenset(declared).intersection(SUPPORTED_RUNTIME_SPEC_VERSIONS)


def runtime_spec_version_from_compiled(spec: dict[str, Any]) -> str:
    version = spec.get("version") if isinstance(spec, dict) else None
    if version not in SUPPORTED_RUNTIME_SPEC_VERSIONS:
        raise RuntimeSpecError(f"compiled runtime spec has invalid version: {version!r}")
    return str(version)


def _detection_for_item(item: dict, *, gesture: str) -> dict[str, Any]:
    base = _DETECTION_BY_GESTURE.get(gesture)
    if base is None:
        raise RuntimeSpecError(f"unsupported gesture: {gesture}")
    detection = dict(base)
    region = item.get("region")
    if isinstance(region, dict):
        detection["place"] = region_to_place(region)
    gate = item.get("gate_at_ms")
    gate_end = item.get("gate_end_ms")
    if isinstance(gate, int) and not isinstance(gate, bool) and isinstance(gate_end, int) and not isinstance(gate_end, bool):
        if gate_end < gate:
            raise RuntimeSpecError("gate_end_ms must be >= gate_at_ms")
        detection["response_window_ms"] = gate_end - gate
    if gesture == "camera_motion":
        try:
            detection["vision"] = normalize_vision_config(item.get("vision"))
        except VisionTargetError as exc:
            raise RuntimeSpecError(str(exc)) from exc
    return detection


def _validate_sustained_source(
    timeline: dict[str, Any],
    interactions: list[Any],
) -> None:
    sustained_types = {"continuous_swipe", "continuous_tap"}
    if not any(
        isinstance(item, dict) and item.get("gesture") in sustained_types
        for item in interactions
    ):
        return
    media = timeline.get("media")
    duration = media.get("duration_ms") if isinstance(media, dict) else None
    if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
        raise RuntimeSpecError(
            "timeline.media.duration_ms is required for sustained interactions"
        )
    for index, item in enumerate(interactions):
        if not isinstance(item, dict) or item.get("gesture") not in sustained_types:
            continue
        label = f"interaction[{index}]"
        gesture = str(item.get("gesture"))
        gate = item.get("gate_at_ms")
        if isinstance(gate, bool) or not isinstance(gate, int) or gate < 0:
            raise RuntimeSpecError(f"{label} gate_at_ms must be a non-negative integer")
        if gate >= duration:
            raise RuntimeSpecError(
                f"{label} {gesture} must start before media.duration_ms"
            )
        if item.get("pause_video", True) is not True:
            raise RuntimeSpecError(f"{label} {gesture} requires pause_video=true")
        if "gate_end_ms" in item:
            raise RuntimeSpecError(f"{label} {gesture} does not allow gate_end_ms")
        if "outcomes" in item:
            raise RuntimeSpecError(f"{label} {gesture} does not allow outcomes")
        if "region" in item:
            raise RuntimeSpecError(f"{label} {gesture} does not allow region")
        if index + 1 < len(interactions):
            following = interactions[index + 1]
            next_gate = following.get("gate_at_ms") if isinstance(following, dict) else None
            if (
                isinstance(next_gate, bool)
                or not isinstance(next_gate, int)
                or next_gate <= gate
            ):
                raise RuntimeSpecError(
                    f"{label} {gesture} must end at a later interaction"
                )


def _outcome_to_action(outcome: Any) -> dict[str, Any]:
    """
    Map story outcome edge → v1.0 Action.

    - goto + clip_id → jump_video (timing=immediate)
    - continue → continue
    - replay → restart_video
    - missing / end / timeout / unknown → continue
    """
    if not isinstance(outcome, dict):
        return dict(_ACTION_CONTINUE)
    action = outcome.get("action")
    if action == "goto":
        clip_id = outcome.get("clip_id")
        if isinstance(clip_id, str) and clip_id.strip():
            return {
                "action": "jump_video",
                "target_video_id": clip_id.strip(),
                "timing": _TIMING_IMMEDIATE,
            }
        return dict(_ACTION_CONTINUE)
    if action == "continue":
        return dict(_ACTION_CONTINUE)
    if action == "replay":
        return {"action": "restart_video"}
    return dict(_ACTION_CONTINUE)


def _actions_from_item(item: dict) -> tuple[dict[str, Any], dict[str, Any]]:
    outcomes = item.get("outcomes")
    if not isinstance(outcomes, dict):
        return dict(_ACTION_CONTINUE), dict(_ACTION_CONTINUE)
    return (
        _outcome_to_action(outcomes.get("success")),
        _outcome_to_action(outcomes.get("fail")),
    )


def _one_interaction(item: dict, *, index: int) -> dict:
    gesture = item.get("gesture")
    gate = item.get("gate_at_ms")
    if not isinstance(gesture, str) or not gesture.strip():
        raise RuntimeSpecError(f"interaction[{index}] gesture required")
    gesture = gesture.strip()
    if isinstance(gate, bool) or not isinstance(gate, int):
        raise RuntimeSpecError(f"interaction[{index}] gate_at_ms must be an integer")
    if gate < 0:
        raise RuntimeSpecError(f"interaction[{index}] gate_at_ms must be non-negative")
    detection = _detection_for_item(item, gesture=gesture)
    sustained = gesture in {"continuous_swipe", "continuous_tap"}
    if sustained:
        on_success, on_miss = dict(_ACTION_CONTINUE), dict(_ACTION_CONTINUE)
    else:
        on_success, on_miss = _actions_from_item(item)
    pause_video = item.get("pause_video", True)
    if not isinstance(pause_video, bool):
        raise RuntimeSpecError(f"interaction[{index}] pause_video must be boolean")
    description = interaction_instruction(gesture)
    if gesture == "camera_motion":
        vision = detection.get("vision")
        target = vision.get("target") if isinstance(vision, dict) else None
        if isinstance(target, str):
            try:
                description = canonical_vision_instruction(target)
            except VisionTargetError as exc:
                raise RuntimeSpecError(str(exc)) from exc
    return {
        "id": f"action_{index + 1:03d}",
        "type": gesture,
        "description": description,
        "offset_time_ms": gate,
        "pause_video": pause_video,
        "detection": detection,
        "feedback": dict(_CONTINUOUS_FEEDBACK if sustained else _FEEDBACK),
        "on_success": on_success,
        "on_miss": on_miss,
    }


def timeline_to_clip(
    timeline: dict | None,
    video_url: str,
    *,
    clip_id: str,
) -> dict[str, Any]:
    """Convert a single-segment timeline into one protocol clip (App v1.0)."""
    interactions_out: list[dict] = []
    if isinstance(timeline, dict):
        interactions = timeline.get("interactions") or []
        if not isinstance(interactions, list):
            raise RuntimeSpecError("timeline.interactions must be an array")
        _validate_sustained_source(timeline, interactions)
        for index, item in enumerate(interactions):
            if not isinstance(item, dict):
                raise RuntimeSpecError(f"interaction[{index}] must be an object")
            interactions_out.append(_one_interaction(item, index=index))
    return {
        "video_id": clip_id,
        "video": video_url,
        "interactions": interactions_out,
    }


def timeline_to_video(
    timeline: dict | None,
    video_url: str,
    *,
    clip_id: str | None = None,
) -> dict[str, Any]:
    """
    Convert an ivcore timeline into one protocol clip.

    ``clip_id`` defaults to empty and should be set by callers (publish item id).
    Returns ``{video_id, video, interactions}`` (no transition).
    """
    cid = (clip_id or "").strip() or "clip"
    return timeline_to_clip(timeline, video_url, clip_id=cid)


def _on_end_action(on_end: Any) -> dict[str, Any] | None:
    """Map the Story clip-end event to the existing ExperienceSpec Action union."""
    if not isinstance(on_end, dict):
        return None
    action = on_end.get("action")
    if action == "goto":
        clip_id = on_end.get("clip_id")
        if isinstance(clip_id, str) and clip_id.strip():
            return {
                "action": "jump_video",
                "target_video_id": clip_id.strip(),
                "timing": _TIMING_IMMEDIATE,
            }
        return None
    if action == "end":
        return {"action": "end_experience", "timing": _TIMING_IMMEDIATE}
    if action == "retry_previous_point":
        return {"action": "retry_previous_point"}
    return None


def _clip_from_story_body(
    body: dict,
    *,
    clip_id: str,
    video_url: str,
) -> dict[str, Any]:
    """timeline.interactions → interactions[]; optional body.on_end → clip.on_end."""
    timeline = body.get("timeline")
    clip = timeline_to_clip(
        timeline if isinstance(timeline, dict) else None,
        video_url,
        clip_id=clip_id,
    )
    end_action = _on_end_action(body.get("on_end"))
    if "on_end" in body and end_action is None:
        raise RuntimeSpecError(f"story clip {clip_id!r} has an invalid on_end action")
    if end_action is not None:
        clip["on_end"] = end_action
    return clip


def story_to_video(
    story: dict | None,
    url_by_clip_id: dict[str, str],
) -> list[dict[str, Any]]:
    """
    Convert story ``{entry_clip_id, clips}`` into protocol clips.

    Entry clip is placed first. Missing URLs are skipped with a warning.
    """
    if not isinstance(story, dict):
        return []
    clips_in = story.get("clips")
    if not isinstance(clips_in, dict) or not clips_in:
        return []

    entry = str(story.get("entry_clip_id") or "").strip()
    ordered_ids: list[str] = []
    if entry and entry in clips_in:
        ordered_ids.append(entry)
    for cid in clips_in:
        if cid == entry:
            continue
        ordered_ids.append(str(cid))

    out: list[dict[str, Any]] = []
    for cid in ordered_ids:
        body = clips_in.get(cid)
        if not isinstance(body, dict):
            continue
        url = url_by_clip_id.get(cid) or url_by_clip_id.get(str(cid))
        if not url:
            log.warning("story_to_video missing url for clip_id=%s", cid)
            continue
        out.append(
            _clip_from_story_body(
                body,
                clip_id=str(cid),
                video_url=url,
            )
        )
    return out


def result_to_video(result: dict, video_url: str, *, clip_id: str | None = None) -> dict[str, Any]:
    """Convenience: read ``result['timeline']`` then ``timeline_to_video``."""
    return timeline_to_video(
        result.get("timeline") if isinstance(result, dict) else None,
        video_url,
        clip_id=clip_id,
    )


def compile_runtime_spec(
    *,
    item_id: str,
    content_mode: str,
    source: dict[str, Any],
    video_url: str,
    video_urls: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compile and validate the complete playback payload for persistence."""
    mode = (content_mode or "single").strip().lower()
    if mode == "story":
        clips_in = source.get("clips") if isinstance(source, dict) else None
        if not isinstance(clips_in, dict) or not clips_in:
            raise RuntimeSpecError("story.clips must be a non-empty object")
        entry = source.get("entry_clip_id")
        if not isinstance(entry, str) or entry not in clips_in:
            raise RuntimeSpecError("story.entry_clip_id must reference a clip")
        for clip_id, body in clips_in.items():
            if not isinstance(body, dict) or not isinstance(body.get("timeline"), dict):
                raise RuntimeSpecError(f"story clip {clip_id!r} has no timeline")
        if video_urls is None:
            # Legacy/local publications use the stable API indirection. OSS
            # publications pass an exact immutable public URL for every clip.
            urls = {str(cid): f"/media/{item_id}/{cid}.mp4" for cid in clips_in}
        else:
            urls = {
                str(cid): str(video_urls.get(str(cid)) or "").strip()
                for cid in clips_in
            }
            missing = sorted(cid for cid, url in urls.items() if not url)
            if missing:
                raise RuntimeSpecError(
                    f"story media URLs missing for clips: {', '.join(missing)}"
                )
        clips_raw = story_to_video(source, urls)
    elif mode == "single":
        clips_raw = [timeline_to_video(source, video_url, clip_id=item_id)]
    else:
        raise RuntimeSpecError(f"unsupported content mode: {content_mode}")

    if not clips_raw:
        raise RuntimeSpecError("runtime spec has no playable clips")
    try:
        clips = [
            ClipOut.model_validate(clip).model_dump(mode="json", exclude_none=True)
            for clip in clips_raw
        ]
    except ValidationError as exc:
        raise RuntimeSpecError(f"invalid compiled runtime spec: {exc}") from exc
    clip_ids = {clip["video_id"] for clip in clips}
    incoming_interaction_targets: set[str] = set()
    for clip in clips:
        actions = [clip.get("on_end")]
        for interaction in clip["interactions"]:
            actions.extend((interaction.get("on_success"), interaction.get("on_miss")))
        for action in actions:
            if not isinstance(action, dict) or action.get("action") != "jump_video":
                continue
            if action.get("target_video_id") not in clip_ids:
                raise RuntimeSpecError(
                    f"jump target not found: {action.get('target_video_id')!r}"
                )
        for interaction in clip["interactions"]:
            for action in (interaction.get("on_success"), interaction.get("on_miss")):
                if isinstance(action, dict) and action.get("action") == "jump_video":
                    incoming_interaction_targets.add(str(action.get("target_video_id") or ""))
    for clip in clips:
        if (
            isinstance(clip.get("on_end"), dict)
            and clip["on_end"].get("action") == "retry_previous_point"
            and clip["video_id"] not in incoming_interaction_targets
        ):
            raise RuntimeSpecError(
                f"video {clip['video_id']!r} on_end retry_previous_point requires "
                "an incoming interaction jump_video"
            )
    compiled_version = (
        RUNTIME_SPEC_VERSION
        if any(
            interaction["type"] == "continuous_tap"
            for clip in clips
            for interaction in clip["interactions"]
        )
        else BASE_RUNTIME_SPEC_VERSION
    )
    return {
        "schema": RUNTIME_SPEC_SCHEMA,
        "version": compiled_version,
        "item_id": item_id,
        "video": clips,
    }


def read_runtime_spec(
    spec: dict[str, Any] | None,
    *,
    item_id: str,
    version: str | None,
) -> list[ClipOut]:
    """Validate persisted data without adding, changing, or defaulting fields."""
    if version not in SUPPORTED_RUNTIME_SPEC_VERSIONS:
        raise RuntimeSpecError(
            f"unsupported runtime spec version: {version!r}"
        )
    if not isinstance(spec, dict):
        raise RuntimeSpecError("runtime spec missing")
    if spec.get("schema") != RUNTIME_SPEC_SCHEMA:
        raise RuntimeSpecError("runtime spec schema mismatch")
    if spec.get("version") != version:
        raise RuntimeSpecError("runtime spec embedded version mismatch")
    if spec.get("item_id") != item_id:
        raise RuntimeSpecError("runtime spec item_id mismatch")
    raw_clips = spec.get("video")
    if not isinstance(raw_clips, list) or not raw_clips:
        raise RuntimeSpecError("runtime spec has no video clips")
    try:
        clips = [ClipOut.model_validate(raw) for raw in raw_clips]
    except ValidationError as exc:
        raise RuntimeSpecError(f"runtime spec validation failed: {exc}") from exc
    if version == "1.0" and any(clip.on_end is not None for clip in clips):
        raise RuntimeSpecError("video.on_end requires runtime spec version 1.1")
    clip_ids = {clip.video_id for clip in clips}
    incoming_interaction_targets: set[str] = set()
    for clip in clips:
        actions = [clip.on_end]
        for interaction in clip.interactions:
            actions.extend((interaction.on_success, interaction.on_miss))
            for action in (interaction.on_success, interaction.on_miss):
                if action.action == "jump_video" and action.target_video_id:
                    incoming_interaction_targets.add(action.target_video_id)
        for action in actions:
            if action is None or action.action != "jump_video":
                continue
            if action.target_video_id not in clip_ids:
                raise RuntimeSpecError(
                    f"jump target not found: {action.target_video_id!r}"
                )
        for index, interaction in enumerate(clip.interactions):
            if interaction.type not in _DETECTION_BY_GESTURE:
                raise RuntimeSpecError(f"unsupported gesture: {interaction.type}")
            if version == "1.0" and interaction.type == "continuous_swipe":
                raise RuntimeSpecError("continuous_swipe requires runtime spec version 1.1")
            if version in {"1.0", "1.1"} and interaction.type == "continuous_tap":
                raise RuntimeSpecError("continuous_tap requires runtime spec version 1.2")
            if interaction.detection.response_window_ms < 0:
                raise RuntimeSpecError("interaction response_window_ms must be non-negative")
            if interaction.type == "continuous_swipe":
                detection = interaction.detection
                if (
                    interaction.offset_time_ms is None
                    or interaction.pause_video is not True
                    or detection.response_window_ms != 0
                    or detection.place != "middle_middle"
                    or detection.min_travel_dp != 32
                    or detection.idle_timeout_ms not in {180, 360, 500}
                    or interaction.on_success.action != "continue"
                    or interaction.on_miss.action != "continue"
                ):
                    raise RuntimeSpecError(
                        "continuous_swipe persisted contract is invalid"
                    )
                next_offset = (
                    clip.interactions[index + 1].offset_time_ms
                    if index + 1 < len(clip.interactions)
                    else None
                )
                if next_offset is not None and next_offset <= interaction.offset_time_ms:
                    raise RuntimeSpecError(
                        "continuous_swipe must end at a later interaction"
                    )
            if interaction.type == "continuous_tap":
                detection = interaction.detection
                if (
                    interaction.pause_video is not True
                    or detection.response_window_ms != 0
                    or detection.place != "middle_middle"
                    or detection.idle_timeout_ms != 500
                    or detection.min_travel_dp is not None
                    or interaction.on_success.action != "continue"
                    or interaction.on_miss.action != "continue"
                ):
                    raise RuntimeSpecError(
                        "continuous_tap persisted contract is invalid"
                    )
                next_offset = (
                    clip.interactions[index + 1].offset_time_ms
                    if index + 1 < len(clip.interactions)
                    else None
                )
                if next_offset is not None and next_offset <= interaction.offset_time_ms:
                    raise RuntimeSpecError(
                        "continuous_tap must end at a later interaction"
                    )
            if interaction.type == "camera_motion":
                try:
                    normalize_vision_config((interaction.detection.model_extra or {}).get("vision"))
                except VisionTargetError as exc:
                    raise RuntimeSpecError(str(exc)) from exc
    for clip in clips:
        if (
            clip.on_end is not None
            and clip.on_end.action == "retry_previous_point"
            and clip.video_id not in incoming_interaction_targets
        ):
            raise RuntimeSpecError(
                f"video {clip.video_id!r} on_end retry_previous_point requires "
                "an incoming interaction jump_video"
            )
    return clips
