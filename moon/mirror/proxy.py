from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

from moon.media.probe import probe_media


AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav"}
H264_CONTAINERS = {".avi", ".m4v", ".mkv", ".mov", ".mp4"}


class ProxyBuilder:
    def __init__(
        self,
        *,
        runner: Callable[..., Any] = subprocess.run,
        prober: Callable[[str | Path], dict[str, Any]] = probe_media,
    ) -> None:
        self.runner = runner
        self.prober = prober

    @staticmethod
    def proxy_relative_path(asset_id: str, source: Path, media: dict[str, Any]) -> str:
        if not media.get("has_video"):
            return f"proxies/{asset_id}{source.suffix.lower()}"
        suffix = source.suffix.lower() if source.suffix.lower() in H264_CONTAINERS else ".mp4"
        return f"proxies/{asset_id}{suffix}"

    def build(self, source: Path, destination: Path, media: dict[str, Any]) -> dict[str, Any]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.stem}.{os.getpid()}.{uuid.uuid4().hex}{destination.suffix}"
        )
        try:
            if not media.get("has_video"):
                shutil.copy2(source, temporary)
            else:
                command = [
                    "ffmpeg", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
                    "-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
                    "-movflags", "+faststart", str(temporary),
                ]
                self.runner(command, capture_output=True, text=True, check=True)
            result = self.prober(temporary)
            expected = media.get("duration_seconds")
            actual = result.get("duration_seconds")
            if expected is not None and actual is not None and abs(float(expected) - float(actual)) > 0.12:
                raise ValueError(f"proxy duration drift for {source}: {expected} -> {actual}")
            os.replace(temporary, destination)
            return result
        finally:
            temporary.unlink(missing_ok=True)
