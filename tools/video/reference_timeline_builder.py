"""Build a runtime-agnostic replication timeline from Phase 1 + Phase 2 artifacts.

The tool is intentionally mechanical. It preserves the Reference Blueprint's
ordering and timing, resolves each reference segment to the concrete source
range selected by Phase 2, and computes the minimum timing adaptation required
for a later renderer.

Creative/editorial choices remain with the agent. This tool does not choose a
render runtime, replace a match, invent semantic cues, or render video.
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


class ReferenceTimelineBuilder(BaseTool):
    """Convert a full-coverage matching plan into an exact reference timeline."""

    name = "reference_timeline_builder"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "reference_replication_timeline"
    provider = "openmontage"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies: list[str] = []
    install_instructions = "No extra runtime dependency beyond the OpenMontage Python environment."
    agent_skills: list[str] = []
    capabilities = [
        "preserve_reference_timing",
        "resolve_matched_source_ranges",
        "compute_speed_fit",
        "flag_fallback_and_timing_risks",
    ]
    best_for = [
        "Phase 3 reference replication timeline construction",
        "turning matching.json into a render-ready but runtime-agnostic timeline",
    ]
    not_good_for = [
        "choosing footage",
        "choosing a render runtime",
        "creative transition redesign",
        "rendering video",
    ]

    input_schema = {
        "type": "object",
        "required": ["reference_blueprint_path", "reference_matching_path", "output_path"],
        "properties": {
            "reference_blueprint_path": {"type": "string"},
            "reference_matching_path": {"type": "string"},
            "output_path": {"type": "string"},
            "contiguity_tolerance_seconds": {
                "type": "number",
                "minimum": 0,
                "maximum": 0.25,
                "default": 0.05,
            },
            "extreme_speed_min": {
                "type": "number",
                "minimum": 0.1,
                "default": 0.5,
            },
            "extreme_speed_max": {
                "type": "number",
                "minimum": 1.0,
                "default": 2.0,
            },
        },
    }
    output_schema = {
        "type": "object",
        "description": "Replication Timeline artifact — see schemas/artifacts/replication_timeline.schema.json",
    }

    resource_profile = ResourceProfile(
        cpu_cores=1,
        ram_mb=256,
        vram_mb=0,
        disk_mb=10,
        network_required=False,
    )
    idempotency_key_fields = ["reference_blueprint_path", "reference_matching_path"]
    side_effects = ["writes replication_timeline.json"]
    user_visible_verification = [
        "Verify timeline order matches Reference Blueprint order",
        "Verify every reference segment resolves to a concrete source range",
        "Review fallback, hold, and extreme-speed warnings before rendering",
    ]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        blueprint_path = Path(inputs["reference_blueprint_path"])
        matching_path = Path(inputs["reference_matching_path"])
        output_path = Path(inputs["output_path"])

        try:
            blueprint = self._read_json(blueprint_path)
            matching = self._read_json(matching_path)
            timeline = self.build_timeline(
                blueprint,
                matching,
                reference_blueprint_path=str(blueprint_path),
                reference_matching_path=str(matching_path),
                contiguity_tolerance_seconds=float(inputs.get("contiguity_tolerance_seconds", 0.05)),
                extreme_speed_min=float(inputs.get("extreme_speed_min", 0.5)),
                extreme_speed_max=float(inputs.get("extreme_speed_max", 2.0)),
            )
            self._validate_schema(timeline)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(timeline, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return ToolResult(success=False, error=f"Reference timeline build failed: {exc}")

        return ToolResult(success=True, data=timeline, artifacts=[str(output_path)])

    def build_timeline(
        self,
        blueprint: dict[str, Any],
        matching: dict[str, Any],
        *,
        reference_blueprint_path: str = "reference_blueprint.json",
        reference_matching_path: str = "matching.json",
        contiguity_tolerance_seconds: float = 0.05,
        extreme_speed_min: float = 0.5,
        extreme_speed_max: float = 2.0,
    ) -> dict[str, Any]:
        if extreme_speed_min < 0.1:
            raise ValueError("extreme_speed_min must be >= 0.1")
        if extreme_speed_max < extreme_speed_min:
            raise ValueError("extreme_speed_max must be >= extreme_speed_min")

        reference_duration, reference_segments = self._validate_blueprint(
            blueprint,
            tolerance=contiguity_tolerance_seconds,
        )
        matches_by_reference = self._validate_matching(matching, reference_segments)

        output_segments: list[dict[str, Any]] = []
        warnings: list[str] = []
        fallback_count = 0
        extreme_speed_count = 0
        hold_segment_count = 0

        for order, reference in enumerate(reference_segments, start=1):
            reference_id = str(reference["id"])
            match = matches_by_reference[reference_id]
            selected = match["selected"]

            timeline_start = round(float(reference["start_seconds"]), 6)
            timeline_end = round(float(reference["end_seconds"]), 6)
            target_duration = round(timeline_end - timeline_start, 6)
            source_in = round(float(selected["source_in"]), 6)
            source_out = round(float(selected["source_out"]), 6)
            source_duration = round(source_out - source_in, 6)

            timing_fit = self._compute_timing_fit(
                source_duration=source_duration,
                target_duration=target_duration,
                extreme_speed_min=extreme_speed_min,
                extreme_speed_max=extreme_speed_max,
            )

            match_class = str(match["match_class"])
            overall_score = float((match.get("scores") or {}).get("overall", 0.0))
            quality_risks = [str(item) for item in match.get("tradeoffs") or []]

            if match_class == "fallback":
                fallback_count += 1
                quality_risks.append("Phase 2 fallback: footage is not a close reference match")
                warnings.append(
                    f"{reference_id}: fallback footage selected; final fidelity depends on better source footage."
                )

            if timing_fit["extreme_speed"]:
                extreme_speed_count += 1
                quality_risks.append(
                    f"Timing requires {timing_fit['speed']:.3f}x playback to preserve reference duration"
                )
                warnings.append(
                    f"{reference_id}: extreme timing adaptation ({timing_fit['speed']:.3f}x)."
                )

            if timing_fit["hold_seconds"] > 0:
                hold_segment_count += 1
                quality_risks.append(
                    f"Source is too short even at 0.1x; renderer must hold the final frame for {timing_fit['hold_seconds']:.3f}s"
                )
                warnings.append(
                    f"{reference_id}: source is very short; requires {timing_fit['hold_seconds']:.3f}s frame hold."
                )

            edit = reference.get("edit") or {}
            output_segments.append(
                {
                    "id": f"timeline_{order:03d}",
                    "order": order,
                    "reference_segment_id": reference_id,
                    "timeline_start": timeline_start,
                    "timeline_end": timeline_end,
                    "target_duration_seconds": target_duration,
                    "source": {
                        "path": str(selected["source_path"]),
                        "footage_segment_id": str(selected["footage_segment_id"]),
                        "in_seconds": source_in,
                        "out_seconds": source_out,
                        "duration_seconds": source_duration,
                    },
                    "timing_fit": timing_fit,
                    "match": {
                        "class": match_class,
                        "overall_score": round(overall_score, 6),
                        "rationale": str(match.get("rationale") or ""),
                        "tradeoffs": [str(item) for item in match.get("tradeoffs") or []],
                    },
                    "reference_cues": {
                        "transition_in": self._nullable_string(edit.get("transition_in")),
                        "transition_out": self._nullable_string(edit.get("transition_out")),
                        "camera": dict(reference.get("camera") or {}),
                        "spatial": dict(reference.get("spatial") or {}),
                        "text": dict(reference.get("text") or {}),
                        "audio": dict(reference.get("audio") or {}),
                    },
                    "quality_risks": list(dict.fromkeys(quality_risks)),
                }
            )

        timeline = {
            "version": "1.0",
            "reference_duration_seconds": round(reference_duration, 6),
            "segments": output_segments,
            "coverage": {
                "segment_count": len(output_segments),
                "full_coverage": True,
                "timeline_contiguous": True,
                "fallback_count": fallback_count,
                "extreme_speed_count": extreme_speed_count,
                "hold_segment_count": hold_segment_count,
            },
            "warnings": warnings,
            "metadata": {
                "generated_by": self.name,
                "reference_blueprint_path": reference_blueprint_path,
                "reference_matching_path": reference_matching_path,
                "render_runtime_locked": False,
                "notes": [
                    "Phase 3 is runtime-agnostic; Phase 4 must choose/confirm a render runtime before composition.",
                    "Timeline timing comes from measured Reference Blueprint boundaries, not from source clip lengths.",
                    "Phase 2 fallback choices remain visible instead of creating empty timeline slots.",
                ],
            },
        }
        self._validate_timeline_invariants(timeline, tolerance=contiguity_tolerance_seconds)
        return timeline

    def _validate_blueprint(
        self,
        blueprint: dict[str, Any],
        *,
        tolerance: float,
    ) -> tuple[float, list[dict[str, Any]]]:
        source = blueprint.get("source") or {}
        duration = float(source.get("duration_seconds") or 0.0)
        if duration <= 0:
            raise ValueError("Reference Blueprint requires a positive source.duration_seconds")

        raw_segments = blueprint.get("segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            raise ValueError("Reference Blueprint requires non-empty segments")

        segments = sorted(raw_segments, key=lambda item: float(item.get("start_seconds", 0.0)))
        ids = [str(item.get("id") or "") for item in segments]
        if any(not item for item in ids) or len(set(ids)) != len(ids):
            raise ValueError("Reference Blueprint segment ids must be non-empty and unique")

        previous_end = 0.0
        for index, segment in enumerate(segments):
            start = float(segment.get("start_seconds", -1.0))
            end = float(segment.get("end_seconds", -1.0))
            if start < 0 or end <= start:
                raise ValueError(f"Reference segment {segment.get('id')} has invalid timing [{start}, {end}]")
            if index == 0 and abs(start) > tolerance:
                raise ValueError(f"Reference timeline must begin at 0s; got {start}")
            if index > 0 and abs(start - previous_end) > tolerance:
                relation = "gap" if start > previous_end else "overlap"
                raise ValueError(
                    f"Reference Blueprint is not contiguous: {relation} before {segment.get('id')} "
                    f"({previous_end} -> {start})"
                )
            previous_end = end

        if abs(previous_end - duration) > tolerance:
            raise ValueError(
                f"Reference Blueprint does not cover full duration: final end={previous_end}, duration={duration}"
            )
        return duration, segments

    def _validate_matching(
        self,
        matching: dict[str, Any],
        reference_segments: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        coverage = matching.get("coverage") or {}
        if coverage.get("full_coverage") is not True:
            raise ValueError("Phase 2 matching must declare full_coverage=true")

        raw_matches = matching.get("matches")
        if not isinstance(raw_matches, list) or not raw_matches:
            raise ValueError("Phase 2 matching requires non-empty matches")

        by_reference: dict[str, dict[str, Any]] = {}
        for match in raw_matches:
            reference_id = str(match.get("reference_segment_id") or "")
            if not reference_id or reference_id in by_reference:
                raise ValueError("Matching reference_segment_id values must be non-empty and unique")
            selected = match.get("selected") or {}
            source_in = float(selected.get("source_in", -1.0))
            source_out = float(selected.get("source_out", -1.0))
            if not selected.get("source_path") or not selected.get("footage_segment_id"):
                raise ValueError(f"{reference_id}: selected footage is incomplete")
            if source_in < 0 or source_out <= source_in:
                raise ValueError(f"{reference_id}: selected source range is invalid")
            match_class = match.get("match_class")
            if match_class not in {"good", "acceptable", "fallback"}:
                raise ValueError(f"{reference_id}: invalid match_class {match_class!r}")
            overall = (match.get("scores") or {}).get("overall")
            if overall is None or not 0 <= float(overall) <= 1:
                raise ValueError(f"{reference_id}: scores.overall must be between 0 and 1")
            by_reference[reference_id] = match

        expected_ids = {str(segment["id"]) for segment in reference_segments}
        actual_ids = set(by_reference)
        if expected_ids != actual_ids:
            missing = sorted(expected_ids - actual_ids)
            extra = sorted(actual_ids - expected_ids)
            raise ValueError(f"Phase 2 coverage mismatch; missing={missing}, extra={extra}")

        if int(coverage.get("reference_segment_count", len(expected_ids))) != len(expected_ids):
            raise ValueError("Phase 2 reference_segment_count disagrees with Reference Blueprint")
        if int(coverage.get("matched_segment_count", len(actual_ids))) != len(actual_ids):
            raise ValueError("Phase 2 matched_segment_count disagrees with matches")
        return by_reference

    def _compute_timing_fit(
        self,
        *,
        source_duration: float,
        target_duration: float,
        extreme_speed_min: float,
        extreme_speed_max: float,
    ) -> dict[str, Any]:
        if source_duration <= 0 or target_duration <= 0:
            raise ValueError("Source and target durations must both be positive")

        exact_speed = source_duration / target_duration
        if exact_speed >= 0.1:
            speed = exact_speed
            hold_seconds = 0.0
            mode = "speed_fit"
        else:
            speed = 0.1
            played_duration = source_duration / speed
            hold_seconds = max(0.0, target_duration - played_duration)
            mode = "speed_fit_with_hold"

        extreme = speed < extreme_speed_min or speed > extreme_speed_max or hold_seconds > 0
        return {
            "mode": mode,
            "speed": round(speed, 6),
            "hold_seconds": round(hold_seconds, 6),
            "extreme_speed": bool(extreme),
        }

    def _validate_timeline_invariants(self, timeline: dict[str, Any], *, tolerance: float) -> None:
        segments = timeline.get("segments") or []
        if not segments:
            raise ValueError("Replication timeline has no segments")
        previous_end = 0.0
        for index, segment in enumerate(segments):
            start = float(segment["timeline_start"])
            end = float(segment["timeline_end"])
            if index == 0 and abs(start) > tolerance:
                raise ValueError("Replication timeline does not start at 0s")
            if index > 0 and abs(start - previous_end) > tolerance:
                raise ValueError("Replication timeline is not contiguous")
            if end <= start:
                raise ValueError(f"{segment['id']} has non-positive target duration")
            previous_end = end
        if abs(previous_end - float(timeline["reference_duration_seconds"])) > tolerance:
            raise ValueError("Replication timeline does not cover the full reference duration")

    def _validate_schema(self, timeline: dict[str, Any]) -> None:
        try:
            import jsonschema
        except ImportError:
            return
        schema_path = Path(__file__).resolve().parents[2] / "schemas" / "artifacts" / "replication_timeline.schema.json"
        schema = self._read_json(schema_path)
        jsonschema.validate(instance=timeline, schema=schema)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise OSError(f"JSON file not found: {path}")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError(f"Expected JSON object: {path}")
        return value

    @staticmethod
    def _nullable_string(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
