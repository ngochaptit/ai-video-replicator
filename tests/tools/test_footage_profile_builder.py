from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from tools.analysis.footage_profile_builder import FootageProfileBuilder
from tools.base_tool import ToolResult


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas" / "artifacts" / "footage_profiles.schema.json"


def _fake_video_analysis(source: str, output_dir: str) -> ToolResult:
    output = Path(output_dir)
    keyframes = output / "keyframes"
    keyframes.mkdir(parents=True, exist_ok=True)
    frame = keyframes / "frame.jpg"
    frame.write_bytes(b"jpg")
    brief_path = output / "video_analysis_brief.json"
    brief_path.write_text("{}\n", encoding="utf-8")
    return ToolResult(
        success=True,
        data={
            "source": {"duration_seconds": 4.0, "resolution": "1920x1080", "fps": 30.0},
            "structure_analysis": {
                "scenes": [{"scene_index": 0, "start_time": 0.0, "end_time": 4.0}]
            },
            "keyframes": [{"timestamp": 1.0, "path": str(frame)}],
        },
        artifacts=[str(brief_path)],
    )


def test_preprocessing_cache_hit_skips_video_analysis(tmp_path: Path, monkeypatch) -> None:
    footage = tmp_path / "footage"
    footage.mkdir()
    (footage / "a.mp4").write_bytes(b"source-v1")
    calls: list[str] = []

    def fake_execute(_self, inputs):
        calls.append(Path(inputs["source"]).name)
        return _fake_video_analysis(inputs["source"], inputs["output_dir"])

    monkeypatch.setattr("tools.analysis.video_analyzer.VideoAnalyzer.execute", fake_execute)
    inputs = {
        "footage_dir": str(footage),
        "output_dir": str(tmp_path / "analysis"),
        "preprocess_checkpoint_path": str(tmp_path / "checkpoint.json"),
    }

    assert FootageProfileBuilder().execute(inputs).success
    assert FootageProfileBuilder().execute(inputs).success

    assert calls == ["a.mp4"]
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["clips"][str((footage / "a.mp4").resolve())]["status"] == "completed"


def test_preprocessing_cache_invalidates_when_source_changes(tmp_path: Path, monkeypatch) -> None:
    footage = tmp_path / "footage"
    footage.mkdir()
    source = footage / "a.mp4"
    source.write_bytes(b"source-v1")
    calls: list[str] = []

    def fake_execute(_self, inputs):
        calls.append(Path(inputs["source"]).read_bytes().decode("ascii"))
        return _fake_video_analysis(inputs["source"], inputs["output_dir"])

    monkeypatch.setattr("tools.analysis.video_analyzer.VideoAnalyzer.execute", fake_execute)
    inputs = {
        "footage_dir": str(footage),
        "output_dir": str(tmp_path / "analysis"),
        "preprocess_checkpoint_path": str(tmp_path / "checkpoint.json"),
    }
    assert FootageProfileBuilder().execute(inputs).success
    source.write_bytes(b"source-v2")
    assert FootageProfileBuilder().execute(inputs).success

    assert calls == ["source-v1", "source-v2"]


def test_preprocessing_resumes_after_partial_clip_failure(tmp_path: Path, monkeypatch) -> None:
    footage = tmp_path / "footage"
    footage.mkdir()
    (footage / "a.mp4").write_bytes(b"a")
    (footage / "b.mp4").write_bytes(b"b")
    calls: list[str] = []

    def fake_execute(_self, inputs):
        name = Path(inputs["source"]).name
        calls.append(name)
        if name == "b.mp4" and calls.count(name) == 1:
            return ToolResult(success=False, error="interrupted")
        return _fake_video_analysis(inputs["source"], inputs["output_dir"])

    monkeypatch.setattr("tools.analysis.video_analyzer.VideoAnalyzer.execute", fake_execute)
    inputs = {
        "footage_dir": str(footage),
        "output_dir": str(tmp_path / "analysis"),
        "preprocess_checkpoint_path": str(tmp_path / "checkpoint.json"),
    }

    first = FootageProfileBuilder().execute(inputs)
    partial = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    second = FootageProfileBuilder().execute(inputs)

    assert first.success and len(first.data["clips"]) == 1
    assert sorted(entry["status"] for entry in partial["clips"].values()) == [
        "completed", "failed"
    ]
    assert second.success and len(second.data["clips"]) == 2
    assert calls == ["a.mp4", "b.mp4", "b.mp4"]


