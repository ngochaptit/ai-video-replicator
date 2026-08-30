from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from tools.analysis.reference_blueprint_builder import ReferenceBlueprintBuilder


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas" / "artifacts" / "reference_blueprint.schema.json"


def test_long_scene_is_split_into_analysis_windows() -> None:
    builder = ReferenceBlueprintBuilder()
    scenes = [
        {
            "scene_index": 0,
            "start_time": 0.0,
            "end_time": 5.0,
            "motion_type": "motion_clip",
            "flow_variance": 2.2,
        }
    ]

    windows = builder._build_windows(scenes, duration=5.0, max_window=2.0)

    assert [(round(start, 1), round(end, 1)) for start, end, _, _ in windows] == [
        (0.0, 2.0),
        (2.0, 4.0),
        (4.0, 5.0),
    ]
    assert "scene_cut" in windows[0][3]
    assert "analysis_window" in windows[1][3]
    assert "video_end" in windows[-1][3]


def test_no_scene_fallback_still_covers_full_duration() -> None:
    builder = ReferenceBlueprintBuilder()

    windows = builder._build_windows([], duration=4.5, max_window=2.0)

    assert windows[0][0] == 0.0
    assert windows[-1][1] == 4.5
    assert sum(end - start for start, end, _, _ in windows) == 4.5


def test_last_scene_is_clamped_to_measured_source_duration() -> None:
    builder = ReferenceBlueprintBuilder()
    scenes = [{"scene_index": 0, "start_time": 0.0, "end_time": 4.999}]

    windows = builder._build_windows(scenes, duration=5.0, max_window=2.0)

    assert windows[-1][1] == 5.0
    assert "video_end" in windows[-1][3]


def test_evidence_prefers_frames_inside_window() -> None:
    builder = ReferenceBlueprintBuilder()
    keyframes = [
        {"timestamp": 0.5, "path": "a.jpg"},
        {"timestamp": 1.5, "path": "b.jpg"},
        {"timestamp": 3.0, "path": "c.jpg"},
    ]

    evidence = builder._evidence_for_window(keyframes, 1.0, 2.0, 7)

    assert evidence == {
        "scene_index": 7,
        "frame_paths": ["b.jpg"],
        "frame_timestamps": [1.5],
    }


def test_evidence_does_not_reuse_nearest_frame_outside_window() -> None:
    builder = ReferenceBlueprintBuilder()
    keyframes = [
        {"timestamp": 0.5, "path": "before.jpg"},
        {"timestamp": 3.0, "path": "after.jpg"},
    ]

    evidence = builder._evidence_for_window(keyframes, 1.0, 2.0, 7)

    assert evidence == {
        "scene_index": 7,
        "frame_paths": [],
        "frame_timestamps": [],
    }


def test_negative_flow_variance_is_unavailable() -> None:
    builder = ReferenceBlueprintBuilder()

    assert builder._normalise_flow_variance(-1) is None
    assert builder._normalise_flow_variance("-1") is None
    assert builder._normalise_flow_variance(0) == 0.0


