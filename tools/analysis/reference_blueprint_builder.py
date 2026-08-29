"""Build a deterministic Reference Blueprint scaffold from a reference video.

This tool deliberately does NOT infer semantic actions. OpenMontage's agent-first
architecture keeps creative/semantic reasoning in skills. Python is responsible
for measurable timing, scene/motion evidence, frame references, and a stable
artifact shape that the multimodal agent enriches afterward.
"""

from __future__ import annotations

import json
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
    version = "0.1.0"
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
                    "start_seconds": round(start, 3),
                    "end_seconds": round(end, 3),
                    "duration_seconds": round(max(0.0, end - start), 3),
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
                        "flow_variance": scene.get("flow_variance") if scene else None,
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

        out_path = output_dir / "reference_blueprint.json"
        out_path.write_text(json.dumps(blueprint, indent=2, ensure_ascii=False), encoding="utf-8")
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
        if not selected and keyframes:
            midpoint = (start + end) / 2
            selected = [min(keyframes, key=lambda frame: abs(float(frame.get("timestamp", 0)) - midpoint))]
        return {
            "scene_index": scene_index,
            "frame_paths": [str(frame.get("path", "")) for frame in selected if frame.get("path")],
            "frame_timestamps": [round(float(frame.get("timestamp", 0)), 3) for frame in selected],
        }

    def _scene_by_index(self, scenes: list[dict[str, Any]], scene_index: int | None) -> dict[str, Any] | None:
        if scene_index is None:
            return None
        for pos, scene in enumerate(scenes):
            idx = scene.get("scene_index", scene.get("index", pos))
            if idx == scene_index:
                return scene
        return None
