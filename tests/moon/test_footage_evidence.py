from __future__ import annotations

from pathlib import Path

from moon.core.project import MoonProject
from moon.footage_evidence import FootageEvidencePlanner


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
