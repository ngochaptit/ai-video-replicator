"""Build deterministic reference-vs-draft frame evidence for Phase 5 GPT QC.

The tool does not judge semantic similarity. It pairs equivalent timeline
positions, reuses grounded Phase 1 reference evidence when available, extracts
draft/reference frames when needed, and reports technical duration drift. The
agent vision model performs the actual editorial comparison.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)


class ReplicationQCEvidenceBuilder(BaseTool):
    name = "replication_qc_evidence_builder"
    version = "0.1.0"
    tier = ToolTier.ANALYZE
    capability = "reference_replication_qc_evidence"
    provider = "openmontage"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["cmd:ffmpeg", "cmd:ffprobe"]
    install_instructions = "Install FFmpeg/ffprobe. No local AI model is required."
    agent_skills = ["ffmpeg", "video-understand"]
    capabilities = [
        "pair_reference_and_draft_timestamps",
        "extract_qc_frames",
        "reuse_reference_evidence",
        "measure_duration_drift",
    ]
    best_for = ["Phase 5 semantic QC evidence preparation"]
    not_good_for = ["semantic scoring", "automatic editorial judgment", "rendering"]

    input_schema = {
        "type": "object",
        "required": ["reference_blueprint_path", "replication_timeline_path", "draft_video_path", "output_dir"],
        "properties": {
            "reference_blueprint_path": {"type": "string"},
            "replication_timeline_path": {"type": "string"},
            "draft_video_path": {"type": "string"},
            "reference_video_path": {"type": "string"},
            "output_dir": {"type": "string"},
            "samples_per_segment": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
            "iteration": {"type": "integer", "minimum": 1, "default": 1},
        },
    }
    output_schema = {
        "type": "object",
        "description": "Replication QC Evidence — see schemas/artifacts/replication_qc_evidence.schema.json",
    }
    resource_profile = ResourceProfile(
        cpu_cores=2,
        ram_mb=1024,
        vram_mb=0,
        disk_mb=1000,
        network_required=False,
    )
    idempotency_key_fields = ["reference_blueprint_path", "replication_timeline_path", "draft_video_path", "iteration"]
    side_effects = ["writes paired QC frame images and qc_evidence.json"]
    user_visible_verification = [
        "Inspect paired reference/draft frames for equivalent timestamps",
        "Confirm duration_delta_seconds is technically plausible",
    ]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            return ToolResult(success=False, error="FFmpeg/ffprobe are required for QC evidence extraction")

        blueprint_path = Path(inputs["reference_blueprint_path"])
        timeline_path = Path(inputs["replication_timeline_path"])
        draft_path = Path(inputs["draft_video_path"])
        output_dir = Path(inputs["output_dir"])
        samples_per_segment = int(inputs.get("samples_per_segment", 3))
        iteration = int(inputs.get("iteration", 1))

        try:
            blueprint = self._read_json(blueprint_path)
            timeline = self._read_json(timeline_path)
            reference_path = Path(
                inputs.get("reference_video_path")
                or (blueprint.get("source") or {}).get("path")
                or ""
            )
            if not reference_path.is_file():
                raise OSError(f"Reference video not found: {reference_path}")
            if not draft_path.is_file():
                raise OSError(f"Draft video not found: {draft_path}")

            output_dir.mkdir(parents=True, exist_ok=True)
            reference_frames_dir = output_dir / "reference_frames"
            draft_frames_dir = output_dir / "draft_frames"
            reference_frames_dir.mkdir(parents=True, exist_ok=True)
            draft_frames_dir.mkdir(parents=True, exist_ok=True)

            evidence = self.build_evidence(
                blueprint,
                timeline,
                reference_video_path=reference_path,
                draft_video_path=draft_path,
                reference_frames_dir=reference_frames_dir,
                draft_frames_dir=draft_frames_dir,
                samples_per_segment=samples_per_segment,
                iteration=iteration,
                extract_frames=True,
            )
            self._validate_schema(evidence)
            output_path = output_dir / "qc_evidence.json"
            output_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
            return ToolResult(success=False, error=f"QC evidence build failed: {exc}")

        artifacts = [str(output_path)]
        for segment in evidence["segments"]:
            artifacts.extend(segment["reference_frames"])
            artifacts.extend(segment["draft_frames"])
        return ToolResult(success=True, data=evidence, artifacts=artifacts)

    def build_evidence(
        self,
        blueprint: dict[str, Any],
        timeline: dict[str, Any],
        *,
        reference_video_path: Path | str,
        draft_video_path: Path | str,
        reference_frames_dir: Path | str,
        draft_frames_dir: Path | str,
        samples_per_segment: int = 3,
        iteration: int = 1,
        extract_frames: bool = False,
        reference_duration_seconds: float | None = None,
        draft_duration_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not 1 <= samples_per_segment <= 5:
            raise ValueError("samples_per_segment must be between 1 and 5")
        reference_path = Path(reference_video_path)
        draft_path = Path(draft_video_path)
        ref_dir = Path(reference_frames_dir)
        draft_dir = Path(draft_frames_dir)

        blueprint_segments = {str(item["id"]): item for item in blueprint.get("segments") or []}
        timeline_segments = timeline.get("segments") or []
        if not blueprint_segments or not timeline_segments:
            raise ValueError("QC evidence requires Blueprint and timeline segments")

        reference_duration = (
            float(reference_duration_seconds)
            if reference_duration_seconds is not None
            else self._probe_duration(reference_path)
        )
        draft_duration = (
            float(draft_duration_seconds)
            if draft_duration_seconds is not None
            else self._probe_duration(draft_path)
        )
        duration_delta = abs(reference_duration - draft_duration)
        warnings: list[str] = []
        if duration_delta > 0.15:
            warnings.append(
                f"Draft duration differs from reference by {duration_delta:.3f}s; inspect timing fidelity before publish."
            )

        output_segments: list[dict[str, Any]] = []
        for order, timeline_segment in enumerate(timeline_segments, start=1):
            reference_id = str(timeline_segment.get("reference_segment_id") or "")
            reference_segment = blueprint_segments.get(reference_id)
            if reference_segment is None:
                raise ValueError(f"Timeline references unknown Blueprint segment: {reference_id}")
            start = float(timeline_segment["timeline_start"])
            end = float(timeline_segment["timeline_end"])
            if end <= start:
                raise ValueError(f"Invalid timeline range for {reference_id}")
            timestamps = self.sample_timestamps(start, end, samples_per_segment)

            reference_frames: list[str] = []
            draft_frames: list[str] = []
            for sample_index, timestamp in enumerate(timestamps, start=1):
                existing_ref = self._nearest_existing_reference_frame(reference_segment, timestamp)
                if existing_ref:
                    reference_frame = existing_ref
                else:
                    reference_frame_path = ref_dir / f"{order:03d}_{reference_id}_{sample_index:02d}.jpg"
                    reference_frame = str(reference_frame_path)
                    if extract_frames:
                        self._extract_frame(reference_path, timestamp, reference_frame_path)

                draft_frame_path = draft_dir / f"{order:03d}_{reference_id}_{sample_index:02d}.jpg"
                if extract_frames:
                    self._extract_frame(draft_path, timestamp, draft_frame_path)
                reference_frames.append(str(reference_frame))
                draft_frames.append(str(draft_frame_path))

            output_segments.append(
                {
                    "reference_segment_id": reference_id,
                    "start_seconds": round(start, 6),
                    "end_seconds": round(end, 6),
                    "sample_timestamps": timestamps,
                    "reference_frames": reference_frames,
                    "draft_frames": draft_frames,
                }
            )

        return {
            "version": "1.0",
            "reference_video_path": str(reference_path),
            "draft_video_path": str(draft_path),
            "reference_duration_seconds": round(reference_duration, 6),
            "draft_duration_seconds": round(draft_duration, 6),
            "duration_delta_seconds": round(duration_delta, 6),
            "segments": output_segments,
            "metadata": {
                "generated_by": self.name,
                "samples_per_segment": samples_per_segment,
                "semantic_review_required": True,
                "iteration": iteration,
                "warnings": warnings,
            },
        }

    @staticmethod
    def sample_timestamps(start: float, end: float, count: int) -> list[float]:
        if count < 1 or end <= start:
            raise ValueError("sample_timestamps requires count>=1 and end>start")
        duration = end - start
        return [round(start + duration * ((index + 1) / (count + 1)), 6) for index in range(count)]

    def _nearest_existing_reference_frame(self, segment: dict[str, Any], target: float) -> str | None:
        evidence = segment.get("evidence") or {}
        paths = evidence.get("frame_paths") or []
        timestamps = evidence.get("frame_timestamps") or []
        pairs = []
        for path, timestamp in zip(paths, timestamps):
            try:
                timestamp_value = float(timestamp)
            except (TypeError, ValueError):
                continue
            path_obj = Path(str(path))
            if path_obj.is_file():
                pairs.append((abs(timestamp_value - target), str(path_obj)))
        if not pairs:
            return None
        return min(pairs, key=lambda item: item[0])[1]

    def _extract_frame(self, video_path: Path, timestamp: float, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self.run_command([
            "ffmpeg", "-y",
            "-ss", f"{timestamp:.6f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-q:v", "2",
            str(output_path),
        ])
        if not output_path.is_file():
            raise OSError(f"FFmpeg did not create QC frame: {output_path}")

    @staticmethod
    def _probe_duration(path: Path) -> float:
        output = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
        value = float(output)
        if value <= 0:
            raise ValueError(f"Non-positive media duration: {path}")
        return value

    def _validate_schema(self, evidence: dict[str, Any]) -> None:
        try:
            import jsonschema
        except ImportError:
            return
        schema_path = Path(__file__).resolve().parents[2] / "schemas" / "artifacts" / "replication_qc_evidence.schema.json"
        schema = self._read_json(schema_path)
        try:
            jsonschema.validate(instance=evidence, schema=schema)
        except jsonschema.ValidationError as exc:
            raise ValueError(f"QC evidence schema validation failed: {exc.message}") from exc

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise OSError(f"JSON file not found: {path}")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError(f"Expected JSON object: {path}")
        return value
