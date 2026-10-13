from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


class VideoProbeError(ValueError):
    pass


@dataclass(frozen=True)
class VideoMetadata:
    duration_ms: int
    width: int
    height: int


def probe_video(path: Path, *, timeout_seconds: float = 15.0) -> VideoMetadata:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "format=duration:stream=width,height,duration",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        payload = json.loads(completed.stdout)
        streams = payload.get("streams") or []
        stream = streams[0]
        format_duration = float((payload.get("format") or {}).get("duration"))
        stream_duration_value = stream.get("duration")
        try:
            stream_duration = float(stream_duration_value)
        except (TypeError, ValueError):
            stream_duration = 0
        duration = stream_duration if stream_duration > 0 else format_duration
        width = int(stream.get("width"))
        height = int(stream.get("height"))
    except (subprocess.SubprocessError, OSError, ValueError, TypeError, IndexError, json.JSONDecodeError) as exc:
        raise VideoProbeError("video could not be decoded") from exc
    if duration <= 0 or width <= 0 or height <= 0:
        raise VideoProbeError("video metadata is invalid")
    return VideoMetadata(
        duration_ms=max(1, round(duration * 1000)),
        width=width,
        height=height,
    )
