from __future__ import annotations

import hashlib
import json
from pathlib import Path

from moon.core.project import MoonProject
from moon.evidence import SampledFrameEvidenceStore
from moon.footage_batches import FootageBatchPolicy, FootageSemanticProgress
from moon.runner.pipeline import PipelineRunner


def _event(
    *,
    group_id: str,
    source: Path,
    clip_id: str,
    frame_paths: list[Path],
    timestamps: list[float],
    start: float,
    end: float,
    count: int,
    width: int,
    sample_kind: str | None = None,
) -> dict:
    payload = {
        "schema_version": 1,
        "event_type": "sampled_frame_group",
        "stage": "footage",
        "pipeline_revision": 0,
        "group_id": group_id,
        "sampling_method": "ffmpeg_single_frame_seek_v1",
        "checkpoint_state": "completed",
        "source": {
            "clip_id": clip_id,
            "path": source.relative_to(source.parents[1]).as_posix(),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
        "request": {
            "start_seconds": start,
            "end_seconds": end,
            "count": count,
            "width": width,
        },
        "frames": [
            {
                "timestamp_seconds": timestamp,
                "path": path.relative_to(source.parents[1]).as_posix(),
            }
            for path, timestamp in zip(frame_paths, timestamps)
        ],
    }
    if sample_kind is not None:
        payload["sample_kind"] = sample_kind
        payload["handoff_revision"] = 0 if sample_kind == "coarse" else 1
    return payload


def test_migrated_legacy_refinement_is_not_bootstrapped_as_coarse_and_duplicate_coarse_is_collapsed(
    tmp_path: Path,
) -> None:
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))
    source = tmp_path / "footage" / "clip.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    runner.artifacts.write(
        "footage_profiles_scaffold",
        {
            "version": "1.0",
            "source_dir": "footage",
            "clips": [
                {
                    "clip_id": "clip_001",
                    "path": str(source),
                    "duration_seconds": 20.0,
                    "segments": [],
                }
            ],
        },
    )

    root = tmp_path / ".moon" / "cache" / "connector-frames" / "footage" / "revision_000"
    old_coarse_root = root / "legacy-coarse"
    new_coarse_root = root / "typed-coarse"
    refinement_root = root / "refinement_001" / "legacy-refinement"
    for folder in (old_coarse_root, new_coarse_root, refinement_root):
        folder.mkdir(parents=True, exist_ok=True)

    timestamps = [0.0, 4.0, 8.0]
    old_frames = []
    new_frames = []
    for index, timestamp in enumerate(timestamps, start=1):
        old = old_coarse_root / f"frame_{index}.jpg"
        new = new_coarse_root / f"frame_{index}.jpg"
        old.write_bytes(f"same-{timestamp}".encode())
        new.write_bytes(f"same-{timestamp}".encode())
        old_frames.append(old)
        new_frames.append(new)

    refinement_frames = []
    for index, timestamp in enumerate((0.0, 5.0, 9.999), start=1):
        frame = refinement_root / f"frame_{index}.jpg"
        frame.write_bytes(f"refine-{timestamp}".encode())
        refinement_frames.append(frame)

    store = SampledFrameEvidenceStore(runner.project, 0)
    registry = store.registry_path("footage")
    registry.parent.mkdir(parents=True, exist_ok=True)
    events = [
        _event(
            group_id="legacy-coarse",
            source=source,
            clip_id="clip_001",
            frame_paths=old_frames,
            timestamps=timestamps,
            start=0.0,
            end=8.0,
            count=3,
            width=320,
        ),
        _event(
            group_id="typed-coarse",
            source=source,
            clip_id="clip_001",
            frame_paths=new_frames,
            timestamps=timestamps,
            start=0.0,
            end=8.0,
            count=3,
            width=320,
            sample_kind="coarse",
        ),
        _event(
            group_id="legacy-refinement",
            source=source,
            clip_id="clip_001",
            frame_paths=refinement_frames,
            timestamps=[0.0, 5.0, 9.999],
            start=0.0,
            end=10.0,
            count=3,
            width=640,
        ),
    ]
    registry.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in events),
        encoding="utf-8",
    )

    available = store.available("footage")
    by_id = {group["group_id"]: group for group in available["groups"]}

    assert set(by_id) == {"typed-coarse", "legacy-refinement"}
    assert by_id["typed-coarse"]["sample_kind"] == "coarse"
    assert by_id["legacy-refinement"]["sample_kind"] == "dense_refinement"
    assert available["frame_count"] == 6

    progress = FootageSemanticProgress(
        runner, FootageBatchPolicy(max_clips=3, max_refinement_ranges=3, max_frames=60)
    ).ensure()
    assert progress is not None
    assert len(progress["batches"]) == 1
    batch = progress["batches"][0]
    assert batch["batch_type"] == "coarse"
    assert batch["frame_count"] == 3
    assert [item["group_id"] for item in batch["ranges"]] == ["typed-coarse"]
