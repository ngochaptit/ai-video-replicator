from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools.video.reference_finalizer import ReferenceFinalizer


def test_pass_qc_promotes_draft_to_final(tmp_path: Path) -> None:
    draft = tmp_path / "draft.mp4"
    draft.write_bytes(b"fake-video-bytes")
    qc_path = tmp_path / "replication_qc.json"
    qc_path.write_text(json.dumps(_qc("pass")), encoding="utf-8")
    final = tmp_path / "final.mp4"

    result = ReferenceFinalizer().execute(
        {
            "replication_qc_path": str(qc_path),
            "draft_video_path": str(draft),
            "output_path": str(final),
        }
    )

    assert result.success is True
    assert final.read_bytes() == draft.read_bytes()
    assert result.data["sha256"] == hashlib.sha256(draft.read_bytes()).hexdigest()


def test_publishable_footage_limited_qc_can_finalize(tmp_path: Path) -> None:
    draft = tmp_path / "draft.mp4"
    draft.write_bytes(b"complete-video")
    qc = _qc("footage_limited")
    qc["improvement_requests"] = [
        {
            "reference_segment_id": "seg_002",
            "reason": "No matching POV action exists.",
            "suggested_footage": "Upload a closer POV action shot.",
        }
    ]
    qc_path = tmp_path / "replication_qc.json"
    qc_path.write_text(json.dumps(qc), encoding="utf-8")

    result = ReferenceFinalizer().execute(
        {
            "replication_qc_path": str(qc_path),
            "draft_video_path": str(draft),
            "output_path": str(tmp_path / "final.mp4"),
        }
    )

    assert result.success is True
    assert result.data["qc_status"] == "footage_limited"
    assert result.data["footage_improvement_requests"]


def test_revise_qc_cannot_finalize(tmp_path: Path) -> None:
    draft = tmp_path / "draft.mp4"
    draft.write_bytes(b"not-final")
    qc_path = tmp_path / "replication_qc.json"
    qc_path.write_text(json.dumps(_qc("revise")), encoding="utf-8")

    result = ReferenceFinalizer().execute(
        {
            "replication_qc_path": str(qc_path),
            "draft_video_path": str(draft),
            "output_path": str(tmp_path / "final.mp4"),
        }
    )

    assert result.success is False
    assert "not publishable" in result.error


def _qc(status: str) -> dict:
    if status == "revise":
        publishable = False
        rerender = True
    else:
        publishable = True
        rerender = False
    return {
        "version": "1.0",
        "status": status,
        "scores": {"fidelity_score": 0.9 if status == "pass" else 0.65, "quality_score": 0.88},
        "final_decision": {
            "publishable": publishable,
            "requires_rerender": rerender,
            "reason": "test gate",
        },
        "improvement_requests": [],
    }
