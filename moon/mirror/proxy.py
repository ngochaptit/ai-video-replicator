from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable

from moon.media.probe import probe_media


AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav"}
H264_CONTAINERS = {".m4v", ".mkv", ".mov", ".mp4"}


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
    def proxy_relative_path(relative_path: str, source: Path, media: dict[str, Any]) -> str:
        """Return the GPT-visible path, preserving source identity when possible."""
        normalized = PurePosixPath(str(relative_path).replace("\\", "/"))
        if not media.get("has_video") or source.suffix.lower() in H264_CONTAINERS:
            return normalized.as_posix()
        # A renamed container is explicit in manifest.proxy.path/renamed. Never
        # silently pretend an MP4 payload still has the source extension.
        return normalized.with_name(f"{normalized.name}.proxy.mp4").as_posix()

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
                    "-vf", "scale=720:720:force_original_aspect_ratio=decrease:force_divisible_by=2",
                    "-c:v", "libx264", "-preset", "veryfast",
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
