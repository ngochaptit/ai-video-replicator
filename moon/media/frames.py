from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Any


class FrameSamplingError(RuntimeError):
    pass


def sample_frames(
    source: str | Path,
    output_dir: str | Path,
    *,
    start_seconds: float,
    end_seconds: float,
    count: int = 8,
    width: int = 320,
) -> dict[str, Any]:
    source_path = Path(source).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    if start_seconds < 0 or end_seconds <= start_seconds:
        raise ValueError("frame window must satisfy 0 <= start < end")
    if count < 1 or count > 24:
        raise ValueError("count must be between 1 and 24")
    if width < 64:
        raise ValueError("width must be at least 64")

    target_dir = Path(output_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    span = end_seconds - start_seconds
    timestamps = _sample_timestamps(start_seconds, end_seconds, count)
    frames: list[dict[str, Any]] = []

    for index, timestamp in enumerate(timestamps, start=1):
        output_path = target_dir / f"frame_{index:03d}_{timestamp:.3f}s.jpg"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp:.6f}",
            "-i",
            str(source_path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-2",
            "-q:v",
            "2",
            "-y",
            str(output_path),
        ]
        try:
            subprocess.run(command, capture_output=True, text=True, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise FrameSamplingError(f"ffmpeg failed at {timestamp:.3f}s for {source_path}: {exc}") from exc
        frames.append({"timestamp_seconds": round(timestamp, 6), "path": str(output_path)})

    return {
        "source": str(source_path),
        "start_seconds": round(start_seconds, 6),
        "end_seconds": round(end_seconds, 6),
        "window_seconds": round(span, 6),
        "count": len(frames),
        "width": width,
        "frames": frames,
    }


def _sample_timestamps(start_seconds: float, end_seconds: float, count: int) -> list[float]:
    if count == 1:
        return [(start_seconds + end_seconds) / 2.0]
    epsilon = min(0.001, (end_seconds - start_seconds) / 1000.0)
    usable_end = max(start_seconds, end_seconds - epsilon)
    step = (usable_end - start_seconds) / (count - 1)
    values = [start_seconds + step * index for index in range(count)]
    return [0.0 if math.isclose(value, 0.0) else value for value in values]
