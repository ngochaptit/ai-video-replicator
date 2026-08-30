from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from tools.video.reference_timeline_builder import ReferenceTimelineBuilder


ROOT = Path(__file__).resolve().parents[2]


def test_builds_full_runtime_agnostic_timeline_in_reference_order() -> None:
    builder = ReferenceTimelineBuilder()
    matching = _matching()
    matching["matches"] = list(reversed(matching["matches"]))

    timeline = builder.build_timeline(_blueprint(), matching)

    assert [item["reference_segment_id"] for item in timeline["segments"]] == ["seg_001", "seg_002"]
    assert [item["timeline_start"] for item in timeline["segments"]] == [0.0, 2.0]
    assert timeline["segments"][0]["timing_fit"]["speed"] == 1.0
    assert timeline["segments"][1]["timing_fit"]["speed"] == 0.5
    assert timeline["coverage"]["full_coverage"] is True
    assert timeline["coverage"]["timeline_contiguous"] is True
    assert timeline["coverage"]["fallback_count"] == 1
    assert timeline["metadata"]["render_runtime_locked"] is False


def test_output_validates_against_replication_timeline_schema() -> None:
    timeline = ReferenceTimelineBuilder().build_timeline(_blueprint(), _matching())
    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "replication_timeline.schema.json").read_text(encoding="utf-8")
    )

    jsonschema.validate(instance=timeline, schema=schema)


def test_very_short_source_uses_hold_instead_of_creating_timeline_gap() -> None:
    matching = _matching()
    selected = matching["matches"][1]["selected"]
    selected["source_in"] = 4.0
    selected["source_out"] = 4.1
    selected["duration_seconds"] = 0.1

    timeline = ReferenceTimelineBuilder().build_timeline(_blueprint(), matching)
    segment = timeline["segments"][1]

    assert segment["timing_fit"]["mode"] == "speed_fit_with_hold"
    assert segment["timing_fit"]["speed"] == 0.1
    assert segment["timing_fit"]["hold_seconds"] == 1.0
    assert timeline["coverage"]["hold_segment_count"] == 1
    assert timeline["coverage"]["full_coverage"] is True


def test_missing_phase2_match_is_rejected() -> None:
    matching = _matching()
    matching["matches"] = matching["matches"][:1]
    matching["coverage"]["matched_segment_count"] = 1

    with pytest.raises(ValueError, match="coverage mismatch"):
        ReferenceTimelineBuilder().build_timeline(_blueprint(), matching)


def test_noncontiguous_reference_blueprint_is_rejected() -> None:
    blueprint = _blueprint()
    blueprint["segments"][1]["start_seconds"] = 2.2

    with pytest.raises(ValueError, match="not contiguous"):
        ReferenceTimelineBuilder().build_timeline(blueprint, _matching())


def _blueprint() -> dict:
    return {
        "version": "1.0",
        "source": {"path": "reference.mp4", "duration_seconds": 4.0},
        "segments": [
            {
                "id": "seg_001",
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "duration_seconds": 2.0,
                "edit": {"transition_in": "cut", "transition_out": "cut", "segment_role": "setup"},
                "camera": {"pov": "first_person"},
                "spatial": {"framing_notes": "centered"},
                "text": {"content": "", "position": None, "timing_notes": ""},
                "audio": {"speech": None, "sound_cue": "ice", "beat_cue": None, "energy_notes": ""},
            },
            {
                "id": "seg_002",
                "start_seconds": 2.0,
                "end_seconds": 4.0,
                "duration_seconds": 2.0,
                "edit": {"transition_in": "cut", "transition_out": "cut", "segment_role": "action"},
                "camera": {"pov": "first_person"},
                "spatial": {"framing_notes": "centered"},
                "text": {"content": "", "position": None, "timing_notes": ""},
                "audio": {"speech": None, "sound_cue": "pour", "beat_cue": None, "energy_notes": ""},
            },
        ],
    }


def _matching() -> dict:
    return {
        "version": "1.0",
        "reference_blueprint_path": "reference_blueprint.json",
        "footage_profiles_path": "footage_profiles.json",
        "matches": [
            {
                "reference_segment_id": "seg_001",
                "selected": {
                    "footage_segment_id": "clip_001_action",
                    "source_path": "footage/a.mp4",
                    "source_in": 1.0,
                    "source_out": 3.0,
                    "duration_seconds": 2.0,
                },
                "match_class": "good",
                "scores": _scores(0.9),
                "rationale": "Strong action fit.",
                "tradeoffs": [],
                "alternatives": [],
            },
            {
                "reference_segment_id": "seg_002",
                "selected": {
                    "footage_segment_id": "clip_002_fallback",
                    "source_path": "footage/b.mp4",
                    "source_in": 4.0,
                    "source_out": 5.0,
                    "duration_seconds": 1.0,
                },
                "match_class": "fallback",
                "scores": _scores(0.4),
                "rationale": "Best available footage keeps the edit complete.",
                "tradeoffs": ["action mismatch"],
                "alternatives": [],
            },
        ],
        "coverage": {
            "reference_segment_count": 2,
            "matched_segment_count": 2,
            "full_coverage": True,
            "fallback_count": 1,
        },
        "improvement_requests": [
            {
                "reference_segment_id": "seg_002",
                "reason": "Action mismatch.",
                "suggested_footage": "Add a closer action take.",
            }
        ],
        "notes": [],
    }


def _scores(overall: float) -> dict:
    return {
        "action": overall,
        "interaction": overall,
        "camera": overall,
        "spatial": overall,
        "motion": overall,
        "duration": overall,
        "overall": overall,
    }
