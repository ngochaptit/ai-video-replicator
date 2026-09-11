from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from moon.core.project import MoonProject
from moon.evidence import SampledFrameEvidenceStore
from moon.footage_batches import FootageBatchPolicy, FootageSemanticProgress
from moon.footage_refinement import FootageRefinementService
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
            _register_group(
                runner,
                store,
                source=source,
                clip_id=clip_id,
                start=start,
                end=end,
                sample_kind=sample_kind,
                count=3,
                width=320,
                handoff_revision=(
                    1 if sample_kind == "dense_refinement" else 0
                ),
                label=f"{clip_id}-{range_index}",
            )
    runner.artifacts.write(
        "footage_profiles_scaffold",
        {
            "version": "1.0",
            "source_dir": "footage",
            "clips": scaffold_clips,
        },
    )
    return runner


def _register_group(
    runner: PipelineRunner,
    store: SampledFrameEvidenceStore,
    *,
    source: Path,
    clip_id: str,
    start: float,
    end: float,
    sample_kind: str,
    count: int,
    width: int,
    handoff_revision: int,
    label: str,
) -> str:
    frame_root = (
        runner.project.cache_dir
        / "batch-fixture"
        / clip_id
        / label.replace("/", "_")
    )
    frame_root.mkdir(parents=True, exist_ok=True)
    frames = []
    for frame_index in range(count):
        timestamp = (
            start
            if count == 1
            else start + (end - start) * frame_index / (count - 1)
        )
        frame = frame_root / f"frame-{frame_index}.jpg"
        frame.write_bytes(f"{label}-{frame_index}".encode())
        frames.append({"timestamp_seconds": timestamp, "path": str(frame)})
    group_id = store.group_id(
        "footage",
        source,
        start_seconds=start,
        end_seconds=end,
        count=count,
        width=width,
    )
    store.register(
        "footage",
        {
            "source": str(source),
            "start_seconds": start,
            "end_seconds": end,
            "count": count,
            "width": width,
            "frames": frames,
        },
        group_id=group_id,
        clip_id=clip_id,
        sample_kind=sample_kind,
        handoff_revision=handoff_revision,
    )
    return group_id


def _result_for(batch: dict) -> dict:
    clips = []
    for clip_id in batch["clip_ids"]:
        ranges = [
            item
            for item in batch["ranges"]
            if item["clip_id"] == clip_id
        ]
        segments = [
            {
                "id": f"{clip_id}_{item['start_seconds']:g}",
                "source_in": item["start_seconds"],
                "source_out": item["end_seconds"],
                "boundary_basis": ["sampled_frame"],
            }
            for item in ranges
        ]
        clips.append(
            {"clip_id": clip_id, "usable": True, "segments": segments}
        )
    return {
        "artifact": "footage_semantic_batch",
        "batch_id": batch["batch_id"],
        "clips": clips,
    }


def _refinement_payload(
    clip_id: str,
    start: float,
    end: float,
) -> dict:
    return {
        "artifact": "footage_refinement_request",
        "requests": [
            {
                "clip_id": clip_id,
                "start_seconds": start,
                "end_seconds": end,
                "reason": "boundary needs denser measured evidence",
            }
        ],
    }


def test_twenty_clips_are_split_by_hard_batch_budget(
    tmp_path: Path,
) -> None:
    runner = _runner_with_groups(tmp_path, clips=20)
    progress = FootageSemanticProgress(
        runner,
        FootageBatchPolicy(
            max_clips=3,
            max_refinement_ranges=3,
            max_frames=60,
        ),
    ).ensure()

    assert progress is not None
    assert len(progress["batches"]) == 7
    assert all(
        len(batch["clip_ids"]) <= 3 for batch in progress["batches"]
    )
    assert all(
        len(batch["ranges"]) <= 3 for batch in progress["batches"]
    )
    assert all(
        batch["frame_count"] <= 60 for batch in progress["batches"]
    )
    assert all(
        batch["evidence_bytes"] <= 24 * 1024 * 1024
        for batch in progress["batches"]
    )


