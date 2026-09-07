"""Persistent, measured bridge coverage supplemental to VideoAnalyzer keyframes."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from moon.runner.pipeline import PipelineRunner

ARTIFACT = "reference_bridge_frames"


def _image_exists(path: str, root: Path) -> bool:
    resolved = Path(path).resolve()
    return (resolved.is_relative_to(root.resolve()) and resolved.is_file()
            and resolved.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            and resolved.stat().st_size > 0)


def ensure_reference_coverage(runner: PipelineRunner) -> list[dict[str, Any]]:
    from tools.analysis.frame_sampler import FrameSampler

    scaffold = runner.artifacts.read("reference_blueprint_scaffold")
    signature = hashlib.sha256(json.dumps(scaffold, sort_keys=True).encode()).hexdigest()
    stored = runner.artifacts.read(ARTIFACT) if runner.artifacts.exists(ARTIFACT) else {}
    supplemental = stored.get("frames", []) if stored.get("scaffold_sha256") == signature else []
    supplemental = [frame for frame in supplemental if _image_exists(frame["path"], runner.project.root)]
    frames = []
    for segment in scaffold["segments"]:
        evidence = segment["evidence"]
        for path, timestamp in zip(evidence["frame_paths"], evidence["frame_timestamps"]):
            if segment["start_seconds"] <= timestamp <= segment["end_seconds"] and _image_exists(path, runner.project.root):
                frames.append({"path": path, "timestamp_seconds": timestamp, "origin": "video_analyzer"})
    frames.extend(supplemental)
    for segment in scaffold["segments"]:
        start, end = segment["start_seconds"], segment["end_seconds"]
        if any(start <= frame["timestamp_seconds"] <= end for frame in frames):
            continue
        timestamp = (start + end) / 2
        if not start < timestamp < end:
            raise ValueError(f"cannot sample interior reference timestamp for segment {segment['id']}")
        # Separate stable directories prevent frame_0000.jpg overwrites on retry.
        key = hashlib.sha256(timestamp.hex().encode()).hexdigest()[:16]
        output = runner.project.root / "analysis/reference/source_analysis/bridge_frames" / signature[:16] / key
        result = FrameSampler().execute({
            "input_path": str(runner.project.root / "reference.mp4"), "strategy": "timestamps",
            "timestamps": [timestamp], "output_dir": str(output), "format": "jpg",
        })
        sampled = (result.data or {}).get("frames", []) if result.success else []
        matched = next((frame for frame in sampled if frame.get("timestamp_seconds") == timestamp
                        and _image_exists(frame.get("path", ""), runner.project.root)), None)
        if matched is None:
            raise ValueError(f"reference coverage sampling failed at {timestamp}s: {result.error or 'no measured image returned'}")
        frame = {"path": str(Path(matched["path"]).resolve()), "timestamp_seconds": timestamp,
                 "origin": "bridge_coverage", "segment_id": segment["id"]}
        supplemental.append(frame)
        frames.append(frame)
        # Persist each success so a later sampling failure can be retried cheaply.
        runner.artifacts.write(ARTIFACT, {"scaffold_sha256": signature, "frames": supplemental})
    return list({(frame["path"], frame["timestamp_seconds"]): frame for frame in frames}.values())


def measured_reference_scaffold(runner: PipelineRunner) -> dict[str, Any]:
    """Combine trusted measured evidence without modifying the original scaffold."""
    frames = ensure_reference_coverage(runner)
    scaffold = deepcopy(runner.artifacts.read("reference_blueprint_scaffold"))
    for segment in scaffold["segments"]:
        inside = [frame for frame in frames
                  if segment["start_seconds"] <= frame["timestamp_seconds"] <= segment["end_seconds"]]
        segment["evidence"]["frame_paths"] = [frame["path"] for frame in inside]
        segment["evidence"]["frame_timestamps"] = [frame["timestamp_seconds"] for frame in inside]
    return scaffold
