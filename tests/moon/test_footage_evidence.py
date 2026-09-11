from __future__ import annotations

from pathlib import Path

import pytest

from moon.core.project import MoonProject
from moon.footage_evidence import FootageEvidencePlanner
from moon.footage_refinement import FootageRefinementService
from moon.runner.pipeline import PipelineRunner


def _scaffold(project: MoonProject, *, duration: float = 283.214567) -> dict:
    source = project.root / "footage" / "oneshot.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"video")
    return {
        "clips": [
            {
                "clip_id": "clip_001",
                "path": str(source),
                "duration_seconds": duration,
                "segments": [],
            }
        ]
    }


def test_plan_283s_clip_keeps_initial_evidence_dense() -> None:
    plan = FootageEvidencePlanner.plan_clip(283.214567)

    assert plan["spacing_seconds"] == 4.0
    assert plan["estimated_unique_frames"] >= 70
    assert plan["estimated_unique_frames"] <= 120
    assert plan["groups"][0]["start_seconds"] == 0.0
    assert plan["groups"][-1]["end_seconds"] == 283.214567
    assert all(group["count"] <= 24 for group in plan["groups"])
    assert all(
        (group["end_seconds"] - group["start_seconds"]) / (group["count"] - 1) <= 4.001
        for group in plan["groups"]
    )


def test_seed_is_idempotent_and_persists_builder_catalog(tmp_path, monkeypatch) -> None:
    project = MoonProject.open(tmp_path, create=True)
    scaffold = _scaffold(project, duration=20.0)
    planner = FootageEvidencePlanner(project, 0)
    calls = []

    def fake_sample(source, output_dir, *, start_seconds, end_seconds, count, width):
        calls.append((start_seconds, end_seconds, count, width))
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        step = (end_seconds - start_seconds) / (count - 1)
        frames = []
        for index in range(count):
            timestamp = start_seconds + step * index
            path = output_dir / f"frame_{index:03d}.jpg"
            path.write_bytes(b"jpg")
            frames.append({"timestamp_seconds": timestamp, "path": str(path)})
        return {
            "source": str(source),
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "window_seconds": end_seconds - start_seconds,
            "count": count,
            "width": width,
            "frames": frames,
        }

    monkeypatch.setattr("moon.footage_evidence.sample_frames", fake_sample)

    first = planner.seed(scaffold)
    second = planner.seed(scaffold)
    catalog = planner.evidence_catalog(scaffold)

    assert first["seeded_groups"] > 0
    assert second["seeded_groups"] == 0
    assert second["skipped_existing_groups"] == first["seeded_groups"]
    assert len(calls) == first["seeded_groups"]
    assert len(catalog) >= 6
    assert catalog[0]["clip_id"] == "clip_001"
    assert Path(catalog[0]["path"]).is_file()
    assert planner.coverage_summary(scaffold)[0]["max_gap_seconds"] <= 4.001


def test_seed_cache_invalidates_when_source_changes(tmp_path, monkeypatch) -> None:
    project = MoonProject.open(tmp_path, create=True)
    scaffold = _scaffold(project, duration=4.0)
    planner = FootageEvidencePlanner(project, 0)
    calls = []

    def fake_sample(source, output_dir, *, start_seconds, end_seconds, count, width):
        calls.append(Path(source).read_bytes())
        output_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for index, timestamp in enumerate((start_seconds, end_seconds)):
            path = output_dir / f"frame_{index}.jpg"
            path.write_bytes(f"{calls[-1]!r}-{index}".encode())
            frames.append({"timestamp_seconds": timestamp, "path": str(path)})
        return {
            "source": str(source),
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "count": count,
            "width": width,
            "frames": frames,
        }

    monkeypatch.setattr("moon.footage_evidence.sample_frames", fake_sample)
    assert planner.seed(scaffold)["seeded_groups"] == 1
    source = project.root / "footage" / "oneshot.mp4"
    source.write_bytes(b"changed-video")
    assert planner.seed(scaffold)["seeded_groups"] == 1

    selected = planner.handoff_evidence(0)
    assert calls == [b"video", b"changed-video"]
    assert len(selected["groups"]) == 1
    assert selected["groups"][0]["source"]["sha256"] == planner.store.source_fingerprint(source)


def test_missing_completed_range_is_reprocessed(tmp_path, monkeypatch) -> None:
    project = MoonProject.open(tmp_path, create=True)
    scaffold = _scaffold(project, duration=4.0)
    planner = FootageEvidencePlanner(project, 0)
    calls = []

    def fake_sample(source, output_dir, *, start_seconds, end_seconds, count, width):
        calls.append(1)
        output_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for index, timestamp in enumerate((start_seconds, end_seconds)):
            path = output_dir / f"frame_{index}.jpg"
            path.write_bytes(b"jpg")
            frames.append({"timestamp_seconds": timestamp, "path": str(path)})
        return {
            "source": str(source), "start_seconds": start_seconds,
            "end_seconds": end_seconds, "count": count, "width": width,
            "frames": frames,
        }

    monkeypatch.setattr("moon.footage_evidence.sample_frames", fake_sample)
    planner.seed(scaffold)
    Path(planner.handoff_evidence(0)["groups"][0]["frames"][0]["absolute_path"]).unlink()
    result = planner.seed(scaffold)

    assert result["seeded_groups"] == 1
    assert len(calls) == 2


def test_refinement_resumes_without_reprocessing_completed_range(tmp_path, monkeypatch) -> None:
    project = MoonProject.open(tmp_path, create=True)
    scaffold = _scaffold(project, duration=10.0)
    runner = PipelineRunner(project)
    runner.artifacts.write("footage_profiles_scaffold", scaffold)
    calls: list[float] = []

    def fake_sample(source, output_dir, *, start_seconds, end_seconds, count, width):
        calls.append(start_seconds)
        if start_seconds == 5.0 and calls.count(5.0) == 1:
            raise RuntimeError("interrupted")
        output_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for index in range(count):
            timestamp = start_seconds + (end_seconds - start_seconds) * index / (count - 1)
            path = output_dir / f"frame_{index}.jpg"
            path.write_bytes(b"jpg")
            frames.append({"timestamp_seconds": timestamp, "path": str(path)})
        return {
            "source": str(source), "start_seconds": start_seconds,
            "end_seconds": end_seconds, "count": count, "width": width,
            "frames": frames,
        }

    monkeypatch.setattr("moon.footage_refinement.sample_frames", fake_sample)
    requests = [
        {"clip_id": "clip_001", "start_seconds": 1.0, "end_seconds": 2.0, "reason": "a"},
        {"clip_id": "clip_001", "start_seconds": 5.0, "end_seconds": 6.0, "reason": "b"},
    ]
    with pytest.raises(RuntimeError, match="interrupted"):
        FootageRefinementService(runner).sample(requests, handoff_revision=1)

    result = FootageRefinementService(runner).sample(requests, handoff_revision=1)

    assert calls == [1.0, 5.0, 5.0]
    assert result["sampled_groups"] == 1
    assert result["skipped_existing_groups"] == 1
