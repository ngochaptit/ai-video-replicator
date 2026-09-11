from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from moon.core.project import MoonProject
from moon.evidence import SampledFrameEvidenceStore
from moon.footage_batches import FootageBatchPolicy, FootageSemanticProgress
from moon.runner.pipeline import PipelineRunner


def _runner_with_groups(
    tmp_path: Path,
    *,
    clips: int = 1,
    ranges_per_clip: int = 1,
    sample_kind: str = "coarse",
) -> PipelineRunner:
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))
    scaffold_clips = []
    store = SampledFrameEvidenceStore(runner.project, runner.state.revision)
    for clip_index in range(clips):
        clip_id = f"clip_{clip_index + 1:03d}"
        source = tmp_path / "footage" / f"{clip_id}.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"source-{clip_id}".encode())
        scaffold_clips.append(
            {
                "clip_id": clip_id,
                "path": str(source),
                "duration_seconds": float(ranges_per_clip * 10),
                "fps": 30.0,
                "segments": [],
            }
        )
        for range_index in range(ranges_per_clip):
            start = float(range_index * 10)
            end = start + 10.0
            frame_root = tmp_path / ".moon" / "cache" / "batch-fixture" / clip_id / str(range_index)
            frame_root.mkdir(parents=True, exist_ok=True)
            frames = []
            for frame_index, timestamp in enumerate((start, start + 5.0, end)):
                frame = frame_root / f"frame-{frame_index}.jpg"
                frame.write_bytes(f"{clip_id}-{range_index}-{frame_index}".encode())
                frames.append({"timestamp_seconds": timestamp, "path": str(frame)})
            group_id = store.group_id(
                "footage", source, start_seconds=start, end_seconds=end,
                count=3, width=320,
            )
            store.register(
                "footage",
                {
                    "source": str(source), "start_seconds": start,
                    "end_seconds": end, "count": 3, "width": 320,
                    "frames": frames,
                },
                group_id=group_id,
                clip_id=clip_id,
                sample_kind=sample_kind,
                handoff_revision=1 if sample_kind == "dense_refinement" else 0,
            )
    runner.artifacts.write(
        "footage_profiles_scaffold",
        {"version": "1.0", "source_dir": "footage", "clips": scaffold_clips},
    )
    return runner


def _result_for(batch: dict) -> dict:
    clips = []
    for clip_id in batch["clip_ids"]:
        ranges = [item for item in batch["ranges"] if item["clip_id"] == clip_id]
        segments = [
            {
                "id": f"{clip_id}_{item['start_seconds']:g}",
                "source_in": item["start_seconds"],
                "source_out": item["end_seconds"],
                "boundary_basis": ["sampled_frame"],
            }
            for item in ranges
        ]
        clips.append({"clip_id": clip_id, "usable": True, "segments": segments})
    return {"artifact": "footage_semantic_batch", "batch_id": batch["batch_id"], "clips": clips}


def test_twenty_clips_are_split_by_hard_batch_budget(tmp_path: Path) -> None:
    runner = _runner_with_groups(tmp_path, clips=20)
    progress = FootageSemanticProgress(
        runner, FootageBatchPolicy(max_clips=3, max_refinement_ranges=3, max_frames=60)
    ).ensure()

    assert progress is not None
    assert len(progress["batches"]) == 7
    assert all(len(batch["clip_ids"]) <= 3 for batch in progress["batches"])
    assert all(batch["frame_count"] <= 60 for batch in progress["batches"])
    assert all(
        batch["evidence_bytes"] <= 24 * 1024 * 1024
        for batch in progress["batches"]
    )


def test_fifteen_refinement_ranges_become_five_batches(tmp_path: Path) -> None:
    runner = _runner_with_groups(
        tmp_path, clips=1, ranges_per_clip=15, sample_kind="dense_refinement"
    )
    progress = FootageSemanticProgress(runner, FootageBatchPolicy()).ensure()

    assert progress is not None
    refinement = [item for item in progress["batches"] if item["batch_type"] == "refinement"]
    assert len(refinement) == 5
    assert all(len(item["ranges"]) <= 3 for item in refinement)
    assert all(item["frame_count"] == 9 for item in refinement)


def test_single_range_over_budget_fails_with_bounded_diagnostic(tmp_path: Path) -> None:
    runner = _runner_with_groups(tmp_path)
    progress = FootageSemanticProgress(
        runner, FootageBatchPolicy(max_evidence_bytes=10)
    )

    with pytest.raises(ValueError, match="single footage evidence range exceeds batch budget"):
        progress.ensure()