def test_semantic_enrichment_refines_measured_boundaries_and_preserves_utf8(tmp_path: Path) -> None:
    builder = ReferenceBlueprintBuilder()
    blueprint = _two_window_scaffold()
    caption = "Pov: Khi pha mỗi ngày hơn 50 ly, có vẻ nước lọc là ngon nhất đối với Barista"
    enrichment = {
        "defaults": {
            "camera": {
                "pov": "first-person",
                "shot_scale": "CU",
                "angle": "high",
                "movement": "follow",
                "steadiness": "handheld",
                "playback_speed": "real-time",
            },
            "spatial": {
                "actor_position": "foreground",
                "object_position": "center",
                "entry_direction": None,
                "exit_direction": None,
                "depth": "foreground",
                "framing_notes": "centered",
            },
            "motion": {"intensity": "medium", "speed_behavior": "steady"},
            "edit": {"transition_in": None, "transition_out": None, "segment_role": "action"},
            "text": {"content": caption, "position": "upper-center", "timing_notes": "persistent"},
            "audio": {"speech": None, "sound_cue": None, "beat_cue": None, "energy_notes": "unavailable"},
            "confidence": {"timing": 1.0, "semantic": 0.9, "camera": 0.9, "overall": 0.9},
        },
        "evidence_catalog": [
            {"path": "at_1.jpg", "timestamp": 1.0},
            {"path": "at_3.jpg", "timestamp": 3.0},
        ],
        "segments": [
            {
                "id": "seg_001",
                "start_seconds": 0.0,
                "end_seconds": 1.0,
                "boundary_basis": ["scene_cut"],
                "semantic": _semantic("prepare"),
            },
            {
                "id": "seg_002",
                "start_seconds": 1.0,
                "end_seconds": 3.0,
                "boundary_basis": ["action_change"],
                "semantic": _semantic("pour"),
            },
            {
                "id": "seg_003",
                "start_seconds": 3.0,
                "end_seconds": 4.0,
                "boundary_basis": ["interaction_change", "video_end"],
                "semantic": _semantic("present"),
            },
        ],
        "choreography": {
            "summary": "prepare, pour, present",
            "action_order": ["seg_001: prepare", "seg_002: pour", "seg_003: present"],
            "critical_constraints": ["order"],
            "soft_constraints": ["crop"],
        },
    }

    enriched = builder.apply_semantic_enrichment(blueprint, enrichment)

    assert [(segment["start_seconds"], segment["end_seconds"]) for segment in enriched["segments"]] == [
        (0.0, 1.0),
        (1.0, 3.0),
        (3.0, 4.0),
    ]
    assert sum(
        bool({"action_change", "interaction_change"}.intersection(segment["boundary_basis"]))
        for segment in enriched["segments"][1:]
    ) == 2
    assert all(
        segment["start_seconds"] <= timestamp <= segment["end_seconds"]
        for segment in enriched["segments"]
        for timestamp in segment["evidence"]["frame_timestamps"]
    )
    assert all(segment["motion"]["flow_variance"] is None for segment in enriched["segments"])
    assert enriched["segments"][0]["text"]["content"] == caption
    assert enriched["analysis_meta"]["semantic_enrichment_required"] is False

    output_path = tmp_path / "reference_blueprint.json"
    builder._write_json_utf8(output_path, enriched)
    raw = output_path.read_bytes()
    assert caption.encode("utf-8") in raw
    assert json.loads(raw.decode("utf-8"))["segments"][0]["text"]["content"] == caption

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(instance=enriched, schema=schema)


def test_semantic_enrichment_rejects_unmeasured_boundary() -> None:
    builder = ReferenceBlueprintBuilder()
    enrichment = {
        "evidence_catalog": [{"path": "at_1.jpg", "timestamp": 1.0}],
        "segments": [
            {
                "start_seconds": 0.0,
                "end_seconds": 1.5,
                "boundary_basis": ["scene_cut"],
                "semantic": _semantic("prepare"),
            },
            {
                "start_seconds": 1.5,
                "end_seconds": 4.0,
                "boundary_basis": ["action_change", "video_end"],
                "semantic": _semantic("present"),
            },
        ],
    }

    with pytest.raises(ValueError, match="not a measured timestamp"):
        builder.apply_semantic_enrichment(_two_window_scaffold(), enrichment)


def test_blueprint_invariant_rejects_out_of_segment_evidence() -> None:
    builder = ReferenceBlueprintBuilder()
    blueprint = _two_window_scaffold()
    blueprint["segments"][0]["evidence"] = {
        "scene_index": 0,
        "frame_paths": ["outside.jpg"],
        "frame_timestamps": [2.5],
    }

    with pytest.raises(ValueError, match="out-of-range evidence"):
        builder._validate_blueprint_invariants(blueprint)


