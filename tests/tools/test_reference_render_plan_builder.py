from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from tools.video.reference_render_plan_builder import ReferenceRenderPlanBuilder


ROOT = Path(__file__).resolve().parents[2]


def test_builds_edit_decisions_assets_hold_map_and_text_overlays() -> None:
    plan = ReferenceRenderPlanBuilder().build_plan(
        _timeline(),
        _blueprint(),
        render_runtime="ffmpeg",
        renderer_family="documentary-montage",
        composition_mode="templated",
        runtime_approved=True,
        output_video_path="renders/draft.mp4",
    )

    assert plan["runtime_approved"] is True
    assert plan["render_runtime"] == "ffmpeg"
    assert plan["output"]["width"] == 1080
    assert plan["output"]["height"] == 1920
    assert plan["edit_decisions"]["render_runtime"] == "ffmpeg"
    assert len(plan["edit_decisions"]["cuts"]) == 2
    assert len(plan["asset_manifest"]["assets"]) == 1  # same source file is deduplicated
    assert plan["metadata"]["hold_segment_count"] == 1
    hold_map = plan["edit_decisions"]["metadata"]["reference_replication"]["hold_seconds_by_cut"]
    assert hold_map == {"replication_cut_002": 0.5}
    assert plan["text_overlays"][0]["content"] == "Đổ sữa"


def test_runtime_must_be_explicitly_approved() -> None:
    with pytest.raises(ValueError, match="explicitly user-approved"):
        ReferenceRenderPlanBuilder().build_plan(
            _timeline(),
            _blueprint(),
            render_runtime="ffmpeg",
            renderer_family="documentary-montage",
            composition_mode="templated",
            runtime_approved=False,
        )


def test_ffmpeg_rejects_atelier_mode() -> None:
    with pytest.raises(ValueError, match="atelier is not applicable"):
        ReferenceRenderPlanBuilder().build_plan(
            _timeline(),
            _blueprint(),
            render_runtime="ffmpeg",
            renderer_family="documentary-montage",
            composition_mode="atelier",
            runtime_approved=True,
        )


def test_plan_and_nested_existing_contracts_validate() -> None:
    plan = ReferenceRenderPlanBuilder().build_plan(
        _timeline(),
        _blueprint(),
        render_runtime="ffmpeg",
        renderer_family="documentary-montage",
        composition_mode="templated",
        runtime_approved=True,
    )
    pairs = [
        (plan, "replication_render_plan.schema.json"),
        (plan["edit_decisions"], "edit_decisions.schema.json"),
        (plan["asset_manifest"], "asset_manifest.schema.json"),
    ]
    for instance, filename in pairs:
        schema = json.loads((ROOT / "schemas" / "artifacts" / filename).read_text(encoding="utf-8"))
        jsonschema.validate(instance=instance, schema=schema)


def _blueprint() -> dict:
    return {
        "version": "1.0",
        "source": {
            "path": "reference.mp4",
            "duration_seconds": 4.0,
            "resolution": "1080x1920",
            "orientation": "vertical",
        },
        "segments": [],
    }


def _timeline() -> dict:
    return {
        "version": "1.0",
        "reference_duration_seconds": 4.0,
        "segments": [
            {
                "id": "timeline_001",
                "order": 1,
                "reference_segment_id": "seg_001",
                "timeline_start": 0.0,
                "timeline_end": 2.0,
                "target_duration_seconds": 2.0,
                "source": {
                    "path": "footage/a.mp4",
                    "footage_segment_id": "clip_a_1",
                    "in_seconds": 1.0,
                    "out_seconds": 3.0,
                    "duration_seconds": 2.0,
                },
                "timing_fit": {"mode": "speed_fit", "speed": 1.0, "hold_seconds": 0.0, "extreme_speed": False},
                "match": {"class": "good", "overall_score": 0.9, "rationale": "good", "tradeoffs": []},
                "reference_cues": {
                    "transition_in": "cut",
                    "transition_out": "cut",
                    "camera": {},
                    "spatial": {},
                    "text": {"content": "", "position": None, "timing_notes": ""},
                    "audio": {},
                },
                "quality_risks": [],
            },
            {
                "id": "timeline_002",
                "order": 2,
                "reference_segment_id": "seg_002",
                "timeline_start": 2.0,
                "timeline_end": 4.0,
                "target_duration_seconds": 2.0,
                "source": {
                    "path": "footage/a.mp4",
                    "footage_segment_id": "clip_a_2",
                    "in_seconds": 3.0,
                    "out_seconds": 4.5,
                    "duration_seconds": 1.5,
                },
                "timing_fit": {"mode": "speed_fit_with_hold", "speed": 1.0, "hold_seconds": 0.5, "extreme_speed": True},
                "match": {"class": "fallback", "overall_score": 0.4, "rationale": "fallback", "tradeoffs": ["action mismatch"]},
                "reference_cues": {
                    "transition_in": "dissolve",
                    "transition_out": "cut",
                    "camera": {},
                    "spatial": {},
                    "text": {"content": "Đổ sữa", "position": "top-center", "timing_notes": "appears near action start"},
                    "audio": {},
                },
                "quality_risks": ["fallback"],
            },
        ],
        "coverage": {
            "segment_count": 2,
            "full_coverage": True,
            "timeline_contiguous": True,
            "fallback_count": 1,
            "extreme_speed_count": 1,
            "hold_segment_count": 1,
        },
        "warnings": ["seg_002 fallback"],
        "metadata": {
            "generated_by": "reference_timeline_builder",
            "reference_blueprint_path": "reference_blueprint.json",
            "reference_matching_path": "matching.json",
            "render_runtime_locked": False,
            "notes": [],
        },
    }