def test_coarse_ranges_respect_range_budget(tmp_path: Path) -> None:
    runner = _runner_with_groups(
        tmp_path, clips=1, ranges_per_clip=8
    )
    progress = FootageSemanticProgress(
        runner,
        FootageBatchPolicy(max_refinement_ranges=3),
    ).ensure()

    assert progress is not None
    assert [len(item["ranges"]) for item in progress["batches"]] == [
        3,
        3,
        2,
    ]
    assert all(
        item["batch_type"] == "coarse"
        for item in progress["batches"]
    )


def test_legacy_dense_groups_are_cache_not_bootstrap_semantic_batches(
    tmp_path: Path,
) -> None:
    runner = _runner_with_groups(tmp_path)
    store = SampledFrameEvidenceStore(
        runner.project, runner.state.revision
    )
    source = Path(
        runner.artifacts.read("footage_profiles_scaffold")["clips"][0][
            "path"
        ]
    )
    _register_group(
        runner,
        store,
        source=source,
        clip_id="clip_001",
        start=4.0,
        end=5.0,
        sample_kind="dense_refinement",
        count=9,
        width=640,
        handoff_revision=7,
        label="legacy-dense",
    )

    value = FootageSemanticProgress(
        runner, FootageBatchPolicy()
    ).ensure()

    assert value is not None
    assert value["batches"]
    assert all(
        item["batch_type"] == "coarse" for item in value["batches"]
    )


def test_legacy_dense_samples_are_reused_after_gpt_requests_refinement(
    tmp_path: Path,
) -> None:
    runner = _runner_with_groups(tmp_path)
    store = SampledFrameEvidenceStore(
        runner.project, runner.state.revision
    )
    source = Path(
        runner.artifacts.read("footage_profiles_scaffold")["clips"][0][
            "path"
        ]
    )
    legacy_group = _register_group(
        runner,
        store,
        source=source,
        clip_id="clip_001",
        start=4.0,
        end=5.0,
        sample_kind="dense_refinement",
        count=9,
        width=640,
        handoff_revision=7,
        label="legacy-reusable",
    )
    progress = FootageSemanticProgress(
        runner, FootageBatchPolicy()
    )
    parent = progress.activate()
    assert parent is not None
    progress.bind_request(
        parent["batch_id"],
        "request-parent",
        parent["revision"],
        ["coarse"],
    )

    service = FootageRefinementService(runner)
    targets = service.validate(
        _refinement_payload("clip_001", 4.0, 5.0)
    )
    sampling = service.sample(targets, handoff_revision=1)

    assert sampling["sampled_groups"] == 0
    assert sampling["skipped_existing_groups"] == 1
    assert sampling["groups"][0]["group_id"] == legacy_group

    deferred = progress.defer_for_refinement(
        "request-parent", [legacy_group], "refinement-response"
    )
    assert len(deferred["refinement_batches"]) == 1
    assert (
        deferred["refinement_batches"][0]["batch_type"]
        == "refinement"
    )


def test_fifteen_requested_refinement_ranges_become_five_batches(
    tmp_path: Path,
) -> None:
    runner = _runner_with_groups(tmp_path)
    progress = FootageSemanticProgress(
        runner, FootageBatchPolicy(max_refinement_ranges=3)
    )
    parent = progress.activate()
    assert parent is not None
    progress.bind_request(
        parent["batch_id"],
        "request-1",
        parent["revision"],
        ["coarse"],
    )
    source = Path(
        runner.artifacts.read("footage_profiles_scaffold")["clips"][0][
            "path"
        ]
    )
    store = SampledFrameEvidenceStore(
        runner.project, runner.state.revision
    )
    group_ids = []
    for index in range(15):
        start = index * 0.6
        end = start + 0.4
        group_ids.append(
            _register_group(
                runner,
                store,
                source=source,
                clip_id="clip_001",
                start=start,
                end=end,
                sample_kind="dense_refinement",
                count=3,
                width=320,
                handoff_revision=1,
                label=f"dense-{index}",
            )
        )

    deferred = progress.defer_for_refinement(
        "request-1", group_ids, "refine-request"
    )
    refinement = deferred["refinement_batches"]

    assert len(refinement) == 5
    assert all(len(item["ranges"]) <= 3 for item in refinement)
    assert all(item["frame_count"] == 9 for item in refinement)


