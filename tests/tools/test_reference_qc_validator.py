from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from tools.analysis.reference_qc_validator import ReferenceQCValidator


ROOT = Path(__file__).resolve().parents[2]


def test_pass_requires_separate_fidelity_and_quality_thresholds() -> None:
    qc = ReferenceQCValidator().build_qc(
        _blueprint(),
        _evidence(),
        _review(status="pass", fidelity=0.90, quality=0.88),
    )

    assert qc["status"] == "pass"
    assert qc["scores"]["fidelity_score"] == 0.9
    assert qc["scores"]["quality_score"] == 0.88
    assert qc["final_decision"] == {
        "publishable": True,
        "requires_rerender": False,
        "reason": "Meets fidelity and quality gates.",
    }

    schema = json.loads((ROOT / "schemas" / "artifacts" / "replication_qc.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(instance=qc, schema=schema)


def test_pass_below_fidelity_threshold_is_rejected() -> None:
    with pytest.raises(ValueError, match="pass requires fidelity"):
        ReferenceQCValidator().build_qc(
            _blueprint(),
            _evidence(),
            _review(status="pass", fidelity=0.70, quality=0.90),
        )


def test_revise_requires_concrete_action_and_rerender() -> None:
    review = _review(status="revise", fidelity=0.72, quality=0.76)
    review["revision_actions"] = [
        {
            "reference_segment_id": "seg_002",
            "route": "render",
            "priority": "high",
            "instruction": "Move crop right so the pouring hand remains visible.",
            "measurable_goal": "Hand is visible in the paired frame at 3.0s.",
        }
    ]
    review["final_decision"] = {
        "publishable": False,
        "requires_rerender": True,
        "reason": "Fixable framing defect remains.",
    }

    qc = ReferenceQCValidator().build_qc(_blueprint(), _evidence(), review)

    assert qc["status"] == "revise"
    assert qc["revision_actions"][0]["route"] == "render"


def test_footage_limited_can_finalize_complete_video_with_improvement_request() -> None:
    review = _review(status="footage_limited", fidelity=0.62, quality=0.86)
    review["revision_actions"] = []
    review["improvement_requests"] = [
        {
            "reference_segment_id": "seg_002",
            "reason": "No source clip contains the same pour interaction from POV.",
            "suggested_footage": "Upload a 2–3 second POV pour shot with the hand entering from frame-right.",
        }
    ]
    review["final_decision"] = {
        "publishable": True,
        "requires_rerender": False,
        "reason": "Output is technically good; remaining mismatch is source-footage limited.",
    }

    qc = ReferenceQCValidator().build_qc(_blueprint(), _evidence(), review)

    assert qc["status"] == "footage_limited"
    assert qc["final_decision"]["publishable"] is True
    assert qc["improvement_requests"][0]["reference_segment_id"] == "seg_002"
    assert any("footage" in note.lower() for note in qc["metadata"]["notes"])


def test_footage_limited_cannot_hide_low_standalone_quality() -> None:
    review = _review(status="footage_limited", fidelity=0.55, quality=0.60)
    review["improvement_requests"] = [
        {
            "reference_segment_id": "seg_002",
            "reason": "Missing matching source action.",
            "suggested_footage": "Add a matching POV action shot.",
        }
    ]
    review["final_decision"] = {
        "publishable": True,
        "requires_rerender": False,
        "reason": "Attempted footage-limited final.",
    }

    with pytest.raises(ValueError, match="standalone quality"):
        ReferenceQCValidator().build_qc(_blueprint(), _evidence(), review)


def test_large_duration_drift_forces_revise() -> None:
    evidence = _evidence()
    evidence["duration_delta_seconds"] = 0.4

    with pytest.raises(ValueError, match="status must be revise"):
        ReferenceQCValidator().build_qc(
            _blueprint(),
            evidence,
            _review(status="pass", fidelity=0.92, quality=0.91),
        )


def test_semantic_pass_cannot_override_deterministic_quality_failure() -> None:
    with pytest.raises(ValueError, match="contradicts deterministic replication quality failure"):
        ReferenceQCValidator().build_qc(
            _blueprint(),
            _evidence(),
            _review(status="pass", fidelity=0.92, quality=0.91),
            deterministic_quality_report=_quality_report(gate="fail", source_limited=False),
        )


def test_source_limited_deterministic_failure_is_preserved_in_canonical_qc() -> None:
    review = _review(status="footage_limited", fidelity=0.62, quality=0.86)
    review["improvement_requests"] = [{"reference_segment_id": "seg_002", "reason": "Missing action.", "suggested_footage": "Add the matching POV action."}]
    review["final_decision"] = {"publishable": True, "requires_rerender": False, "reason": "Source-limited but technically valid."}

    qc = ReferenceQCValidator().build_qc(
        _blueprint(),
        _evidence(),
        review,
        deterministic_quality_report=_quality_report(gate="fail", source_limited=True),
        deterministic_quality_report_path="replication_quality_report.json",
    )

    assert qc["status"] == "footage_limited"
    assert qc["render_integrity"]["status"] == "pass"
    assert qc["replication_quality"]["quality_gate"] == "fail"
    assert qc["metadata"]["deterministic_quality_report_path"] == "replication_quality_report.json"
    schema = json.loads((ROOT / "schemas" / "artifacts" / "replication_qc.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(instance=qc, schema=schema)


def _blueprint() -> dict:
    return {
        "version": "1.0",
        "source": {"duration_seconds": 4.0},
        "segments": [{"id": "seg_001"}, {"id": "seg_002"}],
    }


def _evidence() -> dict:
    return {
        "version": "1.0",
        "duration_delta_seconds": 0.04,
        "segments": [
            {"reference_segment_id": "seg_001"},
            {"reference_segment_id": "seg_002"},
        ],
        "metadata": {"iteration": 1},
    }


def _review(*, status: str, fidelity: float, quality: float) -> dict:
    return {
        "iteration": 1,
        "status": status,
        "scores": {
            "fidelity_score": fidelity,
            "quality_score": quality,
            "choreography": fidelity,
            "timing": fidelity,
            "camera_framing": fidelity,
            "motion_speed": fidelity,
            "transitions": fidelity,
            "text": fidelity,
            "audio": None,
            "technical_quality": quality,
        },
        "summary": "QC review of the current draft.",
        "segment_reviews": [
            {
                "reference_segment_id": "seg_002",
                "severity": "medium" if status != "pass" else "low",
                "dimensions": ["camera_framing"],
                "issue": "Framing differs from reference." if status != "pass" else "Minor framing difference.",
                "evidence_notes": "Compared paired middle frames.",
                "recommended_route": "render" if status == "revise" else "footage" if status == "footage_limited" else "none",
                "recommended_action": "Adjust crop." if status == "revise" else "Add better footage." if status == "footage_limited" else "No action required.",
            }
        ],
        "revision_actions": [],
        "improvement_requests": [],
        "final_decision": {
            "publishable": status == "pass",
            "requires_rerender": status == "revise",
            "reason": "Meets fidelity and quality gates." if status == "pass" else "Review requires follow-up.",
        },
        "notes": [],
    }


def _quality_report(*, gate: str, source_limited: bool) -> dict:
    return {
        "quality_gate": gate,
        "fallback_count": 2 if source_limited else 0,
        "fallback_ratio": 1.0 if source_limited else 0.0,
        "unique_source_segment_count": 1,
        "reuse_ratio": 0.5,
        "max_reuse_count": 2,
        "dominant_source_share": 1.0,
        "overlap_reuse_count": 1,
        "speed": {"min": 1.0, "max": 1.0, "mean": 1.0},
        "chronology": {"backward_jump_count": 0, "large_backward_jump_count": 0, "source_direction_changes": 0, "chronology_consistency_score": 1.0},
        "render_integrity": {"status": "pass", "timeline_full_coverage": True, "timeline_contiguous": True, "draft_exists": True, "duration_delta_seconds": 0.04, "duration_tolerance_seconds": 0.15, "flags": []},
        "replication_quality": {"status": gate, "source_limited": source_limited, "fixable_by_render_revision": False, "recommended_route": "footage" if source_limited else "match_or_timeline"},
        "quality_flags": [],
    }