def _semantic(action: str) -> dict[str, str | None]:
    return {
        "actor": "barista",
        "action": action,
        "object": "drink",
        "target": "viewer",
        "interaction": "hand-object",
        "description": f"Barista performs {action}.",
    }


def _two_window_scaffold() -> dict:
    def segment(segment_id: str, start: float, end: float, timestamp: float) -> dict:
        return {
            "id": segment_id,
            "start_seconds": start,
            "end_seconds": end,
            "duration_seconds": end - start,
            "boundary_basis": ["scene_cut" if start == 0 else "analysis_window"],
            "semantic": _semantic("placeholder"),
            "camera": {"pov": None, "shot_scale": None, "angle": None, "movement": None, "steadiness": None, "playback_speed": None},
            "spatial": {"actor_position": None, "object_position": None, "entry_direction": None, "exit_direction": None, "depth": None, "framing_notes": ""},
            "motion": {"motion_type": "unknown", "intensity": None, "flow_variance": -1, "speed_behavior": None},
            "edit": {"transition_in": None, "transition_out": None, "segment_role": None},
            "text": {"content": None, "position": None, "timing_notes": ""},
            "audio": {"speech": None, "sound_cue": None, "beat_cue": None, "energy_notes": ""},
            "evidence": {"scene_index": 0, "frame_paths": [f"at_{timestamp:g}.jpg"], "frame_timestamps": [timestamp]},
            "confidence": {"timing": 1.0, "semantic": None, "camera": None, "overall": None},
        }

    return {
        "version": "1.0",
        "source": {"path": "reference.mp4", "duration_seconds": 4.0, "resolution": "", "fps": 0, "orientation": "unknown"},
        "segments": [segment("seg_001", 0.0, 2.0, 0.0), segment("seg_002", 2.0, 4.0, 4.0)],
        "choreography": {"summary": "", "action_order": [], "critical_constraints": [], "soft_constraints": []},
        "analysis_meta": {"generated_by": "reference_blueprint_builder", "semantic_enrichment_required": True, "source_analysis_path": "brief.json", "notes": []},
    }


def test_minimal_scaffold_shape_validates_against_schema() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    scaffold = {
        "version": "1.0",
        "source": {
            "path": "reference.mp4",
            "duration_seconds": 2.0,
            "resolution": "1080x1920",
            "fps": 0,
            "orientation": "unknown",
        },
        "segments": [
            {
                "id": "seg_001",
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "duration_seconds": 2.0,
                "boundary_basis": ["analysis_window", "video_end"],
                "semantic": {"actor": None, "action": None, "object": None, "target": None, "interaction": None, "description": ""},
                "camera": {"pov": None, "shot_scale": None, "angle": None, "movement": None, "steadiness": None, "playback_speed": None},
                "spatial": {"actor_position": None, "object_position": None, "entry_direction": None, "exit_direction": None, "depth": None, "framing_notes": ""},
                "motion": {"motion_type": None, "intensity": None, "flow_variance": None, "speed_behavior": None},
                "edit": {"transition_in": None, "transition_out": None, "segment_role": None},
                "text": {"content": None, "position": None, "timing_notes": ""},
                "audio": {"speech": None, "sound_cue": None, "beat_cue": None, "energy_notes": ""},
                "evidence": {"scene_index": None, "frame_paths": [], "frame_timestamps": []},
                "confidence": {"timing": 1.0, "semantic": None, "camera": None, "overall": None},
            }
        ],
        "choreography": {"summary": "", "action_order": [], "critical_constraints": [], "soft_constraints": []},
        "analysis_meta": {
            "generated_by": "reference_blueprint_builder",
            "semantic_enrichment_required": True,
            "source_analysis_path": "source_analysis/video_analysis_brief.json",
            "notes": [],
        },
    }

    jsonschema.validate(instance=scaffold, schema=schema)
