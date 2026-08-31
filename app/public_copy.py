"""Canonical English copy embedded in public runtime specifications.

The values mirror Pixo Runtime interaction catalog 3.2.0.  Source timelines and
model output are editable evidence; this catalog owns the text presented by
published App/Web experiences.
"""

from __future__ import annotations

PUBLIC_COPY_CATALOG_VERSION = "pixo-runtime.interactions.3.2.0-en"

INTERACTION_INSTRUCTIONS: dict[str, str] = {
    "tap": "Tap",
    "double_tap": "Double tap",
    "rapid_tap": "Tap fast",
    "hold": "Hold",
    "hold_still": "Hold still",
    "hold_charge": "Hold to charge",
    "swipe_left": "Swipe left",
    "swipe_right": "Swipe right",
    "swipe_up": "Swipe up",
    "swipe_down": "Swipe down",
    "drag_left": "Hold & drag left",
    "drag_right": "Hold & drag right",
    "drag_up": "Hold & drag up",
    "drag_down": "Hold & drag down",
    "scrub_left": "Scrub left",
    "scrub_right": "Scrub right",
    "scrub_up": "Scrub up",
    "scrub_down": "Scrub down",
    "continuous_swipe": "Swipe back and forth to play",
    "continuous_tap": "Keep tapping to play",
    "pinch": "Pinch",
    "draw_circle": "Draw a circle",
    "erase": "Rub to erase",
    "camera_motion": "Follow the prompt using the front camera",
    "tilt_left": "Tilt left",
    "tilt_right": "Tilt right",
    "shake": "Shake your phone",
    "rotate": "Rotate your phone",
    "mic_level": "Make some noise",
    "mic_blow": "Blow at the mic",
    "mic_clap": "Clap once",
    "mic_quiet": "Stay quiet",
}


def interaction_instruction(gesture: str) -> str:
    """Return deterministic public copy for one supported Runtime gesture."""
    try:
        return INTERACTION_INSTRUCTIONS[gesture]
    except KeyError as exc:
        raise ValueError(f"unsupported public interaction type: {gesture!r}") from exc