def test_refinement_request_cannot_escape_active_batch(
    tmp_path: Path,
) -> None:
    runner = _runner_with_groups(
        tmp_path, clips=1, ranges_per_clip=2
    )
    progress = FootageSemanticProgress(
        runner, FootageBatchPolicy(max_refinement_ranges=1)
    )
    first = progress.activate()
    assert first is not None
    assert first["ranges"][0]["start_seconds"] == 0.0
    assert first["ranges"][0]["end_seconds"] == 10.0

    with pytest.raises(
        ValueError, match="outside the active semantic batch"
    ):
        FootageRefinementService(runner).validate(
            _refinement_payload("clip_001", 12.0, 13.0)
        )


def test_single_range_over_budget_fails_with_bounded_diagnostic(
    tmp_path: Path,
) -> None:
    runner = _runner_with_groups(tmp_path)
    progress = FootageSemanticProgress(
        runner, FootageBatchPolicy(max_evidence_bytes=10)
    )

    with pytest.raises(
        ValueError,
        match="single footage evidence range exceeds batch budget",
    ):
        progress.ensure()


def test_completed_batch_persists_and_restart_selects_only_next_evidence(
    tmp_path: Path,
) -> None:
    runner = _runner_with_groups(tmp_path, clips=4)
    policy = FootageBatchPolicy(max_clips=2)
    progress = FootageSemanticProgress(runner, policy)
    first = progress.activate()
    assert first is not None
    progress.bind_request(
        first["batch_id"],
        "request-1",
        first["revision"],
        ["hash"],
    )
    selected_first = {
        frame["path"]
        for group in progress.selected_evidence()["groups"]
        for frame in group["frames"]
    }
    response_hash = hashlib.sha256(b"response-1").hexdigest()
    progress.complete(
        "request-1", _result_for(first), response_hash
    )

    restarted = FootageSemanticProgress.open_existing(
        PipelineRunner(MoonProject.open(tmp_path))
    )
    assert restarted is not None
    second = restarted.activate()
    assert second is not None
    assert second["batch_id"] != first["batch_id"]
    selected_second = {
        frame["path"]
        for group in restarted.selected_evidence()["groups"]
        for frame in group["frames"]
    }
    durable = restarted.load()
    partial = runner.artifacts.read(
        str(durable["batches"][0]["result_artifact"])
    )

    assert selected_first.isdisjoint(selected_second)
    assert durable["batches"][0]["status"] == "completed"
    assert durable["batches"][0]["result_artifact"]
    assert partial["artifact"] == "footage_semantic_batch"
    assert partial["request_id"] == "request-1"
    assert partial["revision"] == first["revision"]


def test_open_existing_rejects_other_pipeline_revision(
    tmp_path: Path,
) -> None:
    runner = _runner_with_groups(tmp_path)
    progress = FootageSemanticProgress(
        runner, FootageBatchPolicy()
    )
    assert progress.ensure() is not None
    value = progress.load()
    value["pipeline_revision"] = runner.state.revision + 1
    progress.save(value)

    assert FootageSemanticProgress.open_existing(runner) is None


def test_open_existing_rejects_changed_source_identity(
    tmp_path: Path,
) -> None:
    runner = _runner_with_groups(tmp_path)
    progress = FootageSemanticProgress(
        runner, FootageBatchPolicy()
    )
    assert progress.ensure() is not None
    source = Path(
        runner.artifacts.read("footage_profiles_scaffold")["clips"][0][
            "path"
        ]
    )
    source.write_bytes(b"changed-source")

    assert FootageSemanticProgress.open_existing(runner) is None


def test_same_completed_batch_response_is_idempotent(
    tmp_path: Path,
) -> None:
    runner = _runner_with_groups(tmp_path)
    progress = FootageSemanticProgress(
        runner, FootageBatchPolicy()
    )
    batch = progress.activate()
    progress.bind_request(
        batch["batch_id"],
        "request-1",
        batch["revision"],
        ["hash"],
    )
    digest = hashlib.sha256(b"same").hexdigest()
    first = progress.complete(
        "request-1", _result_for(batch), digest
    )
    artifact = runner.artifacts.path_for(
        f"footage_semantic_batch_{batch['batch_id']}"
    )
    original = artifact.read_bytes()

    second = progress.complete(
        "request-1", _result_for(batch), digest
    )

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["remaining_batches"] == 0
    assert second["final_payload"] is not None
    assert artifact.read_bytes() == original


