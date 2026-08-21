from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class FrameSamplingError(RuntimeError):
    pass


@dataclass(frozen=True)
class SampledFrames:
    fast: list[Path]
    slow: list[Path]
    fast_times_ms: list[int]
    slow_times_ms: list[int]


def _times(duration_ms: int, count: int) -> list[int]:
    if count <= 1:
        return [0]
    # Avoid seeking exactly at the container duration where no decoded frame may
    # exist (especially with variable frame-rate sources).
    last = max(0, duration_ms - min(100, duration_ms))
    return sorted(
        {round((last * index) / (count - 1)) for index in range(count)}
    )


def _sample_at_times(
    source: Path,
    output_dir: Path,
    *,
    times_ms: list[int],
    width: int,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, timestamp_ms in enumerate(times_ms, start=1):
        destination = output_dir / f"frame_{index:03d}.jpg"
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{timestamp_ms / 1000:.3f}",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale=w=min({width}\\,iw):h=-2:force_divisible_by=2",
                    "-q:v",
                    "3",
                    str(destination),
                ],
                check=True,
                capture_output=True,
                timeout=45,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise FrameSamplingError(f"could not sample frame at {timestamp_ms}ms") from exc
        if not destination.is_file() or destination.stat().st_size == 0:
            raise FrameSamplingError(f"sampled frame at {timestamp_ms}ms is empty")
        paths.append(destination)
    return paths


def sample_analysis_frames(
    source: Path,
    output_root: Path,
    *,
    duration_ms: int,
    fast_count: int = 24,
    slow_count: int = 8,
) -> SampledFrames:
    fast_times = _times(duration_ms, fast_count)
    slow_times = _times(duration_ms, slow_count)
    fast = _sample_at_times(
        source,
        output_root / "fast",
        times_ms=fast_times,
        width=320,
    )
    slow = _sample_at_times(
        source,
        output_root / "slow",
        times_ms=slow_times,
        width=720,
    )
    return SampledFrames(
        fast=fast,
        slow=slow,
        fast_times_ms=fast_times,
        slow_times_ms=slow_times,
    )
