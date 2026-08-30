from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from tools.analysis.reference_match_validator import ReferenceMatchValidator


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas" / "artifacts" / "reference_matching.schema.json"


def test_full_coverage_plan_accepts_fallback_and_resolves_source_ranges() -> None:
    validator = ReferenceMatchValidator()
    plan = validator.build_canonical_plan(_blueprint(), _profiles(), _proposal())

    assert plan["coverage"] == {
        "reference_segment_count": 2,
        "matched_segment_count": 2,
        "full_coverage": True,
        "fallback_count": 1,
    }
    assert plan["matches"][0]["selected"]["source_path"] == "footage/a.mp4"
    assert plan["matches"][1]["match_class"] == "fallback"

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(instance=plan, schema=schema)


def test_missing_reference_match_is_rejected() -> None:
    validator = ReferenceMatchValidator()
    proposal = _proposal()
    proposal["matches"] = proposal["matches"][:1]

    with pytest.raises(ValueError, match="coverage incomplete"):
        validator.build_canonical_plan(_blueprint(), _profiles(), proposal)


def test_unknown_footage_segment_is_rejected() -> None:
    validator = ReferenceMatchValidator()
    proposal = _proposal()
    proposal["matches"][0]["footage_segment_id"] = "does_not_exist"

    with pytest.raises(ValueError, match="unknown or unusable footage segment"):
        validator.build_canonical_plan(_blueprint(), _profiles(), proposal)


def test_fallback_requires_improvement_request() -> None:
    validator = ReferenceMatchValidator()
    proposal = _proposal()
    proposal["improvement_requests"] = []

    with pytest.raises(ValueError, match="requires an improvement_request"):
        validator.build_canonical_plan(_blueprint(), _profiles(), proposal)


def test_reuse_is_allowed_to_preserve_coverage_but_noted() -> None:
    validator = ReferenceMatchValidator()
    proposal = _proposal()
    proposal["matches"][1]["footage_segment_id"] = "clip_001_action"

    plan = validator.build_canonical_plan(_blueprint(), _profiles(), proposal)

    assert plan["coverage"]["full_coverage"] is True
    assert any("reuse" in note.lower() for note in plan["notes"])


def _blueprint() -> dict:
    return {
        "version": "1.0",
        "source": {"path": "reference.mp4", "duration_seconds": 4.0},
        "segments": [
            {"id": "seg_001"},
            {"id": "seg_002"},
        ],
        "analysis_meta": {"semantic_enrichment_required": False},
    }


def _profiles() -> dict:
    return {
        "version": "1.0",
        "source_dir": "footage",
        "clips": [
            {
                "clip_id": "clip_001",
                "path": "footage/a.mp4",
                "duration_seconds": 6.0,
                "usable": True,
                "segments": [
                    {
                        "id": "clip_001_action",
                        "source_in": 1.0,
                        "source_out": 3.0,
                        "duration_seconds": 2.0,
                    },
                    {
                        "id": "clip_001_fallback",
                        "source_in": 3.0,
                        "source_out": 5.0,
                        "duration_seconds": 2.0,
                    },
                ],
            }
        ],
        "analysis_meta": {"semantic_enrichment_required": False},
    }


def _proposal() -> dict:
    return {
        "matches": [
            {
                "reference_segment_id": "seg_001",
                "footage_segment_id": "clip_001_action",
                "match_class": "good",
                "scores": _scores(0.9),
                "rationale": "Strong action and camera fit.",
                "tradeoffs": [],
                "alternatives": ["clip_001_fallback"],
            },
            {
                "reference_segment_id": "seg_002",
                "footage_segment_id": "clip_001_fallback",
                "match_class": "fallback",
                "scores": _scores(0.45),
                "rationale": "No close action exists, but this keeps the flow complete.",
                "tradeoffs": ["action mismatch"],
                "alternatives": ["clip_001_action"],
            },
        ],
        "improvement_requests": [
            {
                "reference_segment_id": "seg_002",
                "reason": "No close action exists.",
                "suggested_footage": "Upload a closer action from the same POV.",
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
