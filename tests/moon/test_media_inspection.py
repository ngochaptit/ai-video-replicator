from __future__ import annotations

import json
from pathlib import Path

import pytest

from moon.core.project import MoonProject
from moon.media.frames import _sample_timestamps
from moon.media.inspection import inspect_footage, resolve_project_source
from moon.media.probe import _orientation, _parse_rate, probe_media
from moon.protocol import MoonProtocol
from moon.runner.pipeline import PipelineRunner


def test_probe_media_normalizes_ffprobe_payload(tmp_path, monkeypatch) -> None:
    source = tmp_path / "reference.mp4"
    source.write_bytes(b"video")

    payload = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 576,
                "height": 1024,
                "avg_frame_rate": "30000/1001",
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "12.3456789"},
    }

    class Result:
        stdout = json.dumps(payload)

    monkeypatch.setattr("moon.media.probe.subprocess.run", lambda *args, **kwargs: Result())

    result = probe_media(source)

    assert result["orientation"] == "vertical"
    assert result["duration_seconds"] == 12.345679
    assert result["fps"] == 29.97003
    assert result["video_codec"] == "h264"
    assert result["audio_codec"] == "aac"
    assert result["has_audio"] is True


def test_probe_helpers_are_deterministic() -> None:
    assert _orientation(576, 1024) == "vertical"
    assert _orientation(1920, 1080) == "horizontal"
    assert _orientation(1000, 1000) == "square"
    assert _parse_rate("30/1") == 30.0
    assert _parse_rate("0/0") is None


def test_sample_timestamps_cover_requested_window_without_hitting_end() -> None:
    values = _sample_timestamps(120.0, 130.0, 3)

    assert values[0] == 120.0
    assert values[-1] < 130.0
    assert len(values) == 3


def test_inspect_footage_lists_supported_video_files(tmp_path, monkeypatch) -> None:
    project = MoonProject.open(tmp_path, create=True)
    footage = tmp_path / "footage"
    footage.mkdir()
    (footage / "b.mov").write_bytes(b"b")
    (footage / "a.mp4").write_bytes(b"a")
    (footage / "ignore.txt").write_text("x", encoding="utf-8")

    monkeypatch.setattr(
        "moon.media.inspection.probe_media",
        lambda path: {"path": str(path), "duration_seconds": 1.0},
    )

    result = inspect_footage(project)

    assert result["count"] == 2
    assert [Path(item["relative_path"]).name for item in result["items"]] == ["a.mp4", "b.mov"]


def test_protocol_exposes_media_inspection(tmp_path, monkeypatch) -> None:
    project = MoonProject.open(tmp_path, create=True)
    (tmp_path / "reference.mp4").write_bytes(b"ref")
    runner = PipelineRunner(project)
    protocol = MoonProtocol(runner)

    monkeypatch.setattr(
        "moon.protocol.inspect_reference",
        lambda project: {"role": "reference", "duration_seconds": 9.0},
    )

    response = protocol.handle({"action": "media.inspect.reference"})

    assert response == {"ok": True, "result": {"role": "reference", "duration_seconds": 9.0}}


def test_project_source_cannot_escape_project_root(tmp_path) -> None:
    project = MoonProject.open(tmp_path / "project", create=True)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")

    with pytest.raises(ValueError, match="inside the Moon project root"):
        resolve_project_source(project, outside)
