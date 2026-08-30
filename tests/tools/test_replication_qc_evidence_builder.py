from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from tools.analysis.replication_qc_evidence_builder import ReplicationQCEvidenceBuilder


ROOT = Path(__file__).resolve().parents[2]


def test_sample_timestamps_are_inside_segment_and_evenly_spaced() -> None:
    assert ReplicationQCEvidenceBuilder.sample_timestamps(2.0, 6.0, 3) == [3.0, 4.0, 5.0]


def test_build_evidence_pairs_same_reference_and_draft_timestamps_without_local_ai(tmp_path: Path) -> None:
    builder = ReplicationQCEvidenceBuilder()
    evidence = builder.build_evidence(
        _blueprint(),
        _timeline(),
        reference_video_path="reference.mp4",
        draft_video_path="draft.mp4",
        reference_frames_dir=tmp_path / "ref",
        draft_frames_dir=tmp_path / "draft",
        samples_per_segment=2,
        iteration=2,
        extract_frames=False,
        reference_duration_seconds=4.0,
        draft_duration_seconds=4.05,
    )

    assert evidence["duration_delta_seconds"] == 0.05
    assert evidence["metadata"]["semantic_review_required"] is True
    assert evidence["metadata"]["iteration"] == 2
    assert [item["reference_segment_id"] for item in evidence["segments"]] == ["seg_001", "seg_002"]
    assert evidence["segments"][0]["sample_timestamps"] == [0.666667, 1.333333]
    assert evidence["segments"][1]["sample_timestamps"] == [2.666667, 3.333333]
    for segment in evidence["segments"]:
        assert len(segment["reference_frames"]) == 2
        assert len(segment["draft_frames"]) == 2

    schema = json.loads(
        (ROOT / "schemas" / "artifacts" / "replication_qc_evidence.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(instance=evidence, schema=schema)


def _blueprint() -> dict:
    return {
        "version": "1.0",
        "source": {"path": "reference.mp4", "duration_seconds": 4.0},
        "segments": [
            {"id": "seg_001", "start_seconds": 0.0, "end_seconds": 2.0, "evidence": {"frame_paths": [], "frame_timestamps": []}},
            {"id": "seg_002", "start_seconds": 2.0, "end_seconds": 4.0, "evidence": {"frame_paths": [], "frame_timestamps": []}},
        ],
    }


def _timeline() -> dict:
    return {
        "version": "1.0",
        "reference_duration_seconds": 4.0,
        "segments": [
            {"reference_segment_id": "seg_001", "timeline_start": 0.0, "timeline_end": 2.0},
            {"reference_segment_id": "seg_002", "timeline_start": 2.0, "timeline_end": 4.0},
        ],
        "coverage": {"full_coverage": True, "timeline_contiguous": True},
    }
