"""Downstream protocol adapter: timeline / story → Feed clip layer (App v1.0).

Non-core conversion only. Does not change analyze_video outputs.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

log = logging.getLogger(__name__)

_CONF = 0.85

# Per-gesture detection defaults (App interaction-type catalog).
_TOUCH_MID = {"confidence_threshold": _CONF, "place": "middle_middle"}
_MOTION_BOT = {"confidence_threshold": _CONF, "place": "middle_bottom"}

_DETECTION_BY_GESTURE: dict[str, dict[str, Any]] = {
    "tap": {**_TOUCH_MID, "response_window_ms": 500},
    "double_tap": {**_TOUCH_MID, "response_window_ms": 650},
    "rapid_tap": {**_TOUCH_MID, "response_window_ms": 850},
    "hold": {**_TOUCH_MID, "response_window_ms": 1200, "min_duration_ms": 1000},
    "hold_still": {
        **_MOTION_BOT,
        "response_window_ms": 1200,
        "min_duration_ms": 1000,
        "max_motion_score": 20,
    },
    "hold_charge": {**_TOUCH_MID, "response_window_ms": 1800, "min_duration_ms": 1500},
    "swipe_left": {**_TOUCH_MID, "response_window_ms": 800, "min_distance_dp": 64},
    "swipe_right": {**_TOUCH_MID, "response_window_ms": 800, "min_distance_dp": 64},
    "swipe_up": {**_TOUCH_MID, "response_window_ms": 800, "min_distance_dp": 64},
    "swipe_down": {**_TOUCH_MID, "response_window_ms": 800, "min_distance_dp": 64},
    "drag_left": {**_TOUCH_MID, "response_window_ms": 1200, "min_distance_dp": 56},
    "drag_right": {**_TOUCH_MID, "response_window_ms": 1200, "min_distance_dp": 56},
    "drag_up": {**_TOUCH_MID, "response_window_ms": 1200, "min_distance_dp": 56},
    "drag_down": {**_TOUCH_MID, "response_window_ms": 1200, "min_distance_dp": 56},
    "scrub_left": {**_TOUCH_MID, "response_window_ms": 1500, "min_travel_dp": 96},
    "scrub_right": {**_TOUCH_MID, "response_window_ms": 1500, "min_travel_dp": 96},
    "scrub_up": {**_TOUCH_MID, "response_window_ms": 1500, "min_travel_dp": 96},
    "scrub_down": {**_TOUCH_MID, "response_window_ms": 1500, "min_travel_dp": 96},
    "pinch": {**_TOUCH_MID, "response_window_ms": 1200, "min_scale_delta": 0.2},
    "draw_circle": {
        **_TOUCH_MID,
        "response_window_ms": 1800,
        "min_radius_dp": 24,
        "max_closure_gap_dp": 28,
    },
    "erase": {**_TOUCH_MID, "response_window_ms": 1500, "min_travel_dp": 100},
    "camera_motion": {
        **_MOTION_BOT,
        "response_window_ms": 5000,
        "min_motion_score": 45,
    },
    "tilt_left": {**_MOTION_BOT, "response_window_ms": 1200, "min_angle_deg": 15},
    "tilt_right": {**_MOTION_BOT, "response_window_ms": 1200, "min_angle_deg": 15},
    "shake": {**_MOTION_BOT, "response_window_ms": 1500, "min_shake_score": 60},
    "rotate": {**_MOTION_BOT, "response_window_ms": 1800, "min_angle_deg": 75},
    "mic_level": {
        **_MOTION_BOT,
        "response_window_ms": 4000,
        "min_duration_ms": 600,
        "min_volume_score": 60,
    },
    "mic_blow": {
        **_MOTION_BOT,
        "response_window_ms": 4000,
        "min_duration_ms": 1000,
        "min_volume_score": 100,
    },
    "mic_clap": {**_MOTION_BOT, "response_window_ms": 4000, "min_volume_score": 80},
    "mic_quiet": {
        **_MOTION_BOT,
        "response_window_ms": 4000,
        "min_duration_ms": 1000,
        "max_volume_score": 20,
    },
}

_FALLBACK_GESTURE = "tap"

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


def _gate_end_window_ms(item: dict, gate_at_ms: int) -> int | None:
    """Return response_window_ms from gate_end_ms, or None if missing/invalid."""
    end = item.get("gate_end_ms")
    if isinstance(end, bool) or not isinstance(end, int):
        return None
    if end < gate_at_ms:
        return None
    return end - gate_at_ms


def _detection_for_item(item: dict, *, gesture: str, gate_at_ms: int) -> dict[str, Any]:
    base = _DETECTION_BY_GESTURE.get(gesture) or _DETECTION_BY_GESTURE[_FALLBACK_GESTURE]
    detection = deepcopy(base)
    region = item.get("region")
    if isinstance(region, dict):
        detection["place"] = region_to_place(region)
    window = _gate_end_window_ms(item, gate_at_ms)
    if window is not None:
        detection["response_window_ms"] = window
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


def _one_interaction(item: dict, *, index: int) -> dict | None:
    gesture = item.get("gesture")
    gate = item.get("gate_at_ms")
    if not isinstance(gesture, str) or not gesture:
        return None
    if isinstance(gate, bool) or not isinstance(gate, int):
        return None
    detection = _detection_for_item(item, gesture=gesture, gate_at_ms=gate)
    on_success, on_miss = _actions_from_item(item)
    return {
        "id": f"action_{index + 1:03d}",
        "type": gesture,
        "description": item.get("hint") or "",
        "offset_time_ms": gate,
        "pause_video": True,
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
        for index, item in enumerate(timeline.get("interactions") or []):
            if not isinstance(item, dict):
                continue
            converted = _one_interaction(item, index=index)
            if converted:
                interactions_out.append(converted)
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
