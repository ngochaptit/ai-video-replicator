from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from tools.analysis.replication_quality_evaluator import ReplicationQualityEvaluator


ROOT = Path(__file__).resolve().parents[2]


def test_healthy_matching_with_moderate_speed_and_low_fallback_passes() -> None:
    report = _evaluate(_timeline([0, 2, 4, 6, 8]))

    assert report["render_integrity"]["status"] == "pass"
    assert report["quality_gate"] == "pass"
    assert report["speed"]["normal_count"] == 5
    assert report["fallback_ratio"] == 0.0


def test_technically_valid_extreme_speed_fails_replication_quality() -> None:
    timeline = _timeline([0])
    segment = timeline["segments"][0]
    segment["source"]["out_seconds"] = 20.0
    segment["source"]["duration_seconds"] = 20.0
    segment["timing_fit"]["speed"] = 20.0

    report = _evaluate(timeline)

    assert report["render_integrity"]["status"] == "pass"
    assert report["quality_gate"] == "fail"
    assert report["speed"]["invalid_count"] == 1
    assert report["decisions"][0]["effective_speed_factor"] == 20.0
    assert report["decisions"][0]["speed_severity"] == "invalid"


def test_full_fallback_timeline_cannot_pass_quality_gate() -> None:
    timeline = _timeline([0, 2, 4, 6])
    for segment in timeline["segments"]:
        segment["match"]["class"] = "fallback"

    report = _evaluate(timeline)

    assert report["fallback_count"] == 4
    assert report["fallback_ratio"] == 1.0
    assert report["quality_gate"] == "fail"
    assert report["replication_quality"]["source_limited"] is True
    assert report["replication_quality"]["recommended_route"] == "footage"


def test_dominant_repeated_source_segment_is_a_severe_failure() -> None:
    timeline = _timeline([0] * 20 + [2, 4, 6])
    for index, segment in enumerate(timeline["segments"]):
        if index < 20:
            segment["source"]["footage_segment_id"] = "clip_001_dominant"
        else:
            segment["source"]["footage_segment_id"] = f"clip_001_unique_{index}"

    report = _evaluate(timeline)

    assert report["decision_count"] == 23
    assert report["max_reuse_count"] == 20
    assert report["reuse_ratio"] == round(19 / 23, 6)
    assert report["dominant_source_share"] == round(20 / 23, 6)
    assert report["overlap_reuse_count"] >= 19
    assert report["quality_gate"] == "fail"
    assert any(item["code"] == "source_max_reuse_count_fail" for item in report["quality_flags"])


def test_legitimate_limited_reuse_does_not_fail() -> None:
    timeline = _timeline([0, 0, 2, 4, 6])
    timeline["segments"][1]["source"]["footage_segment_id"] = timeline["segments"][0]["source"]["footage_segment_id"]

    report = _evaluate(timeline)

    assert report["max_reuse_count"] == 2
    assert report["reuse_ratio"] == 0.2
    assert report["overlap_reuse_count"] == 1
    assert report["quality_gate"] == "pass"


def test_minor_creative_reorder_remains_acceptable() -> None:
    report = _evaluate(_timeline([0, 10, 8, 20]))

    assert report["chronology"]["backward_jump_count"] == 1
    assert report["chronology"]["large_backward_jump_count"] == 0
    assert report["chronology"]["chronology_consistency_score"] >= 0.8
    assert not any(item["code"].startswith("chronology_") for item in report["quality_flags"])
    assert report["quality_gate"] == "pass"


def test_repeated_large_chronology_resets_fail() -> None:
    report = _evaluate(_timeline([100, 0, 100, 0, 100]))

    assert report["chronology"]["large_backward_jump_count"] == 2
    assert report["quality_gate"] == "fail"
    assert any(item["code"] == "chronology_large_backward_jumps_fail" for item in report["quality_flags"])


def test_report_validates_against_schema() -> None:
    report = _evaluate(_timeline([0, 2, 4]))
    schema = json.loads((ROOT / "schemas" / "artifacts" / "replication_quality_report.schema.json").read_text(encoding="utf-8"))

    jsonschema.validate(instance=report, schema=schema)


def _evaluate(timeline: dict) -> dict:
    return ReplicationQualityEvaluator().build_report(
        timeline,
        {
            "output": "draft.mp4",
            "tool_result": {
                "duration_seconds": float(len(timeline["segments"])),
                "expected_duration_seconds": float(len(timeline["segments"])),
                "duration_delta_seconds": 0.0,
            },
        },
        draft_exists=True,
    )


def _timeline(source_positions: list[float]) -> dict:
    segments = []
    for index, source_in in enumerate(source_positions, start=1):
        source_out = source_in + 1.0
        segments.append(
            {
                "id": f"timeline_{index:03d}",
                "order": index,
                "reference_segment_id": f"seg_{index:03d}",
                "timeline_start": float(index - 1),
                "timeline_end": float(index),
                "target_duration_seconds": 1.0,
                "source": {
                    "path": "footage/clip_001.mp4",
                    "footage_segment_id": f"clip_001_seg_{index:03d}",
                    "in_seconds": float(source_in),
                    "out_seconds": float(source_out),
                    "duration_seconds": 1.0,
                },
                "timing_fit": {"mode": "speed_fit", "speed": 1.0, "hold_seconds": 0.0, "extreme_speed": False},
                "match": {"class": "good", "overall_score": 0.9, "rationale": "test", "tradeoffs": []},
                "reference_cues": {"transition_in": None, "transition_out": None, "camera": {}, "spatial": {}, "text": {}, "audio": {}},
                "quality_risks": [],
            }
        )
    return {
        "version": "1.0",
        "reference_duration_seconds": float(len(segments)),
        "segments": segments,
        "coverage": {"segment_count": len(segments), "full_coverage": True, "timeline_contiguous": True, "fallback_count": 0, "extreme_speed_count": 0, "hold_segment_count": 0},
        "warnings": [],
        "metadata": {"generated_by": "test", "reference_blueprint_path": "blueprint.json", "reference_matching_path": "matching.json", "render_runtime_locked": False},
    }