def test_final_assembly_merges_partial_batches_and_passes_validator(
    tmp_path: Path,
) -> None:
    runner = _runner_with_groups(tmp_path, clips=2)
    progress = FootageSemanticProgress(
        runner, FootageBatchPolicy(max_clips=1)
    )
    first = progress.activate()
    progress.bind_request(
        first["batch_id"], "request-1", first["revision"], ["one"]
    )
    assert (
        progress.complete(
            "request-1", _result_for(first), "one"
        )["final_payload"]
        is None
    )
    second = progress.activate()
    progress.bind_request(
        second["batch_id"],
        "request-2",
        second["revision"],
        ["two"],
    )

    final = progress.complete(
        "request-2", _result_for(second), "two"
    )["final_payload"]

    assert final is not None
    assert {clip["clip_id"] for clip in final["clips"]} == {
        "clip_001",
        "clip_002",
    }
    assert all(clip["segments"] for clip in final["clips"])


def test_final_assembly_uses_only_available_measured_evidence(
    tmp_path: Path,
) -> None:
    runner = _runner_with_groups(tmp_path)
    progress = FootageSemanticProgress(
        runner, FootageBatchPolicy()
    )
    batch = progress.activate()
    progress.bind_request(
        batch["batch_id"],
        "request-1",
        batch["revision"],
        ["hash"],
    )
    assert (
        progress.complete(
            "request-1", _result_for(batch), "response"
        )["final_payload"]
        is not None
    )

    store = SampledFrameEvidenceStore(
        runner.project, runner.state.revision
    )
    for group in store.available("footage")["groups"]:
        for frame in group["frames"]:
            Path(frame["absolute_path"]).unlink()

    with pytest.raises(ValueError, match="in-range frame evidence"):
        progress.assemble_if_complete()


def test_completed_refinement_boundaries_validate_parent_locally_without_resending_frames(
    tmp_path: Path,
) -> None:
    runner = _runner_with_groups(tmp_path)
    progress = FootageSemanticProgress(
        runner, FootageBatchPolicy()
    )
    parent = progress.activate()
    progress.bind_request(
        parent["batch_id"],
        "request-1",
        parent["revision"],
        ["coarse"],
    )
    source = Path(
        runner.artifacts.read("footage_profiles_scaffold")["clips"][0][
            "path"
        ]
    )
    store = SampledFrameEvidenceStore(
        runner.project, runner.state.revision
    )
    group_id = _register_group(
        runner,
        store,
        source=source,
        clip_id="clip_001",
        start=4.0,
        end=5.0,
        sample_kind="dense_refinement",
        count=3,
        width=320,
        handoff_revision=1,
        label="refined-boundary",
    )
    progress.defer_for_refinement(
        "request-1", [group_id], "refine-request"
    )

    refinement = progress.activate()
    progress.bind_request(
        refinement["batch_id"],
        "request-2",
        refinement["revision"],
        ["dense"],
    )
    progress.complete(
        "request-2", _result_for(refinement), "dense-response"
    )

    resumed_parent = progress.activate()
    progress.bind_request(
        resumed_parent["batch_id"],
        "request-3",
        resumed_parent["revision"],
        ["coarse"],
    )
    transport_groups = {
        group["group_id"]
        for group in progress.selected_evidence()["groups"]
    }
    assert group_id not in transport_groups

    parent_result = {
        "artifact": "footage_semantic_batch",
        "batch_id": resumed_parent["batch_id"],
        "clips": [
            {
                "clip_id": "clip_001",
                "usable": True,
                "segments": [
                    {
                        "id": "before",
                        "source_in": 0.0,
                        "source_out": 4.0,
                        "boundary_basis": ["sampled_frame"],
                    },
                    {
                        "id": "after",
                        "source_in": 5.0,
                        "source_out": 10.0,
                        "boundary_basis": ["sampled_frame"],
                    },
                ],
            }
        ],
    }
    outcome = progress.complete(
        "request-3", parent_result, "parent-response"
    )

    assert outcome["final_payload"] is not None
    assert [
        (item["source_in"], item["source_out"])
        for item in outcome["final_payload"]["clips"][0]["segments"]
    ] == [(0.0, 4.0), (4.0, 5.0), (5.0, 10.0)]


