"""Build evidence-grounded profiles for user footage used in reference replication.

Phase 2 keeps OpenMontage's agent-first separation intact: Python owns media
measurement, sampled evidence, invariant validation, and persistence. The agent
owns semantic action/camera/spatial interpretation and decides which portions of
footage are useful.
"""
from __future__ import annotations

from copy import deepcopy
import json
from numbers import Real
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


class FootageProfileBuilder(BaseTool):
    name = "footage_profile_builder"
    version = "0.1.0"
    tier = ToolTier.ANALYZE
    capability = "reference_replication_footage_analysis"
    provider = "openmontage"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["cmd:ffmpeg"]
    install_instructions = "Install FFmpeg and optional deterministic analysis dependencies used by video_analyzer."
    capabilities = [
        "scan_user_footage",
        "build_measured_footage_scaffold",
        "attach_in_range_frame_evidence",
        "apply_agent_semantic_enrichment",
    ]
    best_for = [
        "Phase 2 reference replication footage analysis",
        "turning a footage folder into searchable action segments",
    ]
    not_good_for = [
        "choosing the final reference match",
        "rendering",
        "semantic recognition without agent vision",
    ]

    input_schema = {
        "type": "object",
        "required": ["footage_dir"],
        "properties": {
            "footage_dir": {"type": "string"},
            "output_dir": {"type": "string"},
            "analysis_depth": {"type": "string", "enum": ["standard", "deep"], "default": "deep"},
            "max_keyframes_per_file": {"type": "integer", "minimum": 1, "maximum": 50, "default": 30},
            "max_analysis_window_seconds": {"type": "number", "minimum": 0.25, "maximum": 10.0, "default": 2.0},
            "semantic_enrichment_path": {
                "type": "string",
                "description": "Optional UTF-8 agent enrichment containing measured usable action segments.",
            },
        },
    }
    output_schema = {
        "type": "object",
        "description": "Footage Profiles artifact — see schemas/artifacts/footage_profiles.schema.json",
    }

    resource_profile = ResourceProfile(
        cpu_cores=2,
        ram_mb=2048,
        vram_mb=0,
        disk_mb=5000,
        network_required=False,
    )
    idempotency_key_fields = ["footage_dir", "analysis_depth", "max_analysis_window_seconds"]
    side_effects = ["writes per-file analysis artifacts and footage_profiles.json"]
    user_visible_verification = [
        "Confirm every intended footage file appears in clips",
        "Inspect representative evidence frames before semantic enrichment",
        "Verify final usable segment timestamps are measured and inside their source clips",
    ]

    VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        footage_dir = Path(inputs["footage_dir"])
        if not footage_dir.is_dir():
            return ToolResult(success=False, error=f"Footage directory not found: {footage_dir}")

        video_files = self._discover_videos(footage_dir)
        if not video_files:
            return ToolResult(success=False, error=f"No supported video files found in: {footage_dir}")

        output_dir = Path(inputs.get("output_dir") or "projects/_analysis/reference_replication/footage")
        output_dir.mkdir(parents=True, exist_ok=True)
        source_analysis_dir = output_dir / "source_analysis"
        source_analysis_dir.mkdir(parents=True, exist_ok=True)

        depth = inputs.get("analysis_depth", "deep")
        max_keyframes = int(inputs.get("max_keyframes_per_file", 30))
        max_window = float(inputs.get("max_analysis_window_seconds", 2.0))

        from tools.analysis.video_analyzer import VideoAnalyzer

        clips: list[dict[str, Any]] = []
        notes: list[str] = []
        artifacts: list[str] = []
        for index, path in enumerate(video_files, start=1):
            clip_id = f"clip_{index:03d}"
            clip_analysis_dir = source_analysis_dir / clip_id
            result = VideoAnalyzer().execute(
                {
                    "source": str(path),
                    "analysis_depth": depth,
                    "max_keyframes": max_keyframes,
                    "output_dir": str(clip_analysis_dir),
                }
            )
            if not result.success:
                notes.append(f"Skipped {path.name}: video_analyzer failed: {result.error}")
                continue

            clip = self._build_clip_scaffold(
                clip_id=clip_id,
                path=path,
                brief=result.data,
                max_window=max_window,
                analysis_path=clip_analysis_dir / "video_analysis_brief.json",
            )
            if clip["duration_seconds"] <= 0:
                notes.append(f"Skipped {path.name}: measured duration was not positive")
                continue
            clips.append(clip)
            artifacts.extend(result.artifacts or [])

        if not clips:
            return ToolResult(success=False, error="No footage files could be analyzed successfully")

        profiles = {
            "version": "1.0",
            "source_dir": str(footage_dir),
            "clips": clips,
            "analysis_meta": {
                "generated_by": self.name,
                "semantic_enrichment_required": True,
                "file_count": len(clips),
                "usable_segment_count": sum(len(clip["segments"]) for clip in clips),
                "notes": [
                    "Timing and evidence are deterministic scaffolding.",
                    "Agent vision must decide usable portions and semantic action boundaries.",
                    "Fixed analysis windows are evidence windows, not final semantic segments.",
                    *notes,
                ],
            },
        }

        enrichment_path = inputs.get("semantic_enrichment_path")
        if enrichment_path:
            try:
                enrichment = json.loads(Path(enrichment_path).read_text(encoding="utf-8-sig"))
                profiles = self.apply_semantic_enrichment(profiles, enrichment)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                return ToolResult(success=False, error=f"Footage semantic enrichment failed: {exc}")

        try:
            self._validate_profiles(profiles)
        except ValueError as exc:
            return ToolResult(success=False, error=f"Footage Profiles invariant failed: {exc}")

        out_path = output_dir / "footage_profiles.json"
        self._write_json_utf8(out_path, profiles)
        artifacts.insert(0, str(out_path))
        return ToolResult(success=True, data=profiles, artifacts=artifacts)

    def _discover_videos(self, footage_dir: Path) -> list[Path]:
        return sorted(
            path
            for path in footage_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in self.VIDEO_EXTENSIONS
        )

    def _build_clip_scaffold(
        self,
        clip_id: str,
        path: Path,
        brief: dict[str, Any],
        max_window: float,
        analysis_path: Path,
    ) -> dict[str, Any]:
        source = brief.get("source") or {}
        duration = float(source.get("duration_seconds") or 0.0)
        scenes = brief.get("structure_analysis", {}).get("scenes") or []
        keyframes = brief.get("keyframes") or []
        windows = self._build_windows(scenes, duration, max_window)
        if not windows and duration > 0:
            windows = [(0.0, duration, None, ["analysis_window", "video_end"])]

        segments: list[dict[str, Any]] = []
        for index, (start, end, scene_index, basis) in enumerate(windows, start=1):
            scene = self._scene_by_index(scenes, scene_index)
            segments.append(
                {
                    "id": f"{clip_id}_seg_{index:03d}",
                    "source_in": round(start, 6),
                    "source_out": round(end, 6),
                    "duration_seconds": round(end - start, 6),
                    "boundary_basis": basis,
                    "semantic": {
                        "actor": None,
                        "action": None,
                        "object": None,
                        "target": None,
                        "interaction": None,
                        "description": "",
                    },
                    "camera": {
                        "pov": None,
                        "shot_scale": None,
                        "angle": None,
                        "movement": None,
                        "steadiness": None,
                    },
                    "spatial": {
                        "actor_position": None,
                        "object_position": None,
                        "entry_direction": None,
                        "exit_direction": None,
                        "depth": None,
                        "framing_notes": "",
                    },
                    "motion": {
                        "motion_type": scene.get("motion_type") if scene else None,
                        "intensity": None,
                        "flow_variance": self._normalise_flow_variance(scene.get("flow_variance") if scene else None),
                        "speed_behavior": None,
                    },
                    "quality": {"score": None, "issues": [], "usable_notes": ""},
                    "evidence": self._evidence_for_window(keyframes, start, end, scene_index),
                    "confidence": {"timing": 1.0, "semantic": None, "camera": None, "overall": None},
                }
            )

        resolution = str(source.get("resolution") or "")
        return {
            "clip_id": clip_id,
            "path": str(path),
            "duration_seconds": duration,
            "resolution": resolution,
            "fps": float(source.get("fps") or 0.0),
            "orientation": self._orientation_from_resolution(resolution),
            "usable": True,
            "content_summary": "",
            "quality_risks": [],
            "segments": segments,
            "_analysis_path": str(analysis_path),
        }

    def _build_windows(
        self,
        scenes: list[dict[str, Any]],
        duration: float,
        max_window: float,
    ) -> list[tuple[float, float, int | None, list[str]]]:
        if duration <= 0:
            return []
        if not scenes:
            scenes = [{"scene_index": None, "start_time": 0.0, "end_time": duration}]

        windows: list[tuple[float, float, int | None, list[str]]] = []
        for pos, scene in enumerate(scenes):
            start = max(0.0, float(scene.get("start_time", scene.get("start_seconds", 0.0)) or 0.0))
            end = float(scene.get("end_time", scene.get("end_seconds", start)) or start)
            if pos == len(scenes) - 1:
                end = duration
            end = min(duration, max(start, end))
            scene_index = scene.get("scene_index", scene.get("index", pos))
            cursor = start
            first = True
            while cursor < end - 1e-9:
                window_end = min(end, cursor + max_window)
                basis = ["scene_cut" if first else "analysis_window"]
                if window_end < end - 1e-9:
                    basis.append("analysis_window")
                if window_end >= duration - 1e-9:
                    basis.append("video_end")
                windows.append((cursor, window_end, scene_index, list(dict.fromkeys(basis))))
                cursor = window_end
                first = False
        return windows

    def _evidence_for_window(
        self,
        keyframes: list[dict[str, Any]],
        start: float,
        end: float,
        scene_index: int | None,
    ) -> dict[str, Any]:
        selected = [
            frame
            for frame in keyframes
            if start - 1e-6 <= float(frame.get("timestamp", -1)) <= end + 1e-6
        ]
        return {
            "scene_index": scene_index,
            "frame_paths": [str(frame.get("path", "")) for frame in selected if frame.get("path")],
            "frame_timestamps": [round(float(frame.get("timestamp", 0)), 6) for frame in selected if frame.get("path")],
        }

    def apply_semantic_enrichment(
        self,
        profiles: dict[str, Any],
        enrichment: dict[str, Any],
    ) -> dict[str, Any]:
        clip_specs = enrichment.get("clips")
        if not isinstance(clip_specs, list) or not clip_specs:
            raise ValueError("semantic enrichment must contain a non-empty clips list")

        profile_by_id = {clip["clip_id"]: clip for clip in profiles.get("clips", [])}
        spec_by_id = {str(spec.get("clip_id")): spec for spec in clip_specs if isinstance(spec, dict)}
        if set(spec_by_id) != set(profile_by_id):
            missing = sorted(set(profile_by_id) - set(spec_by_id))
            extra = sorted(set(spec_by_id) - set(profile_by_id))
            raise ValueError(f"enrichment clip ids must exactly match analyzed clips; missing={missing}, extra={extra}")

        global_defaults = enrichment.get("defaults") or {}
        enriched_clips: list[dict[str, Any]] = []
        all_segment_ids: set[str] = set()

        for clip_id, clip in profile_by_id.items():
            spec = spec_by_id[clip_id]
            result_clip = deepcopy(clip)
            result_clip.pop("_analysis_path", None)
            result_clip["usable"] = bool(spec.get("usable", True))
            result_clip["content_summary"] = str(spec.get("content_summary") or "")
            result_clip["quality_risks"] = [str(item) for item in spec.get("quality_risks") or []]

            if not result_clip["usable"]:
                result_clip["segments"] = []
                enriched_clips.append(result_clip)
                continue

            segment_specs = spec.get("segments")
            if not isinstance(segment_specs, list) or not segment_specs:
                raise ValueError(f"usable clip {clip_id} requires at least one semantic segment")

            evidence_catalog = self._build_evidence_catalog(clip, enrichment, clip_id)
            duration = float(clip["duration_seconds"])
            measured = {0.0, duration}
            measured.update(item["timestamp"] for item in evidence_catalog)
            for scaffold in clip.get("segments", []):
                if "scene_cut" in scaffold.get("boundary_basis", []):
                    measured.add(float(scaffold["source_in"]))

            clip_defaults = self._merge_section(global_defaults, spec.get("defaults"))
            refined: list[dict[str, Any]] = []
            previous_end = -1.0
            for index, segment_spec in enumerate(segment_specs, start=1):
                start = round(float(segment_spec["source_in"]), 6)
                end = round(float(segment_spec["source_out"]), 6)
                if start < 0 or end > duration + 1e-6 or end <= start:
                    raise ValueError(f"{clip_id} segment {index} has invalid source range [{start}, {end}]")
                if previous_end >= 0 and start < previous_end - 1e-6:
                    raise ValueError(f"{clip_id} segment {index} overlaps the previous usable segment")
                if not self._timestamp_is_measured(start, measured) or not self._timestamp_is_measured(end, measured):
                    raise ValueError(f"{clip_id} segment {index} boundary is not a measured timestamp")

                basis = list(dict.fromkeys(segment_spec.get("boundary_basis") or []))
                if not basis:
                    raise ValueError(f"{clip_id} segment {index} requires boundary_basis")

                segment_evidence = [
                    item
                    for item in evidence_catalog
                    if start - 1e-6 <= item["timestamp"] <= end + 1e-6
                ]
                if not segment_evidence:
                    raise ValueError(f"{clip_id} segment {index} has no frame evidence inside its source range")

                motion = self._motion_for_interval(clip, start, end)
                motion = self._merge_section(motion, clip_defaults.get("motion"), segment_spec.get("motion"))
                motion["flow_variance"] = self._normalise_flow_variance(motion.get("flow_variance"))

                segment_id = str(segment_spec.get("id") or f"{clip_id}_seg_{index:03d}")
                if segment_id in all_segment_ids:
                    raise ValueError(f"duplicate footage segment id: {segment_id}")
                all_segment_ids.add(segment_id)

                refined.append(
                    {
                        "id": segment_id,
                        "source_in": start,
                        "source_out": end,
                        "duration_seconds": round(end - start, 6),
                        "boundary_basis": basis,
                        "semantic": self._merge_section(
                            {"actor": None, "action": None, "object": None, "target": None, "interaction": None, "description": ""},
                            clip_defaults.get("semantic"),
                            segment_spec.get("semantic"),
                        ),
                        "camera": self._merge_section(
                            {"pov": None, "shot_scale": None, "angle": None, "movement": None, "steadiness": None},
                            clip_defaults.get("camera"),
                            segment_spec.get("camera"),
                        ),
                        "spatial": self._merge_section(
                            {"actor_position": None, "object_position": None, "entry_direction": None, "exit_direction": None, "depth": None, "framing_notes": ""},
                            clip_defaults.get("spatial"),
                            segment_spec.get("spatial"),
                        ),
                        "motion": self._merge_section(
                            {"motion_type": None, "intensity": None, "flow_variance": None, "speed_behavior": None},
                            motion,
                        ),
                        "quality": self._merge_section(
                            {"score": None, "issues": [], "usable_notes": ""},
                            clip_defaults.get("quality"),
                            segment_spec.get("quality"),
                        ),
                        "evidence": {
                            "scene_index": segment_spec.get("scene_index", self._scene_index_for_interval(clip, start, end)),
                            "frame_paths": [item["path"] for item in segment_evidence],
                            "frame_timestamps": [item["timestamp"] for item in segment_evidence],
                        },
                        "confidence": self._merge_section(
                            {"timing": 1.0, "semantic": None, "camera": None, "overall": None},
                            clip_defaults.get("confidence"),
                            segment_spec.get("confidence"),
                        ),
                    }
                )
                previous_end = end

            result_clip["segments"] = refined
            enriched_clips.append(result_clip)

        result = deepcopy(profiles)
        result["clips"] = enriched_clips
        result["analysis_meta"]["semantic_enrichment_required"] = False
        result["analysis_meta"]["usable_segment_count"] = sum(len(clip["segments"]) for clip in enriched_clips)
        result["analysis_meta"].setdefault("notes", []).extend(enrichment.get("analysis_notes") or [])
        result["analysis_meta"]["notes"].append(
            "Agent enrichment selected measured usable action segments; gaps in source clips are allowed because unusable footage need not be matched."
        )
        self._validate_profiles(result)
        return result

    def _build_evidence_catalog(
        self,
        clip: dict[str, Any],
        enrichment: dict[str, Any],
        clip_id: str,
    ) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        for segment in clip.get("segments", []):
            evidence = segment.get("evidence") or {}
            for path, timestamp in zip(evidence.get("frame_paths") or [], evidence.get("frame_timestamps") or []):
                catalog.append(
                    {
                        "path": str(path),
                        "timestamp": round(float(timestamp), 6),
                        "scene_index": evidence.get("scene_index"),
                    }
                )
        for item in enrichment.get("evidence_catalog") or []:
            if str(item.get("clip_id")) != clip_id or not item.get("path"):
                continue
            catalog.append(
                {
                    "path": str(item["path"]),
                    "timestamp": round(float(item["timestamp"]), 6),
                    "scene_index": item.get("scene_index"),
                }
            )
        unique = {(item["path"], item["timestamp"]): item for item in catalog}
        return sorted(unique.values(), key=lambda item: (item["timestamp"], item["path"]))

    def _motion_for_interval(self, clip: dict[str, Any], start: float, end: float) -> dict[str, Any]:
        overlapping = [
            segment.get("motion") or {}
            for segment in clip.get("segments", [])
            if float(segment.get("source_in", 0)) < end and float(segment.get("source_out", 0)) > start
        ]
        motion_types = {
            motion.get("motion_type")
            for motion in overlapping
            if motion.get("motion_type") not in (None, "unknown")
        }
        variances = [
            value
            for value in (self._normalise_flow_variance(motion.get("flow_variance")) for motion in overlapping)
            if value is not None
        ]
        return {
            "motion_type": next(iter(motion_types)) if len(motion_types) == 1 else None,
            "intensity": None,
            "flow_variance": round(sum(variances) / len(variances), 6) if variances else None,
            "speed_behavior": None,
        }

    def _scene_index_for_interval(self, clip: dict[str, Any], start: float, end: float) -> int | None:
        midpoint = (start + end) / 2
        for segment in clip.get("segments", []):
            if float(segment.get("source_in", 0)) <= midpoint <= float(segment.get("source_out", 0)):
                return segment.get("evidence", {}).get("scene_index")
        return None

    def _validate_profiles(self, profiles: dict[str, Any]) -> None:
        clips = profiles.get("clips") or []
        if not clips:
            raise ValueError("footage_profiles must contain at least one analyzed clip")
        semantic_complete = not profiles.get("analysis_meta", {}).get("semantic_enrichment_required", True)
        seen_segments: set[str] = set()
        usable_count = 0
        for clip in clips:
            duration = float(clip.get("duration_seconds") or 0.0)
            if duration <= 0:
                raise ValueError(f"clip {clip.get('clip_id')} has non-positive duration")
            segments = clip.get("segments") or []
            if semantic_complete and clip.get("usable") and not segments:
                raise ValueError(f"usable clip {clip.get('clip_id')} has no usable segments")
            previous_end = -1.0
            for segment in segments:
                segment_id = str(segment.get("id"))
                if segment_id in seen_segments:
                    raise ValueError(f"duplicate footage segment id: {segment_id}")
                seen_segments.add(segment_id)
                start = float(segment["source_in"])
                end = float(segment["source_out"])
                if start < 0 or end > duration + 1e-6 or end <= start:
                    raise ValueError(f"segment {segment_id} is outside source clip range")
                if semantic_complete and previous_end >= 0 and start < previous_end - 1e-6:
                    raise ValueError(f"segment {segment_id} overlaps previous usable segment")
                paths = segment.get("evidence", {}).get("frame_paths") or []
                timestamps = segment.get("evidence", {}).get("frame_timestamps") or []
                if len(paths) != len(timestamps):
                    raise ValueError(f"segment {segment_id} evidence path/timestamp counts differ")
                outside = [ts for ts in timestamps if float(ts) < start - 1e-6 or float(ts) > end + 1e-6]
                if outside:
                    raise ValueError(f"segment {segment_id} has out-of-range evidence timestamps: {outside}")
                flow_variance = segment.get("motion", {}).get("flow_variance")
                if flow_variance is not None and (
                    not isinstance(flow_variance, Real)
                    or isinstance(flow_variance, bool)
                    or float(flow_variance) < 0
                ):
                    raise ValueError(f"segment {segment_id} has invalid flow_variance {flow_variance}")
                previous_end = end
                usable_count += 1
        if semantic_complete and usable_count <= 0:
            raise ValueError("semantic enrichment produced zero usable footage segments")

    @staticmethod
    def _merge_section(base: dict[str, Any], *updates: Any) -> dict[str, Any]:
        merged = deepcopy(base)
        for update in updates:
            if isinstance(update, dict):
                merged.update(deepcopy(update))
        return merged

    @staticmethod
    def _normalise_flow_variance(value: Any) -> float | None:
        if not isinstance(value, Real) or isinstance(value, bool) or float(value) < 0:
            return None
        return float(value)

    @staticmethod
    def _orientation_from_resolution(resolution: str) -> str:
        try:
            width_text, height_text = resolution.lower().split("x", 1)
            width, height = int(width_text), int(height_text)
        except (ValueError, AttributeError):
            return "unknown"
        if width == height:
            return "square"
        return "vertical" if height > width else "horizontal"

    @classmethod
    def _timestamp_is_measured(cls, timestamp: float, measured: set[float]) -> bool:
        return any(abs(float(timestamp) - float(candidate)) <= 1e-6 for candidate in measured)

    @staticmethod
    def _write_json_utf8(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")

    @staticmethod
    def _scene_by_index(scenes: list[dict[str, Any]], scene_index: int | None) -> dict[str, Any] | None:
        if scene_index is None:
            return None
        for pos, scene in enumerate(scenes):
            if scene.get("scene_index", scene.get("index", pos)) == scene_index:
                return scene
        return None
