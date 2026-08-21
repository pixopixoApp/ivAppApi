"""Compile editable interaction source into an immutable App runtime spec.

Compilation happens when content is created or published.  Feed/detail are not
allowed to invent defaults or reinterpret source timelines at request time.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from app.schemas import ClipOut
from app.vision_targets import VisionTargetError, normalize_vision_config

log = logging.getLogger(__name__)

_CONF = 0.85
RUNTIME_SPEC_VERSION = "1.0"
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
    on_success, on_miss = _actions_from_item(item)
    pause_video = item.get("pause_video", True)
    if not isinstance(pause_video, bool):
        raise RuntimeSpecError(f"interaction[{index}] pause_video must be boolean")
    return {
        "id": f"action_{index + 1:03d}",
        "type": gesture,
        "description": item.get("hint") or "",
        "offset_time_ms": gate,
        "pause_video": pause_video,
        "detection": detection,
        "feedback": dict(_FEEDBACK),
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
    """Map clip-level on_end → Action; only goto is supported. Invalid/missing → None."""
    if not isinstance(on_end, dict):
        return None
    if on_end.get("action") != "goto":
        return None
    clip_id = on_end.get("clip_id")
    if isinstance(clip_id, str) and clip_id.strip():
        return {
            "action": "jump_video",
            "target_video_id": clip_id.strip(),
            "timing": _TIMING_IMMEDIATE,
        }
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
    return {
        "schema": RUNTIME_SPEC_SCHEMA,
        "version": RUNTIME_SPEC_VERSION,
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
    if version != RUNTIME_SPEC_VERSION:
        raise RuntimeSpecError(
            f"runtime spec version mismatch: {version!r} != {RUNTIME_SPEC_VERSION!r}"
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
    for clip in clips:
        for interaction in clip.interactions:
            if interaction.detection.response_window_ms < 0:
                raise RuntimeSpecError("interaction response_window_ms must be non-negative")
            if interaction.type == "camera_motion":
                try:
                    normalize_vision_config((interaction.detection.model_extra or {}).get("vision"))
                except VisionTargetError as exc:
                    raise RuntimeSpecError(str(exc)) from exc
    return clips
