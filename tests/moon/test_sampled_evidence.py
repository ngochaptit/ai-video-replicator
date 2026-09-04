from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from moon.connector import AgentConnectorService
from moon.core.project import MoonProject
from moon.evidence import SampledFrameEvidenceStore
from moon.handoff import AgentHandoffService
from moon.mcp_adapter import MoonMCPServer
from moon.runner.pipeline import PipelineRunner


def _footage_runner(tmp_path: Path) -> tuple[PipelineRunner, Path, Path]:
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))
    runner.complete("proposal", {"test": True})
    runner.complete("analyze", {"test": True})

    source = tmp_path / "footage" / "clip.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fake-video")
    deterministic = tmp_path / "analysis" / "footage"
    deterministic.mkdir(parents=True)
    (deterministic / "frame_0000.jpg").write_bytes(b"deterministic-frame")
    (deterministic / "brief.json").write_text('{"kind":"deterministic"}', encoding="utf-8")
    runner.artifacts.write(
        "footage_profiles_scaffold",
        {
            "clips": [
                {
                    "clip_id": "clip_001",
                    "path": "footage/clip.mp4",
                    "duration_seconds": 4.0,
                    "segments": [
                        {
                            "source_in": 0.0,
                            "boundary_basis": ["scene_cut"],
                            "evidence": {"frame_timestamps": [0.5]},
                        }
                    ],
                }
            ]
        },
    )
    runner.artifacts.write(
        "footage_agent_task",
        {"stage": "footage", "evidence_root": str(deterministic)},
    )
    return runner, source, deterministic


def _fake_sample_frames(
    source: str | Path,
    output_dir: str | Path,
    *,
    start_seconds: float,
    end_seconds: float,
    count: int,
    width: int,
) -> dict:
    assert count == 3
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    timestamps = [round(start_seconds, 6), 2.0, round(end_seconds - 0.001, 6)]
    frames = []
    for index, timestamp in enumerate(timestamps, start=1):
        path = output / f"frame_{index:03d}_{timestamp:.3f}s.jpg"
        path.write_bytes(f"sample-{timestamp}".encode())
        frames.append({"timestamp_seconds": timestamp, "path": str(path.resolve())})
    return {
        "source": str(Path(source).resolve()),
        "start_seconds": round(start_seconds, 6),
        "end_seconds": round(end_seconds, 6),
        "window_seconds": round(end_seconds - start_seconds, 6),
        "count": len(frames),
        "width": width,
        "frames": frames,
    }


def _sample(service: AgentConnectorService) -> dict:
    return service.call(
        {
            "tool": "moon.frames.sample",
            "arguments": {
                "source": "footage/clip.mp4",
                "start_seconds": 0.0,
                "end_seconds": 4.0,
                "count": 3,
                "width": 320,
            },
        }
    )


def test_sampled_evidence_survives_restart_and_supports_semantic_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runner, source, _ = _footage_runner(tmp_path)
    monkeypatch.setattr("moon.connector.sample_frames", _fake_sample_frames)
    sampled = _sample(AgentConnectorService(runner))

    assert sampled["evidence_registered"] is True
    assert sampled["stage"] == "footage"
    assert sampled["pipeline_revision"] == 0
    assert sampled["provenance"]["source"] == {
        "clip_id": "clip_001",
        "path": "footage/clip.mp4",
    }
    assert [item["timestamp_seconds"] for item in sampled["provenance"]["frames"]] == [
        0.0,
        2.0,
        3.999,
    ]

    registry = Path(sampled["evidence_registry_path"])
    raw_registry = registry.read_text(encoding="utf-8")
    event = json.loads(raw_registry)
    assert event["request"] == {
        "count": 3,
        "end_seconds": 4.0,
        "start_seconds": 0.0,
        "width": 320,
    }
    assert event["sampling_method"] == "ffmpeg_single_frame_seek_v1"
    assert "base64" not in raw_registry.lower()
    assert not Path(event["source"]["path"]).is_absolute()
    assert all(not Path(item["path"]).is_absolute() for item in event["frames"])

    restarted_runner = PipelineRunner(MoonProject.open(tmp_path))
    restarted = AgentConnectorService(restarted_runner)
    listed = restarted.call({"tool": "moon.evidence.list", "arguments": {}})
    sampled_files = [item for item in listed["files"] if item["kind"] == "sampled_frame"]
    assert listed["sampled_frame_count"] == 3
    assert [item["timestamp_seconds"] for item in sampled_files] == [0.0, 2.0, 3.999]
    assert {item["sampling_group_id"] for item in sampled_files} == {
        sampled["sampling_group_id"]
    }
    assert all(item["source"]["clip_id"] == "clip_001" for item in sampled_files)

    frame = sampled_files[1]
    mcp_result = MoonMCPServer(restarted).handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "moon.evidence.read_image",
                "arguments": {"path": frame["path"]},
            },
        }
    )["result"]
    assert mcp_result["content"][1]["type"] == "image"
    assert base64.b64decode(mcp_result["content"][1]["data"]) == b"sample-2.0"
    assert "data_base64" not in mcp_result["structuredContent"]

    handoff = AgentHandoffService(restarted_runner).package()
    handoff_samples = handoff["inputs"]["evidence"]["sampled_frames"]
    assert handoff_samples["frame_count"] == 3
    assert handoff_samples["groups"][0]["group_id"] == sampled["sampling_group_id"]
    assert str(registry.resolve()) in handoff["inputs"]["evidence"]["files"]
    assert all(
        item["absolute_path"] in handoff["inputs"]["evidence"]["files"]
        for item in handoff_samples["groups"][0]["frames"]
    )

    payload = {
        "clips": [
            {
                "clip_id": "clip_001",
                "path": "footage/clip.mp4",
                "segments": [
                    {"source_in": 0.0, "source_out": 2.0, "boundary_basis": ["sampled_frame"]},
                    {"source_in": 2.0, "source_out": 4.0, "boundary_basis": ["sampled_frame", "video_end"]},
                ],
            }
        ]
    }
    assert AgentHandoffService(restarted_runner).submit("footage", payload)["accepted"] is True
    assert source.is_file()


