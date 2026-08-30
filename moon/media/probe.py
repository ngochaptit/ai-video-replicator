from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class MediaProbeError(RuntimeError):
    pass


def probe_media(path: str | Path) -> dict[str, Any]:
    media_path = Path(path).expanduser().resolve()
    if not media_path.exists():
        raise FileNotFoundError(media_path)

    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(media_path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise MediaProbeError(f"ffprobe failed for {media_path}: {exc}") from exc

    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MediaProbeError(f"ffprobe returned invalid JSON for {media_path}") from exc

    streams = raw.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    format_info = raw.get("format") or {}

    duration = _float_or_none(format_info.get("duration"))
    if duration is None and video is not None:
        duration = _float_or_none(video.get("duration"))

    width = int(video.get("width")) if video and video.get("width") is not None else None
    height = int(video.get("height")) if video and video.get("height") is not None else None

    return {
        "path": str(media_path),
        "name": media_path.name,
        "size_bytes": media_path.stat().st_size,
        "duration_seconds": duration,
        "width": width,
        "height": height,
        "orientation": _orientation(width, height),
        "fps": _parse_rate(video.get("avg_frame_rate")) if video else None,
        "video_codec": video.get("codec_name") if video else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def _orientation(width: int | None, height: int | None) -> str | None:
    if width is None or height is None:
        return None
    if height > width:
        return "vertical"
    if width > height:
        return "horizontal"
    return "square"


def _parse_rate(value: Any) -> float | None:
    if not value or value == "0/0":
        return None
    text = str(value)
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        try:
            denominator_value = float(denominator)
            if denominator_value == 0:
                return None
            return round(float(numerator) / denominator_value, 6)
        except ValueError:
            return None
    return _float_or_none(text)


def _float_or_none(value: Any) -> float | None:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None
