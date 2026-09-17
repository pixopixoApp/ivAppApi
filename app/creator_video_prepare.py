from __future__ import annotations

import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from app.config import Settings
from app.media_cache import ensure_upload_capacity
from app.video_probe import VideoProbeError, probe_video

FIRST_30_SECONDS_PROFILE = "first-30s-v1"


class VideoPreparationError(ValueError):
    pass


@contextmanager
def prepared_creator_video(source: Path, settings: Settings):
    """Prepare a bounded source clip; never submit AI work or change the input."""
    ensure_upload_capacity(settings, settings.creator_video_max_bytes)
    with tempfile.TemporaryDirectory(
        prefix="creator-prepare-", dir=source.parent
    ) as directory:
        output = Path(directory) / "source.mp4"
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-i",
                    str(source),
                    "-t",
                    str(min(30, settings.creator_video_max_duration_seconds)),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                    "-vf",
                    "scale=w='min(1080,iw)':h='min(1920,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "160k",
                    "-movflags",
                    "+faststart",
                    "-metadata:s:v:0",
                    "rotate=0",
                    "-y",
                    str(output),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
            metadata = probe_video(output)
            if (
                metadata.duration_ms
                > settings.creator_video_max_duration_seconds * 1000
            ):
                raise VideoPreparationError(
                    "The prepared clip exceeds the video duration limit."
                )
            if output.stat().st_size > settings.creator_video_max_bytes:
                raise VideoPreparationError(
                    "The prepared clip is larger than 120 MB. Choose a smaller source."
                )
        except (subprocess.SubprocessError, OSError, VideoProbeError) as exc:
            raise VideoPreparationError(
                "This video could not be prepared. Retry or choose another MP4."
            ) from exc
        yield output, metadata