def test_long_footage_scene_is_only_sampling_scaffold() -> None:
    builder = FootageProfileBuilder()
    windows = builder._build_windows(
        [{"scene_index": 0, "start_time": 0.0, "end_time": 5.0}],
        duration=5.0,
        max_window=2.0,
    )

    assert [(start, end) for start, end, _, _ in windows] == [
        (0.0, 2.0),
        (2.0, 4.0),
        (4.0, 5.0),
    ]
    assert "scene_cut" in windows[0][3]
    assert "analysis_window" in windows[1][3]
    assert "video_end" in windows[-1][3]


def test_footage_evidence_never_reuses_frame_outside_window() -> None:
    builder = FootageProfileBuilder()
    evidence = builder._evidence_for_window(
        [
            {"timestamp": 0.5, "path": "before.jpg"},
            {"timestamp": 1.5, "path": "inside.jpg"},
            {"timestamp": 3.0, "path": "after.jpg"},
        ],
        1.0,
        2.0,
        0,
    )

    assert evidence["frame_paths"] == ["inside.jpg"]
    assert evidence["frame_timestamps"] == [1.5]


def test_semantic_enrichment_allows_gaps_and_requires_measured_boundaries() -> None:
    builder = FootageProfileBuilder()
    profiles = _profiles_scaffold()
    enrichment = _enrichment()

    enriched = builder.apply_semantic_enrichment(profiles, enrichment)

    segments = enriched["clips"][0]["segments"]
    assert [(segment["source_in"], segment["source_out"]) for segment in segments] == [
        (0.0, 1.0),
        (3.0, 4.0),
    ]
    assert enriched["analysis_meta"]["semantic_enrichment_required"] is False
    assert enriched["analysis_meta"]["usable_segment_count"] == 2
    assert all(
        segment["source_in"] <= timestamp <= segment["source_out"]
        for segment in segments
        for timestamp in segment["evidence"]["frame_timestamps"]
    )
    assert all(segment["motion"]["flow_variance"] is None for segment in segments)

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(instance=enriched, schema=schema)


def test_semantic_enrichment_rejects_unmeasured_boundary() -> None:
    builder = FootageProfileBuilder()
    enrichment = _enrichment()
    enrichment["clips"][0]["segments"][0]["source_out"] = 1.25

    with pytest.raises(ValueError, match="not a measured timestamp"):
        builder.apply_semantic_enrichment(_profiles_scaffold(), enrichment)


def test_semantic_enrichment_rejects_overlapping_usable_segments() -> None:
    builder = FootageProfileBuilder()
    enrichment = _enrichment()
    enrichment["evidence_catalog"].append(
        {"clip_id": "clip_001", "path": "at_0_5.jpg", "timestamp": 0.5, "scene_index": 0}
    )
    enrichment["clips"][0]["segments"][1]["source_in"] = 0.5

    with pytest.raises(ValueError, match="overlaps"):
        builder.apply_semantic_enrichment(_profiles_scaffold(), enrichment)


def test_negative_motion_sentinel_becomes_null() -> None:
    builder = FootageProfileBuilder()

    assert builder._normalise_flow_variance(-1) is None
    assert builder._normalise_flow_variance("-1") is None
    assert builder._normalise_flow_variance(0) == 0.0


