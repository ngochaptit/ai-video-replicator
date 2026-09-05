"""Build a deterministic Reference Blueprint scaffold from a reference video.

This tool deliberately does NOT infer semantic actions. OpenMontage's agent-first
architecture keeps creative/semantic reasoning in skills. Python is responsible
for measurable timing, scene/motion evidence, frame references, and a stable
artifact shape that the multimodal agent enriches afterward.
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


class ReferenceBlueprintBuilder(BaseTool):
    name = "reference_blueprint_builder"
    version = "0.2.0"
    tier = ToolTier.ANALYZE
    capability = "reference_replication_analysis"
    provider = "openmontage"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["cmd:ffmpeg"]
    install_instructions = "Install FFmpeg and optional analysis dependencies used by video_analyzer."
    capabilities = [
        "build_reference_blueprint_scaffold",
        "preserve_reference_timing",
        "attach_keyframe_evidence",
        "split_long_scenes_into_analysis_windows",
        "apply_validated_semantic_enrichment",
    ]
    best_for = [
        "Phase 1 reference replication analysis",
        "creating a stable timing/evidence scaffold before agent vision enrichment",
    ]
    not_good_for = [
        "semantic action recognition without agent vision",
        "footage matching",
        "rendering",
    ]

    input_schema = {
        "type": "object",
        "required": ["source"],
        "properties": {
            "source": {"type": "string", "description": "Local reference video path or supported URL"},
            "output_dir": {"type": "string"},
            "analysis_depth": {
                "type": "string",
                "enum": ["standard", "deep"],
                "default": "deep",
            },
            "max_keyframes": {"type": "integer", "minimum": 1, "maximum": 50, "default": 40},
            "max_analysis_window_seconds": {
                "type": "number",
                "minimum": 0.25,
                "maximum": 10.0,
                "default": 2.0,
                "description": "Long detected scenes are subdivided so choreography analysis is not hard-cut-only.",
            },
            "semantic_enrichment_path": {
                "type": "string",
                "description": "Optional UTF-8 JSON semantic enrichment with measured action boundaries and evidence catalog.",
            },
        },
    }
    output_schema = {
        "type": "object",
        "description": "Reference Blueprint scaffold; semantic fields are intentionally blank pending agent enrichment.",
    }

    resource_profile = ResourceProfile(
        cpu_cores=2,
        ram_mb=2048,
        vram_mb=0,
        disk_mb=3500,
        network_required=False,
    )
    idempotency_key_fields = ["source", "analysis_depth", "max_analysis_window_seconds"]
    side_effects = ["writes video analysis artifacts and reference_blueprint.json"]
    user_visible_verification = [
        "Check that segment timestamps cover the full reference without gaps",
        "Inspect evidence keyframes before semantic enrichment",
        "Verify long scenes are subdivided instead of treated as one action",
    ]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        source = inputs["source"]
        depth = inputs.get("analysis_depth", "deep")
        max_keyframes = int(inputs.get("max_keyframes", 40))
        max_window = float(inputs.get("max_analysis_window_seconds", 2.0))

        output_dir = Path(inputs.get("output_dir") or "projects/_analysis/reference_replication")
        output_dir.mkdir(parents=True, exist_ok=True)
        analysis_dir = output_dir / "source_analysis"

        from tools.analysis.video_analyzer import VideoAnalyzer

        analysis_result = VideoAnalyzer().execute(
            {
                "source": source,
                "analysis_depth": depth,
                "max_keyframes": max_keyframes,
                "output_dir": str(analysis_dir),
            }
        )
        if not analysis_result.success:
            return ToolResult(success=False, error=analysis_result.error)

        brief = analysis_result.data
        duration = float(brief.get("source", {}).get("duration_seconds") or 0.0)
        scenes = brief.get("structure_analysis", {}).get("scenes") or []
        keyframes = brief.get("keyframes") or []

        windows = self._build_windows(scenes, duration, max_window)
        if not windows and duration > 0:
            windows = [(0.0, duration, None, ["analysis_window", "video_end"])]

        segments = []
        for idx, (start, end, scene_index, basis) in enumerate(windows, start=1):
            evidence = self._evidence_for_window(keyframes, start, end, scene_index)
            scene = self._scene_by_index(scenes, scene_index)
            segments.append(
                {
                    "id": f"seg_{idx:03d}",
                    "start_seconds": round(start, 6),
                    "end_seconds": round(end, 6),
                    "duration_seconds": round(max(0.0, end - start), 6),
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
                        "playback_speed": None,
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
                        "flow_variance": self._normalise_flow_variance(
                            scene.get("flow_variance") if scene else None
                        ),
                        "speed_behavior": None,
                    },
                    "edit": {
                        "transition_in": "cut" if idx > 1 and "scene_cut" in basis else None,
                        "transition_out": None,
                        "segment_role": None,
                    },
                    "text": {"content": None, "position": None, "timing_notes": ""},
                    "audio": {"speech": None, "sound_cue": None, "beat_cue": None, "energy_notes": ""},
                    "evidence": evidence,
                    "confidence": {
                        "timing": 1.0,
                        "semantic": None,
                        "camera": None,
                        "overall": None,
                    },
                }
            )

        blueprint = {
            "version": "1.0",
            "source": {
                "path": brief.get("source", {}).get("local_path") or brief.get("source", {}).get("url") or source,
                "duration_seconds": duration,
                "resolution": brief.get("source", {}).get("resolution", ""),
                "fps": 0,
                "orientation": "unknown",
            },
            "segments": segments,
            "choreography": {
                "summary": "",
                "action_order": [],
                "critical_constraints": [],
                "soft_constraints": [],
            },
            "analysis_meta": {
                "generated_by": self.name,
                "semantic_enrichment_required": True,
                "source_analysis_path": str(analysis_dir / "video_analysis_brief.json"),
                "notes": [
                    "Timing and evidence are deterministic scaffolding.",
                    "Agent vision must refine action boundaries where choreography changes inside an analysis window.",
                    "Do not invent timestamps: semantic refinements must remain grounded in sampled evidence and measured media time.",
                ],
            },
        }

        enrichment_path = inputs.get("semantic_enrichment_path")
        if enrichment_path:
            try:
                enrichment = json.loads(Path(enrichment_path).read_text(encoding="utf-8-sig"))
                blueprint = self.apply_semantic_enrichment(blueprint, enrichment)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                return ToolResult(success=False, error=f"Semantic enrichment failed: {exc}")

        out_path = output_dir / "reference_blueprint.json"
        try:
            self._validate_blueprint_invariants(blueprint)
        except ValueError as exc:
            return ToolResult(success=False, error=f"Reference Blueprint invariant failed: {exc}")
        self._write_json_utf8(out_path, blueprint)
        return ToolResult(
            success=True,
            data=blueprint,
            artifacts=[str(out_path), str(analysis_dir / "video_analysis_brief.json"), str(analysis_dir / "keyframes")],
        )

    def _build_windows(
        self,
        scenes: list[dict[str, Any]],
        duration: float,
        max_window: float,
    ) -> list[tuple[float, float, int | None, list[str]]]:
        windows: list[tuple[float, float, int | None, list[str]]] = []
        if not scenes:
            if duration <= 0:
                return windows
            start = 0.0
            while start < duration:
                end = min(duration, start + max_window)
                basis = ["analysis_window"]
                if end >= duration:
                    basis.append("video_end")
                windows.append((start, end, None, basis))
                start = end
            return windows

        for scene_pos, scene in enumerate(scenes):
            scene_start = float(scene.get("start_time", scene.get("start_seconds", 0.0)) or 0.0)
            scene_end = float(scene.get("end_time", scene.get("end_seconds", scene_start)) or scene_start)
            if scene_pos == len(scenes) - 1 and duration > 0:
                scene_end = duration
            scene_index = scene.get("scene_index", scene.get("index", scene_pos))
            cursor = scene_start
            first = True
            while cursor < scene_end:
                end = min(scene_end, cursor + max_window)
                basis = ["scene_cut" if first else "analysis_window"]
                if end < scene_end:
                    basis.append("analysis_window")
                if duration > 0 and end >= duration:
                    basis.append("video_end")
                windows.append((cursor, end, scene_index, list(dict.fromkeys(basis))))
                cursor = end
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
            if start <= float(frame.get("timestamp", -1)) <= end
        ]
        return {
            "scene_index": scene_index,
            "frame_paths": [str(frame.get("path", "")) for frame in selected if frame.get("path")],
            "frame_timestamps": [round(float(frame.get("timestamp", 0)), 3) for frame in selected],
        }

    def apply_semantic_enrichment(
        self,
        blueprint: dict[str, Any],
        enrichment: dict[str, Any],
    ) -> dict[str, Any]:
        """Replace scaffold windows with agent-defined, measured action segments.

        Python does not infer actions here. The agent supplies semantic segments and
        selects boundaries from sampled evidence. This method validates that choice,
        rebuilds evidence without out-of-range reuse, and performs the UTF-8-safe
        persistence merge used by ``execute``.
        """

        segment_specs = enrichment.get("segments")
        if not isinstance(segment_specs, list) or not segment_specs:
            raise ValueError("semantic enrichment must contain a non-empty segments list")

        duration = float(blueprint.get("source", {}).get("duration_seconds") or 0.0)
        if duration <= 0:
            raise ValueError("source duration must be positive before semantic enrichment")

        evidence_catalog = self._build_evidence_catalog(blueprint, enrichment)
        measured_timestamps = {0.0, duration}
        measured_timestamps.update(item["timestamp"] for item in evidence_catalog)
        for segment in blueprint.get("segments", []):
            if "scene_cut" in segment.get("boundary_basis", []):
                measured_timestamps.add(float(segment["start_seconds"]))

        defaults = enrichment.get("defaults") or {}
        refined_segments: list[dict[str, Any]] = []
        previous_end = 0.0
        action_boundary_count = 0

        for index, spec in enumerate(segment_specs, start=1):
            if not isinstance(spec, dict):
                raise ValueError(f"segment specification {index} must be an object")
            # An agent may enrich an existing window by ID without reauthoring
            # deterministic timing. Refined intervals still need measured bounds.
            original = next((item for item in blueprint["segments"] if item["id"] == spec.get("id")), None)
            spec = deepcopy(spec)
            if original and "start_seconds" not in spec and "end_seconds" not in spec:
                for field in ("start_seconds", "end_seconds", "boundary_basis"):
                    spec.setdefault(field, original[field])
            start = round(float(spec["start_seconds"]), 6)
            end = round(float(spec["end_seconds"]), 6)
            unchanged_window = original and self._close(start, original["start_seconds"]) and self._close(end, original["end_seconds"])
            if end <= start:
                raise ValueError(f"segment {index} must have positive duration")
            if not unchanged_window and not self._timestamp_is_measured(start, measured_timestamps):
                raise ValueError(f"segment {index} start {start} is not a measured timestamp")
            if not unchanged_window and not self._timestamp_is_measured(end, measured_timestamps):
                raise ValueError(f"segment {index} end {end} is not a measured timestamp")
            if index == 1 and not self._close(start, 0.0):
                raise ValueError("first enriched segment must start at 0")
            if index > 1 and not self._close(start, previous_end):
                raise ValueError(
                    f"segment {index} starts at {start}, expected contiguous boundary {previous_end}"
                )

            basis = list(dict.fromkeys(spec.get("boundary_basis") or []))
            if not basis:
                raise ValueError(f"segment {index} requires boundary_basis")
            if index > 1:
                derived = {"action_change", "interaction_change"}.intersection(basis)
                meaningful = derived or {
                    "scene_cut",
                    "audio_cue",
                    "motion_change",
                }.intersection(basis)
                if not meaningful and not (unchanged_window and basis == original["boundary_basis"]):
                    raise ValueError(
                        f"segment {index} boundary must be action/interaction-derived or another measured editorial boundary"
                    )
                if derived:
                    action_boundary_count += 1

            segment_evidence = [
                item
                for item in evidence_catalog
                if start - 1e-6 <= item["timestamp"] <= end + 1e-6
            ]
            if not segment_evidence:
                raise ValueError(f"segment {index} has no evidence inside [{start}, {end}]")

            motion = self._motion_for_interval(blueprint, start, end)
            motion.update(deepcopy(defaults.get("motion") or {}))
            motion.update(deepcopy(spec.get("motion") or {}))
            motion["flow_variance"] = self._normalise_flow_variance(motion.get("flow_variance"))

            segment_id = str(spec.get("id") or f"seg_{index:03d}")
            refined_segments.append(
                {
                    "id": segment_id,
                    "start_seconds": start,
                    "end_seconds": end,
                    "duration_seconds": round(end - start, 6),
                    "boundary_basis": basis,
                    "semantic": self._merge_section(
                        {
                            "actor": None,
                            "action": None,
                            "object": None,
                            "target": None,
                            "interaction": None,
                            "description": "",
                        },
                        defaults.get("semantic"),
                        spec.get("semantic"),
                    ),
                    "camera": self._merge_section(
                        {
                            "pov": None,
                            "shot_scale": None,
                            "angle": None,
                            "movement": None,
                            "steadiness": None,
                            "playback_speed": None,
                        },
                        defaults.get("camera"),
                        spec.get("camera"),
                    ),
                    "spatial": self._merge_section(
                        {
                            "actor_position": None,
                            "object_position": None,
                            "entry_direction": None,
                            "exit_direction": None,
                            "depth": None,
                            "framing_notes": "",
                        },
                        defaults.get("spatial"),
                        spec.get("spatial"),
                    ),
                    "motion": self._merge_section(
                        {
                            "motion_type": None,
                            "intensity": None,
                            "flow_variance": None,
                            "speed_behavior": None,
                        },
                        motion,
                    ),
                    "edit": self._merge_section(
                        {"transition_in": None, "transition_out": None, "segment_role": None},
                        defaults.get("edit"),
                        spec.get("edit"),
                    ),
                    "text": self._merge_section(
                        {"content": None, "position": None, "timing_notes": ""},
                        defaults.get("text"),
                        spec.get("text"),
                    ),
                    "audio": self._merge_section(
                        {"speech": None, "sound_cue": None, "beat_cue": None, "energy_notes": ""},
                        defaults.get("audio"),
                        spec.get("audio"),
                    ),
                    "evidence": {
                        "scene_index": spec.get(
                            "scene_index",
                            self._scene_index_for_interval(blueprint, start, end),
                        ),
                        "frame_paths": [item["path"] for item in segment_evidence],
                        "frame_timestamps": [item["timestamp"] for item in segment_evidence],
                    },
                    "confidence": self._merge_section(
                        {"timing": 1.0, "semantic": None, "camera": None, "overall": None},
                        defaults.get("confidence"),
                        spec.get("confidence"),
                    ),
                }
            )
            previous_end = end

        if not self._close(previous_end, duration):
            raise ValueError(
                f"last enriched segment ends at {previous_end}, expected source duration {duration}"
            )

        result = deepcopy(blueprint)
        result["source"].update(deepcopy(enrichment.get("source") or {}))
        result["segments"] = refined_segments
        choreography = deepcopy(enrichment.get("choreography") or {})
        if not choreography.get("action_order"):
            choreography["action_order"] = [
                f"{segment['id']}: {segment['semantic'].get('action') or 'unresolved'}"
                for segment in refined_segments
            ]
        result["choreography"] = self._merge_section(
            {
                "summary": "",
                "action_order": [],
                "critical_constraints": [],
                "soft_constraints": [],
            },
            choreography,
        )
        result["analysis_meta"]["semantic_enrichment_required"] = False
        result["analysis_meta"].setdefault("notes", []).extend(
            [
                f"Semantic refinement replaced analysis windows with {len(refined_segments)} measured action segments.",
                f"Action/interaction-derived internal boundaries: {action_boundary_count}.",
                "Evidence timestamps were range-validated during UTF-8 semantic merge.",
            ]
        )
        result["analysis_meta"]["notes"].extend(enrichment.get("analysis_notes") or [])
        self._validate_blueprint_invariants(result)
        return result

    def _build_evidence_catalog(
        self,
        blueprint: dict[str, Any],
        enrichment: dict[str, Any],
    ) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        for segment in blueprint.get("segments", []):
            evidence = segment.get("evidence") or {}
            paths = evidence.get("frame_paths") or []
            timestamps = evidence.get("frame_timestamps") or []
            for path, timestamp in zip(paths, timestamps):
                catalog.append(
                    {
                        "path": str(path),
                        "timestamp": round(float(timestamp), 6),
                        "scene_index": evidence.get("scene_index"),
                    }
                )
        for item in enrichment.get("evidence_catalog") or []:
            if not item.get("path"):
                continue
            catalog.append(
                {
                    "path": str(item["path"]),
                    "timestamp": round(float(item["timestamp"]), 6),
                    "scene_index": item.get("scene_index"),
                }
            )

        unique: dict[tuple[str, float], dict[str, Any]] = {}
        for item in catalog:
            unique[(item["path"], item["timestamp"])] = item
        return sorted(unique.values(), key=lambda item: (item["timestamp"], item["path"]))

    def _motion_for_interval(
        self,
        blueprint: dict[str, Any],
        start: float,
        end: float,
    ) -> dict[str, Any]:
        overlapping = [
            segment.get("motion") or {}
            for segment in blueprint.get("segments", [])
            if float(segment.get("start_seconds", 0)) < end
            and float(segment.get("end_seconds", 0)) > start
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

    def _scene_index_for_interval(
        self,
        blueprint: dict[str, Any],
        start: float,
        end: float,
    ) -> int | None:
        midpoint = (start + end) / 2
        for segment in blueprint.get("segments", []):
            if float(segment.get("start_seconds", 0)) <= midpoint <= float(segment.get("end_seconds", 0)):
                return segment.get("evidence", {}).get("scene_index")
        return None

    def _validate_blueprint_invariants(self, blueprint: dict[str, Any]) -> None:
        segments = blueprint.get("segments") or []
        if not segments:
            raise ValueError("blueprint must contain at least one segment")
        duration = float(blueprint.get("source", {}).get("duration_seconds") or 0.0)
        previous_end = 0.0
        for index, segment in enumerate(segments, start=1):
            start = float(segment["start_seconds"])
            end = float(segment["end_seconds"])
            if not self._close(start, previous_end):
                raise ValueError(f"segment {index} creates a gap or overlap at {start}")
            if end <= start:
                raise ValueError(f"segment {index} has non-positive duration")
            paths = segment.get("evidence", {}).get("frame_paths") or []
            timestamps = segment.get("evidence", {}).get("frame_timestamps") or []
            if len(paths) != len(timestamps):
                raise ValueError(f"segment {index} evidence path/timestamp counts differ")
            outside = [
                timestamp
                for timestamp in timestamps
                if float(timestamp) < start - 1e-6 or float(timestamp) > end + 1e-6
            ]
            if outside:
                raise ValueError(f"segment {index} has out-of-range evidence timestamps: {outside}")
            flow_variance = segment.get("motion", {}).get("flow_variance")
            if flow_variance is not None and (
                not isinstance(flow_variance, Real)
                or isinstance(flow_variance, bool)
                or float(flow_variance) < 0
            ):
                raise ValueError(f"segment {index} has invalid flow_variance {flow_variance}")
            previous_end = end
        if not self._close(previous_end, duration):
            raise ValueError(f"segments end at {previous_end}, expected duration {duration}")

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

    @classmethod
    def _timestamp_is_measured(cls, timestamp: float, measured: set[float]) -> bool:
        return any(cls._close(timestamp, candidate) for candidate in measured)

    @staticmethod
    def _close(left: float, right: float) -> bool:
        return abs(float(left) - float(right)) <= 1e-6

    @staticmethod
    def _write_json_utf8(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def _scene_by_index(self, scenes: list[dict[str, Any]], scene_index: int | None) -> dict[str, Any] | None:
        if scene_index is None:
            return None
        for pos, scene in enumerate(scenes):
            idx = scene.get("scene_index", scene.get("index", pos))
            if idx == scene_index:
                return scene
        return None