def test_completed_batch_persists_and_restart_selects_only_next_evidence(tmp_path: Path) -> None:
    runner = _runner_with_groups(tmp_path, clips=4)
    policy = FootageBatchPolicy(max_clips=2)
    progress = FootageSemanticProgress(runner, policy)
    first = progress.activate()
    assert first is not None
    progress.bind_request(first["batch_id"], "request-1", first["revision"], ["hash"])
    selected_first = {
        frame["path"]
        for group in progress.selected_evidence()["groups"]
        for frame in group["frames"]
    }
    response_hash = hashlib.sha256(b"response-1").hexdigest()
    progress.complete("request-1", _result_for(first), response_hash)

    restarted = FootageSemanticProgress.open_existing(PipelineRunner(MoonProject.open(tmp_path)))
    assert restarted is not None
    second = restarted.activate()
    assert second is not None and second["batch_id"] != first["batch_id"]
    selected_second = {
        frame["path"]
        for group in restarted.selected_evidence()["groups"]
        for frame in group["frames"]
    }
    durable = restarted.load()
    partial = runner.artifacts.read(str(durable["batches"][0]["result_artifact"]))

    assert selected_first.isdisjoint(selected_second)
    assert durable["batches"][0]["status"] == "completed"
    assert durable["batches"][0]["result_artifact"]
    assert partial["artifact"] == "footage_semantic_batch"
    assert partial["request_id"] == "request-1"
    assert partial["revision"] == first["revision"]


def test_same_completed_batch_response_is_idempotent(tmp_path: Path) -> None:
    runner = _runner_with_groups(tmp_path)
    progress = FootageSemanticProgress(runner, FootageBatchPolicy())
    batch = progress.activate()
    progress.bind_request(batch["batch_id"], "request-1", batch["revision"], ["hash"])
    digest = hashlib.sha256(b"same").hexdigest()
    first = progress.complete("request-1", _result_for(batch), digest)
    artifact = runner.artifacts.path_for(f"footage_semantic_batch_{batch['batch_id']}")
    original = artifact.read_bytes()

    second = progress.complete("request-1", _result_for(batch), digest)

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["remaining_batches"] == 0
    assert second["final_payload"] is not None
    assert artifact.read_bytes() == original


def test_final_assembly_merges_partial_batches_and_passes_validator(tmp_path: Path) -> None:
    runner = _runner_with_groups(tmp_path, clips=2)
    progress = FootageSemanticProgress(runner, FootageBatchPolicy(max_clips=1))
    first = progress.activate()
    progress.bind_request(first["batch_id"], "request-1", first["revision"], ["one"])
    assert progress.complete("request-1", _result_for(first), "one")["final_payload"] is None
    second = progress.activate()
    progress.bind_request(second["batch_id"], "request-2", second["revision"], ["two"])

    final = progress.complete("request-2", _result_for(second), "two")["final_payload"]

    assert final is not None
    assert {clip["clip_id"] for clip in final["clips"]} == {"clip_001", "clip_002"}
    assert all(clip["segments"] for clip in final["clips"])


def test_completed_refinement_is_checkpointed_into_resumed_parent_context(
    tmp_path: Path,
) -> None:
    runner = _runner_with_groups(tmp_path)
    progress = FootageSemanticProgress(runner, FootageBatchPolicy())
    parent = progress.activate()
    progress.bind_request(parent["batch_id"], "request-1", parent["revision"], ["coarse"])
    source = Path(runner.artifacts.read("footage_profiles_scaffold")["clips"][0]["path"])
    frame_root = runner.project.cache_dir / "refinement-fixture"
    frame_root.mkdir(parents=True)
    frames = []
    for index, timestamp in enumerate((4.0, 4.5, 5.0)):
        frame = frame_root / f"frame-{index}.jpg"
        frame.write_bytes(f"refinement-{index}".encode())
        frames.append({"timestamp_seconds": timestamp, "path": str(frame)})
    store = SampledFrameEvidenceStore(runner.project, runner.state.revision)
    group_id = store.group_id(
        "footage", source, start_seconds=4.0, end_seconds=5.0, count=3, width=320
    )
    store.register(
        "footage",
        {
            "source": str(source),
            "start_seconds": 4.0,
            "end_seconds": 5.0,
            "count": 3,
            "width": 320,
            "frames": frames,
        },
        group_id=group_id,
        clip_id="clip_001",
        sample_kind="dense_refinement",
        handoff_revision=1,
    )
    progress.defer_for_refinement("request-1", [group_id], "refine-request")
    replayed = progress.replayed_refinement("request-1", "refine-request")
    assert replayed is not None and replayed["idempotent"] is True
    assert replayed["group_ids"] == [group_id]

    refinement = progress.activate()
    progress.bind_request(
        refinement["batch_id"], "request-2", refinement["revision"], ["dense"]
    )
    progress.complete("request-2", _result_for(refinement), "dense-response")
    resumed_parent = progress.activate()
    progress.bind_request(
        resumed_parent["batch_id"], "request-3", resumed_parent["revision"], ["coarse"]
    )
    compact = json.loads(progress.write_active_scaffold().read_text(encoding="utf-8"))

    completed = compact["analysis_meta"]["completed_refinement_results"]
    assert resumed_parent["batch_id"] == parent["batch_id"]
    assert [item["batch_id"] for item in completed] == [refinement["batch_id"]]
    assert completed[0]["result"]["clips"][0]["segments"][0]["source_in"] == 4.0