def _profiles_scaffold() -> dict:
    def scaffold_segment(segment_id: str, start: float, end: float, timestamp: float) -> dict:
        return {
            "id": segment_id,
            "source_in": start,
            "source_out": end,
            "duration_seconds": end - start,
            "boundary_basis": ["scene_cut" if start == 0 else "analysis_window"],
            "semantic": _semantic("placeholder"),
            "camera": _camera(),
            "spatial": _spatial(),
            "motion": {"motion_type": "unknown", "intensity": None, "flow_variance": -1, "speed_behavior": None},
            "quality": {"score": None, "issues": [], "usable_notes": ""},
            "evidence": {"scene_index": 0, "frame_paths": [f"frame_{timestamp}.jpg"], "frame_timestamps": [timestamp]},
            "confidence": {"timing": 1.0, "semantic": None, "camera": None, "overall": None},
        }

    return {
        "version": "1.0",
        "source_dir": "footage",
        "clips": [
            {
                "clip_id": "clip_001",
                "path": "footage/a.mp4",
                "duration_seconds": 4.0,
                "resolution": "1080x1920",
                "fps": 30.0,
                "orientation": "vertical",
                "usable": True,
                "content_summary": "",
                "quality_risks": [],
                "segments": [
                    scaffold_segment("clip_001_seg_001", 0.0, 2.0, 0.5),
                    scaffold_segment("clip_001_seg_002", 2.0, 4.0, 3.5),
                ],
                "_analysis_path": "analysis/clip_001/video_analysis_brief.json",
            }
        ],
        "analysis_meta": {
            "generated_by": "footage_profile_builder",
            "semantic_enrichment_required": True,
            "file_count": 1,
            "usable_segment_count": 2,
            "notes": [],
        },
    }


def _enrichment() -> dict:
    return {
        "defaults": {
            "camera": _camera(),
            "spatial": _spatial(),
            "quality": {"score": 0.8, "issues": [], "usable_notes": "clear"},
            "confidence": {"timing": 1.0, "semantic": 0.9, "camera": 0.9, "overall": 0.9},
        },
        "evidence_catalog": [
            {"clip_id": "clip_001", "path": "at_1.jpg", "timestamp": 1.0, "scene_index": 0},
            {"clip_id": "clip_001", "path": "at_3.jpg", "timestamp": 3.0, "scene_index": 0},
            {"clip_id": "clip_001", "path": "at_4.jpg", "timestamp": 4.0, "scene_index": 0},
        ],
        "clips": [
            {
                "clip_id": "clip_001",
                "usable": True,
                "content_summary": "Two usable actions with dead time between them.",
                "quality_risks": [],
                "segments": [
                    {
                        "id": "clip_001_action_01",
                        "source_in": 0.0,
                        "source_out": 1.0,
                        "boundary_basis": ["action_change"],
                        "semantic": _semantic("reach"),
                        "motion": {"intensity": "medium", "speed_behavior": "quick reach"},
                    },
                    {
                        "id": "clip_001_action_02",
                        "source_in": 3.0,
                        "source_out": 4.0,
                        "boundary_basis": ["action_change", "video_end"],
                        "semantic": _semantic("pour"),
                        "motion": {"intensity": "medium", "speed_behavior": "steady pour"},
                    },
                ],
            }
        ],
        "analysis_notes": [],
    }


def _semantic(action: str) -> dict:
    return {
        "actor": "hand",
        "action": action,
        "object": "cup",
        "target": "table",
        "interaction": "hand-cup",
        "description": f"Hand performs {action}.",
    }


def _camera() -> dict:
    return {
        "pov": "first-person",
        "shot_scale": "close",
        "angle": "high",
        "movement": "follow",
        "steadiness": "handheld",
    }


def _spatial() -> dict:
    return {
        "actor_position": "foreground",
        "object_position": "center",
        "entry_direction": None,
        "exit_direction": None,
        "depth": "foreground",
        "framing_notes": "centered",
    }