def test_sampled_evidence_is_stage_scoped_and_clear_is_append_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runner, source, deterministic = _footage_runner(tmp_path)
    monkeypatch.setattr("moon.connector.sample_frames", _fake_sample_frames)
    service = AgentConnectorService(runner)
    sampled = _sample(service)

    unrelated_frame = tmp_path / ".moon" / "cache" / "unrelated.jpg"
    unrelated_frame.write_bytes(b"unrelated")
    store = SampledFrameEvidenceStore(runner.project, runner.state.revision)
    unrelated_result = {
        "source": str(source),
        "start_seconds": 0.0,
        "end_seconds": 1.0,
        "count": 1,
        "width": 320,
        "frames": [{"timestamp_seconds": 0.5, "path": str(unrelated_frame)}],
    }
    store.register("match", unrelated_result, group_id="unrelated-stage", clip_id="clip_001")

    legacy = tmp_path / ".moon" / "cache" / "connector-frames" / "legacy.jpg"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"legacy-unregistered")
    before = service.call({"tool": "moon.evidence.list", "arguments": {}})
    assert before["sampled_frame_count"] == 3
    assert "unrelated-stage" not in json.dumps(before)
    assert str(legacy) not in json.dumps(before)

    cleared = service.call({"tool": "moon.evidence.clear_sampled", "arguments": {}})
    assert cleared["cleared_groups"] == 1
    assert cleared["cleared_frames"] == 3
    assert cleared["images_deleted"] is False
    assert all(Path(item["path"]).is_file() for item in sampled["frames"])
    assert (deterministic / "frame_0000.jpg").is_file()

    restarted = AgentConnectorService(PipelineRunner(MoonProject.open(tmp_path)))
    after = restarted.call({"tool": "moon.evidence.list", "arguments": {}})
    assert after["sampled_frame_count"] == 0
    assert any(Path(item["absolute_path"]) == deterministic / "frame_0000.jpg" for item in after["files"])
    assert all(item["kind"] != "sampled_frame" for item in after["files"])
    events = [
        json.loads(line)
        for line in Path(cleared["registry_path"]).read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event_type"] for event in events] == [
        "sampled_frame_group",
        "clear_sampled_frames",
    ]
    assert events[1]["cleared_group_ids"] == [sampled["sampling_group_id"]]

    restarted_sample = _sample(restarted)
    assert restarted_sample["sampling_group_id"] == sampled["sampling_group_id"]
    resumed = restarted.call({"tool": "moon.evidence.list", "arguments": {}})
    assert resumed["sampled_frame_count"] == 3
    resumed_events = [
        json.loads(line)
        for line in Path(cleared["registry_path"]).read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event_type"] for event in resumed_events] == [
        "sampled_frame_group",
        "clear_sampled_frames",
        "sampled_frame_group",
    ]


def test_sampled_evidence_registration_rejects_paths_outside_project(tmp_path: Path):
    runner, source, _ = _footage_runner(tmp_path)
    store = SampledFrameEvidenceStore(runner.project, runner.state.revision)
    outside = tmp_path.parent / "outside-sampled-frame.jpg"
    outside.write_bytes(b"outside")
    result = {
        "source": str(source),
        "start_seconds": 0.0,
        "end_seconds": 1.0,
        "count": 1,
        "width": 320,
        "frames": [{"timestamp_seconds": 0.5, "path": str(outside)}],
    }
    with pytest.raises(ValueError, match="inside project root"):
        store.register("footage", result, group_id="outside", clip_id="clip_001")
