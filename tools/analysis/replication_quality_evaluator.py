"""Deterministic replication-quality metrics for Moon/reference replication.

This tool measures timeline decisions and render integrity.  It does not make
semantic similarity judgments and does not choose replacement footage.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from statistics import mean
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


DEFAULT_QUALITY_THRESHOLDS: dict[str, Any] = {
    "speed": {
        "hard_min": 0.1,
        "warning_min": 0.33,
        "normal_min": 0.5,
        "normal_max": 2.0,
        "warning_max": 3.0,
        "hard_max": 8.0,
        "severe_ratio_fail": 0.25,
    },
    "fallback": {"warning_ratio": 0.25, "fail_ratio": 0.5},
    "reuse": {
        "warning_ratio": 0.4,
        "fail_ratio": 0.7,
        "warning_max_count": 3,
        "fail_max_count": 6,
        "warning_dominant_share": 0.5,
        "fail_dominant_share": 0.75,
        "warning_overlap_count": 3,
        "fail_overlap_count": 8,
    },
    "chronology": {
        "reorder_tolerance_seconds": 0.5,
        "large_backward_jump_seconds": 10.0,
        "warning_backward_jumps": 3,
        "fail_backward_jumps": 5,
        "warning_large_backward_jumps": 1,
        "fail_large_backward_jumps": 2,
        "warning_direction_changes": 4,
        "fail_direction_changes": 8,
        "warning_consistency_score": 0.8,
        "fail_consistency_score": 0.5,
    },
    "render": {"duration_tolerance_seconds": 0.15},
}


class ReplicationQualityEvaluator(BaseTool):
    name = "replication_quality_evaluator"
    version = "0.1.0"
    tier = ToolTier.ANALYZE
    capability = "reference_replication_quality"
    provider = "openmontage"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies: list[str] = []
    install_instructions = "No extra dependency beyond the OpenMontage Python environment."
    agent_skills: list[str] = []
    capabilities = [
        "measure_speed_adaptation",
        "measure_fallback_burden",
        "measure_source_reuse",
        "measure_source_chronology",
        "separate_render_integrity_from_replication_quality",
    ]
    best_for = ["Deterministic quality evidence before external-agent QC"]
    not_good_for = ["semantic similarity judgment", "choosing footage", "rendering"]

    input_schema = {
        "type": "object",
        "required": ["replication_timeline_path", "draft_render_path", "output_path"],
        "properties": {
            "replication_timeline_path": {"type": "string"},
            "draft_render_path": {"type": "string"},
            "output_path": {"type": "string"},
            "revision": {"type": "integer", "minimum": 0, "default": 0},
            "thresholds": {"type": "object"},
        },
    }
    output_schema = {
        "type": "object",
        "description": "Replication Quality Report — see schemas/artifacts/replication_quality_report.schema.json",
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=128, vram_mb=0, disk_mb=5, network_required=False)
    idempotency_key_fields = ["replication_timeline_path", "draft_render_path", "revision", "thresholds"]
    side_effects = ["writes replication_quality_report.json"]
    user_visible_verification = [
        "Review render_integrity separately from quality_gate",
        "Review per-decision speed severity and aggregate fallback/reuse/chronology metrics",
    ]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        timeline_path = Path(inputs["replication_timeline_path"])
        draft_path = Path(inputs["draft_render_path"])
        output_path = Path(inputs["output_path"])
        try:
            timeline = self._read_json(timeline_path)
            draft_render = self._read_json(draft_path)
            report = self.build_report(
                timeline,
                draft_render,
                revision=int(inputs.get("revision", 0)),
                thresholds=inputs.get("thresholds"),
            )
            self._validate_schema(report)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return ToolResult(success=False, error=f"Replication quality evaluation failed: {exc}")
        return ToolResult(success=True, data=report, artifacts=[str(output_path)])

    def build_report(
        self,
        timeline: dict[str, Any],
        draft_render: dict[str, Any],
        *,
        revision: int = 0,
        thresholds: dict[str, Any] | None = None,
        draft_exists: bool | None = None,
    ) -> dict[str, Any]:
        limits = self._thresholds(thresholds)
        segments = sorted(timeline.get("segments") or [], key=lambda item: int(item.get("order", 0)))
        if not segments:
            raise ValueError("Replication quality evaluation requires non-empty timeline segments")

        decisions: list[dict[str, Any]] = []
        source_segment_counts: dict[str, int] = {}
        source_segment_durations: dict[str, float] = {}
        source_paths: set[str] = set()
        prior_ranges: dict[str, list[tuple[float, float]]] = {}
        last_position: dict[str, float] = {}
        last_direction: dict[str, int] = {}
        overlap_reuse_count = 0
        backward_jump_count = 0
        large_backward_jump_count = 0
        direction_changes = 0
        speed_consistency_error_count = 0
        fallback_count = 0

        for segment in segments:
            source = segment.get("source") or {}
            target_duration = float(segment.get("target_duration_seconds") or 0.0)
            source_in = float(source.get("in_seconds") or 0.0)
            source_out = float(source.get("out_seconds") or 0.0)
            source_duration = source_out - source_in
            if source_duration <= 0 or target_duration <= 0:
                raise ValueError(f"{segment.get('id')}: source and target durations must be positive")

            effective_speed = source_duration / target_duration
            hold_seconds = float((segment.get("timing_fit") or {}).get("hold_seconds") or 0.0)
            severity = self._speed_severity(effective_speed, hold_seconds, limits["speed"])
            stored_speed = float((segment.get("timing_fit") or {}).get("speed") or 0.0)
            expected_render_speed = max(effective_speed, float(limits["speed"]["hard_min"]))
            speed_consistent = abs(stored_speed - expected_render_speed) <= 1e-5
            if not speed_consistent:
                speed_consistency_error_count += 1

            source_path = str(source.get("path") or "")
            footage_segment_id = str(source.get("footage_segment_id") or "")
            if not source_path or not footage_segment_id:
                raise ValueError(f"{segment.get('id')}: source path and footage_segment_id are required")
            source_paths.add(source_path)
            source_key = f"{source_path}|{footage_segment_id}|{source_in:.6f}|{source_out:.6f}"
            source_segment_counts[source_key] = source_segment_counts.get(source_key, 0) + 1
            source_segment_durations[source_key] = source_segment_durations.get(source_key, 0.0) + target_duration

            ranges = prior_ranges.setdefault(source_path, [])
            if any(min(source_out, old_out) - max(source_in, old_in) > 1e-6 for old_in, old_out in ranges):
                overlap_reuse_count += 1
            ranges.append((source_in, source_out))

            previous = last_position.get(source_path)
            if previous is not None:
                delta = source_in - previous
                tolerance = float(limits["chronology"]["reorder_tolerance_seconds"])
                direction = 1 if delta > tolerance else -1 if delta < -tolerance else 0
                if direction < 0:
                    backward_jump_count += 1
                    if abs(delta) >= float(limits["chronology"]["large_backward_jump_seconds"]):
                        large_backward_jump_count += 1
                if direction and source_path in last_direction and direction != last_direction[source_path]:
                    direction_changes += 1
                if direction:
                    last_direction[source_path] = direction
            last_position[source_path] = source_in

            match_class = str((segment.get("match") or {}).get("class") or "")
            if match_class == "fallback":
                fallback_count += 1
            decisions.append(
                {
                    "reference_segment_id": str(segment.get("reference_segment_id") or ""),
                    "footage_segment_id": footage_segment_id,
                    "source_path": source_path,
                    "source_in_seconds": round(source_in, 6),
                    "source_out_seconds": round(source_out, 6),
                    "source_duration_seconds": round(source_duration, 6),
                    "target_duration_seconds": round(target_duration, 6),
                    "effective_speed_factor": round(effective_speed, 6),
                    "speed_severity": severity,
                    "match_class": match_class,
                    "timeline_speed_consistent": speed_consistent,
                }
            )

        decision_count = len(decisions)
        unique_source_segment_count = len(source_segment_counts)
        fallback_ratio = fallback_count / decision_count
        reuse_ratio = (decision_count - unique_source_segment_count) / decision_count
        max_reuse_count = max(source_segment_counts.values())
        total_target_duration = sum(item["target_duration_seconds"] for item in decisions)
        dominant_source_share = max(source_segment_durations.values()) / total_target_duration
        speeds = [item["effective_speed_factor"] for item in decisions]
        speed_counts = {name: sum(item["speed_severity"] == name for item in decisions) for name in ("normal", "warning", "severe", "invalid")}
        transition_count = max(decision_count - len(source_paths), 1)
        chronology_penalty = (
            0.25 * backward_jump_count + 0.5 * large_backward_jump_count + 0.1 * direction_changes
        ) / transition_count
        chronology_score = max(0.0, 1.0 - chronology_penalty)

        quality_flags: list[dict[str, Any]] = []
        self._append_ratio_flag(quality_flags, "fallback", fallback_ratio, limits["fallback"], "fallback ratio")
        self._append_reuse_flags(
            quality_flags,
            reuse_ratio=reuse_ratio,
            max_reuse_count=max_reuse_count,
            dominant_source_share=dominant_source_share,
            overlap_reuse_count=overlap_reuse_count,
            limits=limits["reuse"],
        )
        self._append_speed_flags(quality_flags, speed_counts, decision_count, speed_consistency_error_count, limits["speed"])
        self._append_chronology_flags(
            quality_flags,
            backward_jump_count=backward_jump_count,
            large_backward_jump_count=large_backward_jump_count,
            direction_changes=direction_changes,
            consistency_score=chronology_score,
            limits=limits["chronology"],
        )

        quality_gate = self._gate(quality_flags)
        source_limited = quality_gate == "fail" and fallback_count == decision_count
        recommended_route = "footage" if source_limited else "match_or_timeline" if quality_gate == "fail" else "review" if quality_gate == "warning" else "none"
        render_integrity = self._render_integrity(timeline, draft_render, limits["render"], draft_exists=draft_exists)

        return {
            "version": "1.0",
            "revision": int(revision),
            "decision_count": decision_count,
            "fallback_count": fallback_count,
            "fallback_ratio": round(fallback_ratio, 6),
            "unique_source_count": len(source_paths),
            "unique_source_segment_count": unique_source_segment_count,
            "reuse_ratio": round(reuse_ratio, 6),
            "max_reuse_count": max_reuse_count,
            "dominant_source_share": round(dominant_source_share, 6),
            "overlap_reuse_count": overlap_reuse_count,
            "speed": {
                "canonical_definition": "source_duration_seconds / target_duration_seconds",
                "min": round(min(speeds), 6),
                "max": round(max(speeds), 6),
                "mean": round(mean(speeds), 6),
                "normal_count": speed_counts["normal"],
                "warning_count": speed_counts["warning"],
                "severe_count": speed_counts["severe"],
                "invalid_count": speed_counts["invalid"],
                "timeline_consistency_error_count": speed_consistency_error_count,
            },
            "chronology": {
                "backward_jump_count": backward_jump_count,
                "large_backward_jump_count": large_backward_jump_count,
                "source_direction_changes": direction_changes,
                "chronology_consistency_score": round(chronology_score, 6),
            },
            "decisions": decisions,
            "render_integrity": render_integrity,
            "replication_quality": {
                "status": quality_gate,
                "source_limited": source_limited,
                "fixable_by_render_revision": False,
                "recommended_route": recommended_route,
            },
            "quality_flags": quality_flags,
            "quality_gate": quality_gate,
            "metadata": {
                "generated_by": self.name,
                "thresholds": limits,
                "notes": [
                    "Metrics are deterministic and do not replace external-agent semantic QC.",
                    "Quality failure does not imply render infeasibility; render_integrity is reported separately.",
                ],
            },
        }

    @staticmethod
    def _speed_severity(speed: float, hold_seconds: float, limits: dict[str, Any]) -> str:
        if hold_seconds > 0 or speed < float(limits["hard_min"]) or speed > float(limits["hard_max"]):
            return "invalid"
        if speed < float(limits["warning_min"]) or speed > float(limits["warning_max"]):
            return "severe"
        if speed < float(limits["normal_min"]) or speed > float(limits["normal_max"]):
            return "warning"
        return "normal"

    @staticmethod
    def _append_ratio_flag(flags: list[dict[str, Any]], code: str, value: float, limits: dict[str, Any], label: str) -> None:
        severity = "fail" if value >= float(limits["fail_ratio"]) else "warning" if value >= float(limits["warning_ratio"]) else None
        if severity:
            flags.append({"code": f"{code}_{severity}", "severity": severity, "message": f"{label} is {value:.3f}", "value": round(value, 6)})

    @staticmethod
    def _append_reuse_flags(flags: list[dict[str, Any]], *, reuse_ratio: float, max_reuse_count: int, dominant_source_share: float, overlap_reuse_count: int, limits: dict[str, Any]) -> None:
        metrics = [
            ("reuse_ratio", reuse_ratio, "warning_ratio", "fail_ratio"),
            ("max_reuse_count", max_reuse_count, "warning_max_count", "fail_max_count"),
            ("dominant_source_share", dominant_source_share, "warning_dominant_share", "fail_dominant_share"),
            ("overlap_reuse_count", overlap_reuse_count, "warning_overlap_count", "fail_overlap_count"),
        ]
        for code, value, warning_key, fail_key in metrics:
            severity = "fail" if value >= limits[fail_key] else "warning" if value >= limits[warning_key] else None
            if severity:
                flags.append({"code": f"source_{code}_{severity}", "severity": severity, "message": f"{code} is {value}", "value": round(float(value), 6)})

    @staticmethod
    def _append_speed_flags(flags: list[dict[str, Any]], counts: dict[str, int], decision_count: int, consistency_errors: int, limits: dict[str, Any]) -> None:
        if counts["invalid"]:
            flags.append({"code": "speed_invalid", "severity": "fail", "message": f"{counts['invalid']} decision(s) exceed the hard quality speed range", "value": counts["invalid"]})
        severe_ratio = counts["severe"] / decision_count
        if counts["severe"]:
            severity = "fail" if severe_ratio >= float(limits["severe_ratio_fail"]) else "warning"
            flags.append({"code": f"speed_severe_{severity}", "severity": severity, "message": f"{counts['severe']} decision(s) require severe speed adaptation", "value": round(severe_ratio, 6)})
        if counts["warning"]:
            flags.append({"code": "speed_warning", "severity": "warning", "message": f"{counts['warning']} decision(s) are outside the normal speed range", "value": counts["warning"]})
        if consistency_errors:
            flags.append({"code": "timeline_speed_inconsistent", "severity": "fail", "message": f"{consistency_errors} timeline speed value(s) disagree with the canonical ratio", "value": consistency_errors})

    @staticmethod
    def _append_chronology_flags(flags: list[dict[str, Any]], *, backward_jump_count: int, large_backward_jump_count: int, direction_changes: int, consistency_score: float, limits: dict[str, Any]) -> None:
        metrics = [
            ("backward_jumps", backward_jump_count, "warning_backward_jumps", "fail_backward_jumps"),
            ("large_backward_jumps", large_backward_jump_count, "warning_large_backward_jumps", "fail_large_backward_jumps"),
            ("direction_changes", direction_changes, "warning_direction_changes", "fail_direction_changes"),
        ]
        for code, value, warning_key, fail_key in metrics:
            severity = "fail" if value >= limits[fail_key] else "warning" if value >= limits[warning_key] else None
            if severity:
                flags.append({"code": f"chronology_{code}_{severity}", "severity": severity, "message": f"{code} is {value}", "value": value})
        severity = "fail" if consistency_score < float(limits["fail_consistency_score"]) else "warning" if consistency_score < float(limits["warning_consistency_score"]) else None
        if severity:
            flags.append({"code": f"chronology_consistency_{severity}", "severity": severity, "message": f"chronology consistency score is {consistency_score:.3f}", "value": round(consistency_score, 6)})

    @staticmethod
    def _gate(flags: list[dict[str, Any]]) -> str:
        if any(item["severity"] == "fail" for item in flags):
            return "fail"
        if flags:
            return "warning"
        return "pass"

    @staticmethod
    def _render_integrity(timeline: dict[str, Any], draft_render: dict[str, Any], limits: dict[str, Any], *, draft_exists: bool | None) -> dict[str, Any]:
        coverage = timeline.get("coverage") or {}
        tool_result = draft_render.get("tool_result") or {}
        output = str(draft_render.get("output") or tool_result.get("output") or "")
        exists = bool(draft_exists) if draft_exists is not None else bool(output and Path(output).is_file())
        delta_raw = tool_result.get("duration_delta_seconds")
        if delta_raw is None and tool_result.get("duration_seconds") is not None and tool_result.get("expected_duration_seconds") is not None:
            delta_raw = abs(float(tool_result["duration_seconds"]) - float(tool_result["expected_duration_seconds"]))
        delta = float(delta_raw) if delta_raw is not None else None
        tolerance = float(limits["duration_tolerance_seconds"])
        flags: list[dict[str, Any]] = []
        if coverage.get("full_coverage") is not True:
            flags.append({"code": "timeline_coverage_failed", "message": "timeline does not declare full coverage"})
        if coverage.get("timeline_contiguous") is not True:
            flags.append({"code": "timeline_contiguity_failed", "message": "timeline is not contiguous"})
        if not exists:
            flags.append({"code": "draft_missing", "message": "draft output file does not exist"})
        if delta is None:
            flags.append({"code": "duration_measurement_missing", "message": "draft duration delta is unavailable"})
        elif delta > tolerance:
            flags.append({"code": "duration_tolerance_exceeded", "message": f"duration delta {delta:.3f}s exceeds {tolerance:.3f}s"})
        return {
            "status": "fail" if flags else "pass",
            "timeline_full_coverage": coverage.get("full_coverage") is True,
            "timeline_contiguous": coverage.get("timeline_contiguous") is True,
            "draft_exists": exists,
            "duration_delta_seconds": round(delta, 6) if delta is not None else None,
            "duration_tolerance_seconds": round(tolerance, 6),
            "flags": flags,
        }

    @staticmethod
    def _thresholds(overrides: dict[str, Any] | None) -> dict[str, Any]:
        limits = deepcopy(DEFAULT_QUALITY_THRESHOLDS)
        for group, values in (overrides or {}).items():
            if group not in limits or not isinstance(values, dict):
                raise ValueError(f"unknown quality threshold group: {group!r}")
            for key, value in values.items():
                if key not in limits[group]:
                    raise ValueError(f"unknown quality threshold: {group}.{key}")
                limits[group][key] = value
        speed = limits["speed"]
        ordered = [speed["hard_min"], speed["warning_min"], speed["normal_min"], speed["normal_max"], speed["warning_max"], speed["hard_max"]]
        if ordered != sorted(ordered) or float(speed["hard_min"]) <= 0:
            raise ValueError("speed thresholds must be positive and ordered hard_min <= warning_min <= normal_min <= normal_max <= warning_max <= hard_max")
        return limits

    def _validate_schema(self, report: dict[str, Any]) -> None:
        try:
            import jsonschema
        except ImportError:
            return
        schema_path = Path(__file__).resolve().parents[2] / "schemas" / "artifacts" / "replication_quality_report.schema.json"
        schema = self._read_json(schema_path)
        jsonschema.validate(instance=report, schema=schema)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise OSError(f"JSON file not found: {path}")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError(f"Expected JSON object: {path}")
        return value
