from __future__ import annotations

import json
from pathlib import Path

import jsonschema

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