def test_parent_overlap_with_completed_refinement_is_rejected_without_poisoning_progress(
    tmp_path: Path,
) -> None:
    runner = _runner_with_groups(tmp_path)
    progress = FootageSemanticProgress(
        runner, FootageBatchPolicy()
    )
    parent = progress.activate()
    progress.bind_request(
        parent["batch_id"],
        "request-1",
        parent["revision"],
        ["coarse"],
    )
    source = Path(
        runner.artifacts.read("footage_profiles_scaffold")["clips"][0][
            "path"
        ]
    )
    store = SampledFrameEvidenceStore(
        runner.project, runner.state.revision
    )
    group_id = _register_group(
        runner,
        store,
        source=source,
        clip_id="clip_001",
        start=4.0,
        end=5.0,
        sample_kind="dense_refinement",
        count=3,
        width=320,
        handoff_revision=1,
        label="overlap-child",
    )
    progress.defer_for_refinement(
        "request-1", [group_id], "refine-request"
    )
    child = progress.activate()
    progress.bind_request(
        child["batch_id"],
        "request-2",
        child["revision"],
        ["dense"],
    )
    progress.complete(
        "request-2", _result_for(child), "dense-response"
    )
    resumed_parent = progress.activate()
    progress.bind_request(
        resumed_parent["batch_id"],
        "request-3",
        resumed_parent["revision"],
        ["coarse"],
    )
    invalid = _result_for(resumed_parent)

    with pytest.raises(
        ValueError,
        match="overlaps completed refinement semantics",
    ):
        progress.complete(
            "request-3", invalid, "invalid-parent-response"
        )

    durable = progress.load()
    active = durable["active_batch_id"]
    assert active == resumed_parent["batch_id"]
    parent_state = next(
        item
        for item in durable["batches"]
        if item["batch_id"] == active
    )
    assert parent_state["status"] == "waiting_gpt"
    assert not runner.artifacts.exists(
        f"footage_semantic_batch_{resumed_parent['batch_id']}"
    )


def test_completed_refinement_is_checkpointed_into_resumed_parent_context(
    tmp_path: Path,
) -> None:
    runner = _runner_with_groups(tmp_path)
    progress = FootageSemanticProgress(
        runner, FootageBatchPolicy()
    )
    parent = progress.activate()
    progress.bind_request(
        parent["batch_id"],
        "request-1",
        parent["revision"],
        ["coarse"],
    )
    source = Path(
        runner.artifacts.read("footage_profiles_scaffold")["clips"][0][
            "path"
        ]
    )
    store = SampledFrameEvidenceStore(
        runner.project, runner.state.revision
    )
    group_id = _register_group(
        runner,
        store,
        source=source,
        clip_id="clip_001",
        start=4.0,
        end=5.0,
        sample_kind="dense_refinement",
        count=3,
        width=320,
        handoff_revision=1,
        label="checkpoint-refinement",
    )
    progress.defer_for_refinement(
        "request-1", [group_id], "refine-request"
    )
    replayed = progress.replayed_refinement(
        "request-1", "refine-request"
    )
    assert replayed is not None
    assert replayed["idempotent"] is True
    assert replayed["group_ids"] == [group_id]

    refinement = progress.activate()
    progress.bind_request(
        refinement["batch_id"],
        "request-2",
        refinement["revision"],
        ["dense"],
    )
    progress.complete(
        "request-2", _result_for(refinement), "dense-response"
    )
    resumed_parent = progress.activate()
    progress.bind_request(
        resumed_parent["batch_id"],
        "request-3",
        resumed_parent["revision"],
        ["coarse"],
    )
    compact = json.loads(
        progress.write_active_scaffold().read_text(encoding="utf-8")
    )

    completed = compact["analysis_meta"][
        "completed_refinement_results"
    ]
    assert resumed_parent["batch_id"] == parent["batch_id"]
    assert [item["batch_id"] for item in completed] == [
        refinement["batch_id"]
    ]
    assert (
        completed[0]["result"]["clips"][0]["segments"][0][
            "source_in"
        ]
        == 4.0
    )
