from __future__ import annotations

import json
from pathlib import Path

from moon.core.project import MoonProject
from moon.evidence import SampledFrameEvidenceStore
from moon.execution import StageExecutionService
from moon.runner.pipeline import PipelineRunner


def _runner_at_footage(tmp_path: Path) -> PipelineRunner:
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))
    runner.complete("proposal", {"approved": True})
    runner.complete("analyze", {"artifacts": {}})
    return runner


def test_registered_sampled_evidence_is_bridged_into_builder_enrichment(tmp_path: Path) -> None:
    runner = _runner_at_footage(tmp_path)
    source = tmp_path / "footage" / "oneshot.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"video")

    frame = (
        tmp_path
        / ".moon"
        / "cache"
        / "connector-frames"
        / "footage"
        / "revision_000"
        / "manual"
        / "frame.jpg"
    )
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"jpg")

    scaffold = {
        "clips": [
            {
                "clip_id": "clip_001",
                "path": str(source),
                "duration_seconds": 10.0,
                "segments": [],
            }
        ]
    }
    runner.artifacts.write("footage_profiles_scaffold", scaffold)
    runner.artifacts.write(
        "footage_semantic_enrichment",
        {"clips": [{"clip_id": "clip_001", "segments": []}]},
    )

    store = SampledFrameEvidenceStore(runner.project, 0)
    group_id = store.group_id(
        "footage",
        source,
        start_seconds=2.0,
        end_seconds=4.0,
        count=1,
        width=320,
    )
    store.register(
        "footage",
        {
            "source": str(source),
            "start_seconds": 2.0,
            "end_seconds": 4.0,
            "count": 1,
            "width": 320,
            "frames": [{"timestamp_seconds": 3.0, "path": str(frame)}],
        },
        group_id=group_id,
        clip_id="clip_001",
    )

    path = StageExecutionService(runner)._materialize_footage_enrichment()
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["evidence_catalog"][0]["clip_id"] == "clip_001"
    assert payload["evidence_catalog"][0]["timestamp"] == 3.0
    assert Path(payload["evidence_catalog"][0]["path"]).is_file()
    assert runner.artifacts.exists("footage_evidence_catalog")
